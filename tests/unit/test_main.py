import asyncio
import logging
import time
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, configure_logging, create_app_from_runtime
from app.notify.service import CONFIRMATION_VOIDED, PRINT_FINISHED
from app.printer.events import Connected, PrintFinished, PrintStarted
from tests.factories import IDLE_OBJECTS, make_status
from tests.fakes import FakeWS, Router, connector, ok


def settings_for(tmp_path) -> Settings:
    return Settings(printer_host="printer", app_password="secret", tz="UTC", data_dir=tmp_path)


def test_logging_hides_request_urls():
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_printer_events_become_notifications(tmp_path, clock):
    rt = build_runtime(settings_for(tmp_path), clock=clock)
    rt.watcher.status = make_status()

    async def scenario():
        await rt.bus.publish(Connected(at=clock.now(), status=rt.watcher.status))
        rt.bedcheck.confirm()
        rt.jobs.create(filename="cube.gcode", file_size=1, file_modified=1.0,
                       scheduled_at=clock.now() + timedelta(hours=2), head_map=())
        await rt.bus.publish(PrintStarted(at=clock.now(), filename="other.gcode"))
        await rt.bus.publish(PrintFinished(at=clock.now(), filename="other.gcode", result="complete"))

    asyncio.run(scenario())
    kinds = [n.kind for n in reversed(rt.notifier.latest(10))]
    assert kinds == [CONFIRMATION_VOIDED, PRINT_FINISHED]
    rt.conn.close()


def test_no_voided_notification_when_no_job_is_pending(tmp_path, clock):
    rt = build_runtime(settings_for(tmp_path), clock=clock)
    rt.watcher.status = make_status()

    async def scenario():
        await rt.bus.publish(Connected(at=clock.now(), status=rt.watcher.status))
        rt.bedcheck.confirm()
        await rt.bus.publish(PrintStarted(at=clock.now(), filename="other.gcode"))

    asyncio.run(scenario())
    assert rt.bedcheck.active() is None  # the confirmation really was voided
    assert [n.kind for n in rt.notifier.latest(10)] == []
    rt.conn.close()


def test_app_starts_watches_and_stops(tmp_path):
    router = Router({("GET", "/server/files/list"): ok([])})
    rt = build_runtime(settings_for(tmp_path), printer_transport=httpx.MockTransport(router),
                       ws_connect=connector(FakeWS(IDLE_OBJECTS)))
    with TestClient(create_app_from_runtime(rt)) as client:
        deadline = time.monotonic() + 5
        while not client.get("/healthz").json()["printer_online"]:
            assert time.monotonic() < deadline, "watcher never connected"
            time.sleep(0.05)
        client.post("/login", data={"password": "secret"})
        assert "Head 1: PLA" in client.get("/").text
        assert client.get("/files").status_code == 200
    assert (tmp_path / "app.db").exists() and (tmp_path / "secret_key").exists()


def test_dead_background_task_is_logged_and_fails_health(tmp_path, caplog):
    router = Router({("GET", "/server/files/list"): ok([])})
    rt = build_runtime(settings_for(tmp_path), printer_transport=httpx.MockTransport(router),
                       ws_connect=connector(FakeWS(IDLE_OBJECTS)))

    async def crash():
        raise RuntimeError("db gone")

    rt.runner.run = crash
    with caplog.at_level(logging.CRITICAL), TestClient(create_app_from_runtime(rt)) as client:
        deadline = time.monotonic() + 5
        while "job-runner" not in rt.dead_tasks:
            assert time.monotonic() < deadline, "dead task never noticed"
            time.sleep(0.05)
        response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False and body["dead"] == ["job-runner"] and "printer_online" in body
    records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(records) == 1 and "job-runner" in records[0].getMessage()
    assert records[0].exc_info[1].args == ("db gone",)


def test_camera_is_wired_to_the_printer(tmp_path, clock):
    router = Router({("GET", "/server/files/camera/monitor.jpg"): httpx.Response(
        200, content=b"JPEG", headers={"content-type": "image/jpeg"})})
    ws = FakeWS(answers={"camera.start_monitor": {"state": "success"}})
    rt = build_runtime(settings_for(tmp_path), clock=clock,
                       printer_transport=httpx.MockTransport(router), ws_connect=connector(ws))
    assert asyncio.run(rt.camera.snapshot()) == b"JPEG"
    assert [m["method"] for m in ws.sent] == ["camera.start_monitor"]
    rt.conn.close()
