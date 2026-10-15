import asyncio

from app.bedcheck.service import BedCheck
from app.printer.events import (
    Connected, Disconnected, EventBus, Heartbeat, PrintFinished, PrintStarted,
)
from app.printer.models import PrintState
from app.printer.watcher import PrinterWatcher, StateTracker
from tests.factories import IDLE_OBJECTS, make_status, printing_status
from tests.fakes import FakeHistory, FakeWS, connector, eventually, stop


def make_watcher(clock, *sockets, heartbeat=30.0, bus=None):
    bus = bus or EventBus()
    seen = []

    async def collect(event):
        seen.append(event)

    bus.subscribe(collect)
    watcher = PrinterWatcher("ws://printer/websocket", bus, clock,
                             connect=connector(*sockets), heartbeat_seconds=heartbeat)
    return watcher, seen


def test_tracker_detects_start_and_finish(clock):
    tracker = StateTracker()
    tracker.reset(make_status())
    now = clock.now()
    assert tracker.update(printing_status("a.gcode"), now) == [PrintStarted(at=now, filename="a.gcode")]
    done = make_status(state=PrintState.COMPLETE, filename="a.gcode")
    assert tracker.update(done, now) == [PrintFinished(at=now, filename="a.gcode", result="complete")]


def test_tracker_without_baseline_reports_nothing(clock):
    assert StateTracker().update(printing_status(), clock.now()) == []


def test_tracker_counts_a_start_that_pauses_at_once(clock):
    # Firmware can pause within one status batch (e.g. a filament prompt): standby → paused.
    tracker = StateTracker()
    tracker.reset(make_status())
    now = clock.now()
    paused = make_status(state=PrintState.PAUSED, idle=False, paused=True, filename="a.gcode")
    assert tracker.update(paused, now) == [PrintStarted(at=now, filename="a.gcode")]
    assert tracker.update(printing_status("a.gcode"), now) == []  # resuming is not a new start


async def test_print_that_pauses_as_it_starts_voids_the_confirmation(db, clock):
    ws, bus = FakeWS(IDLE_OBJECTS), EventBus()
    bed = BedCheck(db, clock, FakeHistory(), lambda: watcher.status)
    bed.startup()
    bus.subscribe(bed.handle_event)
    watcher, seen = make_watcher(clock, ws, bus=bus)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: bed.watching)
    bed.confirm()
    ws.push_update({"print_stats": {"state": "paused", "filename": "touch.gcode"},
                    "pause_resume": {"is_paused": True}})
    ws.push_update({"print_stats": {"state": "printing"}, "pause_resume": {"is_paused": False}})
    ws.push_update({"print_stats": {"state": "complete"}})
    await eventually(lambda: any(isinstance(e, PrintFinished) for e in seen))
    assert bed.active() is None
    await stop(task)


async def test_connect_publishes_status(clock):
    ws = FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    assert isinstance(seen[0], Connected) and seen[0].status.idle
    assert watcher.status.online
    assert ws.sent[0]["method"] == "printer.objects.subscribe"
    await stop(task)


async def test_print_start_and_finish_events(clock):
    ws = FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    ws.push_update({"print_stats": {"state": "printing", "filename": "cube.gcode"}})
    ws.push_update({"print_stats": {"state": "paused"}})
    ws.push_update({"print_stats": {"state": "printing"}})
    ws.push_update({"print_stats": {"state": "complete"}})
    await eventually(lambda: any(isinstance(e, PrintFinished) for e in seen))
    starts = [e for e in seen if isinstance(e, PrintStarted)]
    finish = next(e for e in seen if isinstance(e, PrintFinished))
    assert [s.filename for s in starts] == ["cube.gcode"]  # resuming is not a new start
    assert (finish.filename, finish.result) == ("cube.gcode", "complete")
    await stop(task)


async def test_heartbeat_when_quiet(clock):
    watcher, seen = make_watcher(clock, FakeWS(IDLE_OBJECTS), heartbeat=0.02)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: any(isinstance(e, Heartbeat) for e in seen))
    await stop(task)


async def test_reconnects_after_drop(clock):
    first, second = FakeWS(IDLE_OBJECTS), FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, first, second)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    first.drop()
    await eventually(lambda: sum(isinstance(e, Connected) for e in seen) == 2)
    kinds = [type(e).__name__ for e in seen if not isinstance(e, Heartbeat)]
    assert kinds == ["Connected", "Disconnected", "Connected"]
    await stop(task)


async def test_klippy_shutdown_counts_as_disconnect(clock):
    ws = FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    ws.push({"jsonrpc": "2.0", "method": "notify_klippy_shutdown"})
    await eventually(lambda: any(isinstance(e, Disconnected) for e in seen))
    assert watcher.status.online is False
    await stop(task)


async def test_unrelated_messages_during_subscribe_are_skipped(clock):
    ws = FakeWS(IDLE_OBJECTS)
    ws.push({"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]})
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    assert isinstance(seen[0], Connected)
    await stop(task)
