import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.clock import FakeClock, SystemClock
from app.config import Settings
from app.jobs.heads import build_assignments, suggest_heads
from app.jobs.models import JobStatus
from app.main import build_runtime
from app.printer.client import PrinterClient
from app.printer.events import Connected, EventBus
from app.printer.models import PrinterStatus, PrintState
from app.printer.watcher import PrinterWatcher
from tests.fakes import eventually, stop

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).parents[1] / "fixtures" / "two_colour.gcode"
START_WAIT_SECONDS = 15
RUNNING = (PrintState.PRINTING, PrintState.PAUSED)


class PacedClock(FakeClock):
    """A FakeClock whose sleeps also pass in real time.

    The scheduler's "now" stays under the test's control, while the simulator, which
    runs on wall time, gets the time it needs to report a started print over the
    websocket before the runner's start check gives up.
    """

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(seconds)


@pytest.fixture
async def printer(sim_url):
    client = PrinterClient(sim_url)
    yield client
    await client.close()


async def upload_fixture(printer: PrinterClient) -> str:
    name = f"schedule-print-test-{uuid.uuid4().hex[:8]}.gcode"
    await printer.upload(name, FIXTURE.read_bytes())
    return name


async def require_idle(printer: PrinterClient) -> None:
    status = await printer.status()
    if not status.idle:
        pytest.skip(f"simulator is busy ({status.state.value}); its job timeline loops")


async def read_head_map(printer: PrinterClient) -> list[int]:
    objects = await printer.query({"print_task_config": ["extruder_map_table"]})
    return objects["print_task_config"]["extruder_map_table"]


async def wait_until_printing(printer: PrinterClient, name: str) -> PrinterStatus:
    """The printer's status once it reports `name` printing or paused, or after START_WAIT_SECONDS."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_WAIT_SECONDS
    while True:
        status = await printer.status()
        if (status.state in RUNNING and status.filename == name) or loop.time() > deadline:
            return status
        await asyncio.sleep(0.5)


async def cancel_print(printer: PrinterClient, name: str) -> None:
    """Cancel `name` if it is running, so the simulator is idle for the next test."""
    status = await printer.status()
    if status.state in RUNNING and status.filename == name:
        await printer.run_gcode("CANCEL_PRINT")


async def test_status_has_four_heads(printer):
    status = await printer.status()
    assert status.online and status.klippy_ready and len(status.heads) == 4


async def test_upload_lists_file_and_reads_colours(printer):
    name = await upload_fixture(printer)
    assert name in [f.filename for f in await printer.files()]
    info = await printer.file_info(name)
    assert [(c.logical, c.rgb, c.material) for c in info.colours] == [
        (0, "FF0000", "PLA"), (1, "0000FF", "PETG")]


async def test_head_map_round_trip(printer):
    await require_idle(printer)
    await printer.apply_head_map([(0, 1), (1, 3)])
    assert (await read_head_map(printer))[:2] == [1, 3]
    await printer.apply_head_map([(0, 0), (1, 1)])
    assert (await read_head_map(printer))[:2] == [0, 1]


async def test_history_answers_without_raising(printer):
    result = await printer.history(datetime.now(timezone.utc) - timedelta(days=1))
    assert result is None or isinstance(result, list)


async def test_watcher_connects(sim_url):
    bus, seen = EventBus(), []

    async def collect(event):
        seen.append(event)

    bus.subscribe(collect)
    watcher = PrinterWatcher(sim_url.replace("http://", "ws://") + "/websocket", bus, SystemClock())
    task = asyncio.create_task(watcher.run())
    try:
        await eventually(lambda: any(isinstance(e, Connected) for e in seen), timeout=10)
        assert watcher.status.online
    finally:
        await stop(task)


async def test_start_print_is_accepted(printer):
    name = await upload_fixture(printer)
    await require_idle(printer)
    await printer.start_print(name)
    try:
        status = await wait_until_printing(printer, name)
        assert status.state in RUNNING and status.filename == name, status
    finally:
        await cancel_print(printer, name)


async def test_full_scheduler_run_against_sim(sim_url, tmp_path):
    url = urlsplit(sim_url)
    settings = Settings(printer_host=url.hostname, printer_port=url.port or 7125,
                        app_password="unused", tz="UTC", data_dir=tmp_path)
    clock = PacedClock(datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc))
    rt = build_runtime(settings, clock=clock)
    assert not rt.settings.telegram_enabled
    watcher = asyncio.create_task(rt.watcher.run())
    name = None
    try:
        await eventually(lambda: rt.bedcheck.watching, timeout=10)
        await require_idle(rt.printer)
        status = rt.watcher.status
        rt.bedcheck.confirm()

        name = await upload_fixture(rt.printer)
        info = await rt.printer.file_info(name)
        choice = suggest_heads(info.colours, status.heads)
        job = rt.jobs.create(filename=name, file_size=info.size, file_modified=info.modified,
                             scheduled_at=clock.now(),
                             head_map=build_assignments(choice, info.colours, status.heads))

        await rt.runner.tick()

        done = rt.jobs.get(job.id)
        assert (done.status, done.outcome_reason) == (JobStatus.STARTED, None)
        assert rt.bedcheck.latest().consumed_by_job_id == job.id
        assert (await read_head_map(rt.printer))[:2] == [a.head for a in job.head_map]
    finally:
        if name is not None:
            await cancel_print(rt.printer, name)
        await stop(watcher)
        await rt.printer.close()
        rt.conn.close()
