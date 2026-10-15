import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.printer.client import HISTORY_LOOKBACK, PrinterClient, PrinterError, PrinterOffline
from app.printer.models import FileColour
from tests.factories import IDLE_OBJECTS
from tests.fakes import Router, ok


def client_for(routes) -> tuple[PrinterClient, Router]:
    router = Router(routes)
    return PrinterClient("http://printer:7125", transport=httpx.MockTransport(router)), router


def http_date(moment: datetime | None = None) -> str:
    """A Date header value, as the printer's web server stamps it (whole seconds, GMT)."""
    return format_datetime(moment or datetime.now(timezone.utc), usegmt=True)


def unreachable(exc_type) -> PrinterClient:
    def refuse(request):
        raise exc_type("no route to printer", request=request)
    return PrinterClient("http://printer:7125", transport=httpx.MockTransport(refuse))


async def test_status_queries_subscribed_objects():
    client, router = client_for({("GET", "/printer/objects/query"): ok({"status": IDLE_OBJECTS})})
    status = await client.status()
    assert status.idle and len(status.heads) == 4
    query = router.requests[0].url.query.decode()
    assert "print_stats=state,filename" in query and "exception_manager" in query


async def test_status_is_offline_when_unreachable():
    assert (await unreachable(httpx.ConnectError).status()).online is False


async def test_files_lists_only_gcode_newest_first():
    client, _ = client_for({("GET", "/server/files/list"): ok([
        {"path": "old.gcode", "size": 10, "modified": 1.0},
        {"path": "notes.txt", "size": 5, "modified": 3.0},
        {"path": "plates/new.gcode", "size": 20, "modified": 2.0},
    ])})
    assert [f.filename for f in await client.files()] == ["plates/new.gcode", "old.gcode"]


async def test_file_info_from_metadata_needs_no_tail():
    meta = {"size": 10, "modified": 5.0, "filament_colors": ["#FF0000"],
            "filament_type": "PLA", "referenced_tools": [0]}
    client, router = client_for({("GET", "/server/files/metadata"): ok(meta)})
    info = await client.file_info("cube.gcode")
    assert info.colours == (FileColour(0, "FF0000", "PLA"),)
    assert [r.url.path for r in router.requests] == ["/server/files/metadata"]


async def test_file_info_reads_gcode_tail_when_metadata_lacks_colours():  # Review Focus: names
    tail = (b"; filament used [mm] = 10.0, 0.0\n"
            b"; filament_colour = #00FF00;#0000FF\n"
            b"; filament_type = PETG;PLA\n")
    client, router = client_for({
        ("GET", "/server/files/metadata"): ok({"size": 10, "modified": 5.0}),
        ("GET", "/server/files/gcodes/plates/globe light.gcode"): httpx.Response(206, content=tail),
    })
    info = await client.file_info("plates/globe light.gcode")
    assert info.colours == (FileColour(0, "00FF00", "PETG"),)
    assert router.requests[0].url.params["filename"] == "plates/globe light.gcode"
    tail_request = router.requests[1]
    assert tail_request.url.raw_path == b"/server/files/gcodes/plates/globe%20light.gcode"
    assert tail_request.headers["range"] == "bytes=-131072"
    await client.file_info("plates/globe light.gcode")
    assert len(router.requests) == 3  # second call: metadata only, tail cached


async def test_file_info_after_a_failed_tail_fetch_is_not_cached():
    tail = {"fail": True}

    def gcode(request):
        if tail["fail"]:
            return httpx.Response(500, text="busy")
        return httpx.Response(206, content=b"; filament_colour = #00FF00\n; filament_type = PETG\n")

    client, router = client_for({
        ("GET", "/server/files/metadata"): ok({"size": 10, "modified": 5.0}),
        ("GET", "/server/files/gcodes/cube.gcode"): gcode,
    })
    assert (await client.file_info("cube.gcode")).colours == ()
    tail["fail"] = False
    assert (await client.file_info("cube.gcode")).colours == (FileColour(0, "00FF00", "PETG"),)
    assert [r.url.path for r in router.requests] == ["/server/files/metadata",
                                                     "/server/files/gcodes/cube.gcode"] * 2


async def test_file_info_missing_file_is_none():
    client, _ = client_for({})
    assert await client.file_info("gone.gcode") is None


async def test_upload_never_starts_the_print():
    seen = {}

    def upload(request):
        seen["body"] = request.content
        return httpx.Response(201, json={"item": {"path": "a.gcode"}, "print_started": False})

    client, _ = client_for({("POST", "/server/files/upload"): upload})
    await client.upload("a.gcode", b"G28\n")
    assert b'name="print"\r\n\r\nfalse' in seen["body"]
    assert b'name="root"\r\n\r\ngcodes' in seen["body"]


async def test_upload_that_started_a_print_is_an_error():
    client, _ = client_for({("POST", "/server/files/upload"):
                            httpx.Response(201, json={"print_started": True})})
    with pytest.raises(PrinterError, match="started"):
        await client.upload("a.gcode", b"G28\n")


async def test_apply_head_map_checks_readback():
    scripts = []

    def gcode(request):
        scripts.append(json.loads(request.content)["script"])
        return ok("ok")

    table = {"status": {"print_task_config": {"extruder_map_table": [2] + [0] * 31}}}
    client, _ = client_for({
        ("POST", "/printer/gcode/script"): gcode,
        ("GET", "/printer/objects/query"): ok(table),
    })
    await client.apply_head_map([(0, 2)])
    assert scripts == ["SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=0 MAP_EXTRUDER=2"]
    with pytest.raises(PrinterError, match="did not take effect"):
        await client.apply_head_map([(0, 1)])


async def test_start_print_sends_filename():
    client, router = client_for({("POST", "/printer/print/start"): ok("ok")})
    await client.start_print("plates/a b.gcode")
    assert router.requests[0].url.params["filename"] == "plates/a b.gcode"


async def test_server_error_page_becomes_printer_error():  # Review Focus: printer errors
    client, _ = client_for({("POST", "/printer/print/start"):
                            httpx.Response(500, text="<html>oops</html>")})
    with pytest.raises(PrinterError, match="HTTP 500"):
        await client.start_print("a.gcode")


async def test_unreachable_printer_raises_offline():
    with pytest.raises(PrinterOffline):
        await unreachable(httpx.ConnectTimeout).start_print("a.gcode")


def history_reply(jobs, *, printer_behind=timedelta(0), date=True):
    """Handler acting like the fork: filters by `since` and stamps times with its own clock."""
    def reply(request):
        printer_now = datetime.now(timezone.utc) - printer_behind
        since = float(request.url.params["since"])
        stamped = [{**job, "start_time": (job["start_time"] - printer_behind).timestamp()}
                   for job in jobs]
        kept = [job for job in stamped if job["start_time"] > since]
        headers = {"Date": http_date(printer_now)} if date else {}
        return httpx.Response(200, json={"result": {"count": len(kept), "jobs": kept}},
                              headers=headers)
    return reply


async def test_history_parses_or_returns_none():
    started = datetime(2025, 10, 9, 8, tzinfo=timezone.utc)
    client, router = client_for({("GET", "/server/history/list"): history_reply(
        [{"filename": "a.gcode", "start_time": started, "status": "completed"}])})
    since = datetime(2025, 10, 9, tzinfo=timezone.utc)
    jobs = await client.history(since)
    assert [j.filename for j in jobs] == ["a.gcode"]
    assert abs(jobs[0].start_time - started) <= timedelta(seconds=2)
    assert float(router.requests[0].url.params["since"]) == (since - HISTORY_LOOKBACK).timestamp()
    missing, _ = client_for({})
    assert await missing.history(since) is None
    assert await unreachable(httpx.ConnectError).history(since) is None


async def test_history_corrects_for_a_printer_clock_behind_ours():
    now = datetime.now(timezone.utc)
    since, started = now - timedelta(minutes=5), now - timedelta(minutes=3)  # started in the gap
    client, _ = client_for({("GET", "/server/history/list"): history_reply(
        [{"filename": "touch.gcode", "start_time": started, "status": "completed"}],
        printer_behind=timedelta(minutes=10))})
    jobs = await client.history(since)
    assert [j.filename for j in jobs] == ["touch.gcode"]
    assert since <= jobs[0].start_time <= datetime.now(timezone.utc)
    assert abs(jobs[0].start_time - started) <= timedelta(seconds=2)


async def test_history_leaves_out_jobs_before_since():
    now = datetime.now(timezone.utc)
    client, _ = client_for({("GET", "/server/history/list"): history_reply(
        [{"filename": "old.gcode", "start_time": now - timedelta(hours=3), "status": "completed"},
         {"filename": "new.gcode", "start_time": now - timedelta(minutes=3), "status": "completed"}],
        printer_behind=timedelta(minutes=10))})
    jobs = await client.history(now - timedelta(minutes=5))
    assert [j.filename for j in jobs] == ["new.gcode"]


@pytest.mark.parametrize("headers", [{}, {"Date": "yesterday"}, {"Date": ""}])
async def test_history_without_a_readable_date_is_unavailable(headers):
    def reply(request):
        return httpx.Response(200, json={"result": {"count": 0, "jobs": []}}, headers=headers)

    client, _ = client_for({("GET", "/server/history/list"): reply})
    assert await client.history(datetime.now(timezone.utc) - timedelta(hours=1)) is None


async def test_history_with_a_printer_clock_days_off_is_unavailable():
    client, _ = client_for({("GET", "/server/history/list"): history_reply(
        [], printer_behind=timedelta(days=2))})
    assert await client.history(datetime.now(timezone.utc) - timedelta(hours=1)) is None


async def test_history_asks_newest_first():
    client, router = client_for({("GET", "/server/history/list"): history_reply([])})
    assert await client.history(datetime.now(timezone.utc) - timedelta(hours=1)) == []
    assert router.requests[0].url.params["order"] == "desc"


@pytest.mark.parametrize("result", [{}, {"jobs": None}, {"count": 3}])
async def test_history_without_a_jobs_list_is_unavailable(result):  # not "no prints"
    def reply(request):
        return httpx.Response(200, json={"result": result}, headers={"Date": http_date()})

    client, _ = client_for({("GET", "/server/history/list"): reply})
    assert await client.history(datetime.now(timezone.utc) - timedelta(hours=1)) is None


async def test_html_answer_becomes_printer_error():
    client, _ = client_for({("GET", "/server/files/metadata"):
                            httpx.Response(200, text="<html>oops</html>")})
    with pytest.raises(PrinterError) as excinfo:
        await client.file_info("a.gcode")
    assert not isinstance(excinfo.value, PrinterOffline)


@pytest.mark.parametrize("result", ["nope", [1, 2]])
async def test_odd_shape_becomes_printer_error(result):
    client, _ = client_for({("GET", "/server/files/list"): ok(result)})
    with pytest.raises(PrinterError):
        await client.files()


async def test_bad_content_encoding_becomes_printer_error():
    def bad(request):  # a streamed body, so the decode error surfaces while the client reads it
        return httpx.Response(200, stream=httpx.ByteStream(b"not gzip"),
                              headers={"content-encoding": "gzip"})

    client, _ = client_for({("GET", "/server/files/list"): bad,
                            ("GET", "/server/history/list"): bad})
    with pytest.raises(PrinterError) as excinfo:
        await client.files()
    assert not isinstance(excinfo.value, PrinterOffline)
    assert await client.history(datetime(2025, 10, 9, tzinfo=timezone.utc)) is None


async def test_overflowing_number_becomes_printer_error():
    body = '{"result": [{"path": "a.gcode", "size": 1e999, "modified": 1}]}'
    client, _ = client_for({("GET", "/server/files/list"): httpx.Response(200, text=body)})
    with pytest.raises(PrinterError):
        await client.files()


async def test_camera_snapshot_returns_the_picture():
    client, router = client_for({("GET", "/server/files/camera/monitor.jpg"): httpx.Response(
        200, content=b"\xff\xd8JPEG", headers={"content-type": "image/jpeg"})})
    assert await client.camera_snapshot() == b"\xff\xd8JPEG"


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"error": {"message": "Not Found"}}),  # camera asleep
    httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
])
async def test_camera_snapshot_without_a_picture_is_none(response):
    client, _ = client_for({("GET", "/server/files/camera/monitor.jpg"): response})
    assert await client.camera_snapshot() is None


async def test_camera_snapshot_unreachable_raises_offline():
    with pytest.raises(PrinterOffline):
        await unreachable(httpx.ConnectError).camera_snapshot()
