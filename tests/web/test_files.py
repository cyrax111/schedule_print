from app.printer.client import PrinterOffline
from app.printer.models import FileColour
from tests.factories import make_file


def test_files_page_lists_files_with_colours_and_encoded_links(web):  # Review Focus: names
    name = "plates/globe light.gcode"
    web.printer.files_by_name[name] = make_file(
        name, colours=(FileColour(0, "FF0000", "PLA"),), thumbnail="plates/.thumbs/g.png")
    web.login()
    page = web.client.get("/files").text
    assert name in page
    assert 'href="/jobs/new?file=plates/globe%20light.gcode"' in page
    assert "/files/thumbnail?path=plates/.thumbs/g.png" in page
    assert "background:#FF0000" in page


def test_printer_offline_shows_message(web):  # Review Focus: printer errors
    web.printer.fail_on["files"] = PrinterOffline("printer unreachable: ConnectError")
    web.login()
    response = web.client.get("/files")
    assert response.status_code == 200
    assert "Could not read files from the printer: printer unreachable" in response.text


def test_upload_strips_folders_and_redirects(web):
    web.login()
    response = web.client.post("/files/upload", files={"file": ("../../evil/part.gcode", b"G28\n")},
                               follow_redirects=False)
    assert response.status_code == 303
    assert web.printer.uploaded == {"part.gcode": b"G28\n"}


def test_upload_rejects_other_files(web):
    web.login()
    response = web.client.post("/files/upload", files={"file": ("photo.jpg", b"x")})
    assert response.status_code == 400 and "Choose a .gcode file." in response.text
    assert web.printer.uploaded == {}


def test_thumbnail_proxy_and_traversal(web):
    web.login()
    assert web.client.get("/files/thumbnail", params={"path": "a/.thumbs/x.png"}).content == b"PNG"
    assert web.client.get("/files/thumbnail", params={"path": "../secret"}).status_code == 400
