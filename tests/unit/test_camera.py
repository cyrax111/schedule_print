import pytest

from app.printer.camera import WAKE_REQUEST, Camera, CameraError, wake_over_websocket
from app.printer.client import PrinterOffline
from tests.fakes import FakeWS, connector


class Printer:
    """The two things Camera needs: a wake call and a snapshot read."""

    def __init__(self, images=(b"JPEG",)) -> None:
        self.images = list(images)   # each read takes the next; the last one repeats
        self.wakes = 0
        self.reads = 0
        self.wake_error: Exception | None = None
        self.read_error: Exception | None = None

    async def wake(self) -> None:
        self.wakes += 1
        if self.wake_error:
            raise self.wake_error

    async def read(self) -> bytes | None:
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return self.images.pop(0) if len(self.images) > 1 else self.images[0]


def camera_for(printer: Printer, clock) -> Camera:
    return Camera(printer.wake, printer.read, clock)


async def test_first_snapshot_wakes_the_camera(clock):
    printer = Printer()
    assert await camera_for(printer, clock).snapshot() == b"JPEG"
    assert (printer.wakes, printer.reads) == (1, 1)


async def test_wakes_again_only_every_8_seconds(clock):
    printer = Printer()
    camera = camera_for(printer, clock)
    await camera.snapshot()
    clock.advance(7.9)
    await camera.snapshot()
    assert printer.wakes == 1
    clock.advance(0.1)
    await camera.snapshot()
    assert printer.wakes == 2


async def test_waits_for_a_camera_that_was_asleep(clock):
    printer = Printer(images=[None, None, b"JPEG"])
    start = clock.now()
    assert await camera_for(printer, clock).snapshot() == b"JPEG"
    assert printer.reads == 3 and (clock.now() - start).total_seconds() == 2.0


async def test_gives_up_on_a_camera_that_does_not_wake(clock):
    printer = Printer(images=[None])
    assert await camera_for(printer, clock).snapshot() is None
    assert printer.reads == 4  # the first read and three more, a second apart


async def test_no_waiting_when_the_camera_was_already_awake(clock):
    printer = Printer(images=[b"JPEG", None])
    camera = camera_for(printer, clock)
    await camera.snapshot()
    assert await camera.snapshot() is None
    assert printer.reads == 2


async def test_failed_wake_still_reads_and_is_not_retried_at_once(clock):
    printer = Printer()
    printer.wake_error = CameraError("no reply")
    camera = camera_for(printer, clock)
    assert await camera.snapshot() == b"JPEG"
    await camera.snapshot()
    assert printer.wakes == 1


async def test_unreachable_printer_gives_no_picture(clock):
    printer = Printer()
    printer.wake_error = OSError("no route to host")
    printer.read_error = PrinterOffline("printer unreachable: ConnectError")
    assert await camera_for(printer, clock).snapshot() is None


async def test_wake_sends_start_monitor_and_checks_the_reply():
    ws = FakeWS(answers={"camera.start_monitor": {"state": "success",
                                                  "url": "/files/camera/monitor.jpg"}})
    await wake_over_websocket(connector(ws), "ws://printer/websocket")
    assert ws.sent == [{**WAKE_REQUEST, "id": 1}]
    assert WAKE_REQUEST["params"] == {"domain": "lan", "interval": 0}


@pytest.mark.parametrize("answer", [{"state": "failed"}, None])
async def test_wake_refused_is_an_error(answer):
    ws = FakeWS(answers={"camera.start_monitor": answer})
    with pytest.raises(CameraError):
        await wake_over_websocket(connector(ws), "ws://printer/websocket")


async def test_wake_error_reply_is_an_error():
    ws = FakeWS(errors={"camera.start_monitor": {"code": -32601, "message": "Method not found"}})
    with pytest.raises(CameraError, match="Method not found"):
        await wake_over_websocket(connector(ws), "ws://printer/websocket")


async def test_wake_without_a_reply_times_out():
    with pytest.raises(CameraError, match="no reply"):
        await wake_over_websocket(connector(FakeWS()), "ws://printer/websocket", timeout=0.05)
