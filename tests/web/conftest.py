import asyncio
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.bedcheck.service import BedCheck
from app.clock import FakeClock
from app.config import Settings
from app.jobs.store import JobStore
from app.notify.service import Notifier
from app.printer.events import Connected
from app.web.app import create_app
from app.web.deps import Services
from tests.factories import make_file, make_status
from tests.fakes import FakeCamera, FakePrinter, StatusHolder


@dataclass
class Web:
    client: TestClient
    clock: FakeClock
    holder: StatusHolder
    printer: FakePrinter
    bedcheck: BedCheck
    jobs: JobStore
    notifier: Notifier
    woken: list
    camera: FakeCamera

    def login(self) -> None:
        response = self.client.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert response.status_code == 303

    def event(self, event) -> None:
        asyncio.run(self.bedcheck.handle_event(event))

    def watch_printer(self) -> None:
        self.event(Connected(at=self.clock.now(), status=self.holder.status))


@pytest.fixture
def make_web(db, clock, tmp_path):
    def build(**settings_overrides) -> Web:
        holder = StatusHolder(make_status())
        printer = FakePrinter(files={"cube.gcode": make_file()})
        bedcheck = BedCheck(db, clock, printer, lambda: holder.status)
        bedcheck.startup()
        jobs = JobStore(db, clock)
        notifier = Notifier(db, clock, telegram_enabled=False)
        woken: list = []
        camera = FakeCamera()
        settings = Settings(printer_host="sim", app_password="secret", tz="Europe/Berlin",
                            data_dir=tmp_path, **settings_overrides)
        svc = Services(settings=settings, clock=clock, status=lambda: holder.status,
                       printer=printer, bedcheck=bedcheck, jobs=jobs, notifier=notifier,
                       wake_runner=lambda: woken.append(True), camera=camera)
        client = TestClient(create_app(svc, secret_key="test-secret"))
        return Web(client, clock, holder, printer, bedcheck, jobs, notifier, woken, camera)

    return build


@pytest.fixture
def web(make_web) -> Web:
    return make_web()
