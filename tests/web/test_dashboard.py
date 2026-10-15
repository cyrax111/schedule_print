from app.printer.events import Disconnected, PrintStarted
from dataclasses import replace

from app.printer.models import PrinterStatus, PrintState
from tests.factories import make_file, printing_status


def test_dashboard_shows_printer_and_heads(web):
    web.login()
    page = web.client.get("/").text
    assert "Standby" in page and "Head 3: PETG" in page and "Head 4: empty" in page


def test_status_fragment_when_offline(web):
    web.holder.status = PrinterStatus.offline()
    web.login()
    assert "Printer offline" in web.client.get("/partials/status").text


def test_confirm_and_withdraw(web):
    web.watch_printer()
    web.login()
    confirmed = web.client.post("/bed/confirm").text
    assert "Confirmed empty and clean at Wed 14 Oct 2026 20:00 (UTC+02:00)" in confirmed
    withdrawn = web.client.post("/bed/withdraw").text
    assert "Last confirmation ended: withdrawn" in withdrawn


def test_confirm_refused_while_printing(web):
    web.watch_printer()
    web.holder.status = printing_status()
    web.login()
    panel = web.client.post("/bed/confirm").text
    assert "printer is printing" in panel and "disabled" in panel


def test_bed_panel_shows_why_confirmation_ended(web):
    web.watch_printer()
    web.login()
    web.client.post("/bed/confirm")
    web.event(PrintStarted(at=web.clock.now(), filename="x.gcode"))
    assert "printed since confirmation" in web.client.get("/partials/bed").text


def test_telegram_test_message_only_when_configured(make_web):
    plain = make_web()
    plain.login()
    assert "Send Telegram test message" not in plain.client.get("/").text
    assert plain.client.post("/telegram/test").status_code == 400
    telegram = make_web(telegram_bot_token="t", telegram_chat_id="1")
    telegram.login()
    assert "Send Telegram test message" in telegram.client.get("/").text
    telegram.client.post("/telegram/test")
    assert telegram.notifier.latest()[0].text == "Test message from Schedule Print"


def test_notifications_page_and_unread_badge(web):
    web.notifier.notify("job_skipped", "Skipped cube.gcode")
    web.login()
    page = web.client.get("/notifications").text
    assert "Skipped cube.gcode" in page and 'class="badge"' in page
    web.client.post("/notifications/read-all")
    assert 'class="badge"' not in web.client.get("/notifications").text


def test_bed_panel_shows_valid_while_watched(web):
    web.watch_printer()
    web.login()
    web.client.post("/bed/confirm")
    panel = web.client.get("/partials/bed").text
    assert "· valid" in panel and "can't verify" not in panel


def test_bed_panel_cannot_verify_when_not_watched(web):
    web.watch_printer()
    web.login()
    web.client.post("/bed/confirm")
    web.event(Disconnected(at=web.clock.now()))
    panel = web.client.get("/partials/bed").text
    assert "can't verify while the printer isn't watched" in panel and "· valid" not in panel


def printing(**overrides) -> PrinterStatus:
    values = dict(progress=0.5, print_seconds=840.0, layer=12, total_layers=37)
    values.update(overrides)
    return replace(printing_status(), **values)


def test_status_shows_progress_layers_and_finish_time(web):
    web.holder.status = printing()   # cube.gcode: the slicer estimated 1 h
    web.login()
    panel = web.client.get("/partials/status").text
    assert '<progress value="50" max="100">' in panel and "50%" in panel
    assert "Layer 12 of 37" in panel
    assert "Printing for 14 min · about 46 min left · done around 20:46" in panel


def test_paused_print_shows_no_finish_time(web):
    web.holder.status = printing(state=PrintState.PAUSED, paused=True)
    web.login()
    panel = web.client.get("/partials/status").text
    assert "about 46 min left" in panel and "done around" not in panel


def test_progress_without_layers_or_estimate(web):
    web.printer.files_by_name["cube.gcode"] = make_file(estimated_seconds=None)
    web.holder.status = printing(layer=None, total_layers=None)
    web.login()
    panel = web.client.get("/partials/status").text
    assert "Layer" not in panel and "about 14 min left" in panel


def test_last_minute_and_too_early_to_tell(web):
    web.login()
    web.holder.status = printing(print_seconds=3570.0, progress=0.99)
    assert "under a minute left" in web.client.get("/partials/status").text
    web.holder.status = printing(filename="other.gcode", progress=0.01, print_seconds=20.0)
    panel = web.client.get("/partials/status").text  # other.gcode has no estimate
    assert "Printing for under a minute" in panel and "left" not in panel


def test_idle_printer_shows_no_progress(web):
    web.login()
    assert "<progress" not in web.client.get("/partials/status").text


def test_camera_is_in_the_printer_panel_and_off_until_switched_on(web):
    web.login()
    page = web.client.get("/").text
    assert 'id="camera-toggle"' in page and "Camera on" in page
    assert 'src="/camera.jpg' not in page   # the browser loads it only once switched on
    assert web.camera.snapshots == 0
    assert 'id="camera-toggle"' not in web.client.get("/partials/status").text  # not reset by polling


def test_camera_picture(web):
    web.login()
    response = web.client.get("/camera.jpg")
    assert response.status_code == 200 and response.content == b"\xff\xd8JPEG"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"


def test_camera_not_available(web):
    web.camera.image = None
    web.login()
    response = web.client.get("/camera.jpg")
    assert response.status_code == 503 and "Camera not available" in response.text


def test_camera_needs_login(web):
    response = web.client.get("/camera.jpg", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert web.camera.snapshots == 0
