import json
import logging
from datetime import timedelta

import httpx
import pytest

from app.notify.service import Notifier
from app.notify.telegram import RETRY_DELAYS, DeliveryWorker, TelegramError, TelegramSender

TOKEN = "123456:SECRET-token"


def sender_with(handler) -> TelegramSender:
    return TelegramSender(TOKEN, "42", transport=httpx.MockTransport(handler))


class FlakySender:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        if self.failures:
            self.failures -= 1
            raise TelegramError("HTTP 502: <redacted>")
        self.sent.append(text)


async def test_sends_plain_text_to_the_chat():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    await sender_with(handler).send("hello")
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(seen[0].content) == {
        "chat_id": "42", "text": "hello", "disable_web_page_preview": True}


async def test_errors_never_contain_the_token():
    def refuse(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(TelegramError) as caught:
        await sender_with(refuse).send("hello")
    assert TOKEN not in str(caught.value) and "<redacted>" in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_api_error_is_reported():
    def reject(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramError, match="HTTP 400: Bad Request: chat not found"):
        await sender_with(reject).send("x")


async def test_worker_delivers_with_link(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    sender = FlakySender(0)
    worker = DeliveryWorker(notifier, sender, clock, app_base_url="http://pi.local:8000")
    notifier.notify("a", "Skipped cube.gcode")
    assert await worker.deliver_due() == 1
    assert sender.sent == ["Skipped cube.gcode\nhttp://pi.local:8000"]
    assert notifier.latest()[0].telegram_status == "sent"


@pytest.mark.parametrize("response", [
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json="oops"),
])
async def test_unexpected_body_is_a_failed_attempt_with_backoff(db, clock, response):
    notifier = Notifier(db, clock, telegram_enabled=True)
    worker = DeliveryWorker(notifier, sender_with(lambda request: response), clock)
    notifier.notify("a", "x")
    assert await worker.deliver_due() == 0
    row = notifier.latest()[0]
    assert (row.telegram_status, row.telegram_attempts) == ("pending", 1)
    assert row.telegram_next_at == clock.now() + timedelta(seconds=RETRY_DELAYS[0])
    assert row.telegram_error == "HTTP 200: unexpected answer from Telegram"


class ExplodingSender:
    """Raises a non-Telegram error that carries the token on its first call, then delivers."""

    secret = TOKEN

    def __init__(self) -> None:
        self.calls = 0
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError(f"boom {TOKEN}")
        self.sent.append(text)


async def test_unexpected_error_backs_off_without_stalling_the_batch(db, clock, caplog):
    caplog.set_level(logging.DEBUG)
    notifier = Notifier(db, clock, telegram_enabled=True)
    sender = ExplodingSender()
    worker = DeliveryWorker(notifier, sender, clock)
    notifier.notify("a", "first")
    notifier.notify("a", "second")
    assert await worker.deliver_due() == 1
    first, second = notifier.latest(limit=2)[::-1]
    assert (first.telegram_status, first.telegram_attempts) == ("pending", 1)
    assert first.telegram_error == "boom <redacted>"
    assert second.telegram_status == "sent"
    assert sender.sent == ["second"]
    assert TOKEN not in caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


async def test_worker_retry_schedule_then_failed(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    worker = DeliveryWorker(notifier, FlakySender(100), clock)
    notifier.notify("a", "x")
    for delay in RETRY_DELAYS:
        await worker.deliver_due()
        row = notifier.latest()[0]
        assert row.telegram_status == "pending"
        assert row.telegram_next_at == clock.now() + timedelta(seconds=delay)
        clock.advance(delay)
    await worker.deliver_due()
    row = notifier.latest()[0]
    assert (row.telegram_status, row.telegram_attempts) == ("failed", 6)
    assert "<redacted>" in row.telegram_error


async def test_pending_rows_resume_after_restart(db, clock):
    Notifier(db, clock, telegram_enabled=True).notify("a", "queued before restart")
    sender = FlakySender(0)
    await DeliveryWorker(Notifier(db, clock, telegram_enabled=True), sender, clock).deliver_due()
    assert sender.sent == ["queued before restart"]
