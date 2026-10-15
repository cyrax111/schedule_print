"""Send-only Telegram delivery with retries. The bot token never leaves this module."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol

import httpx

from app.clock import Clock

from .service import Notifier

log = logging.getLogger(__name__)

RETRY_DELAYS = (10, 30, 60, 300, 900)  # seconds before retries 1-5; then the row is failed


class TelegramError(Exception):
    """Delivery failed. The message never contains the bot token."""


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "<redacted>") if secret else text


class Sender(Protocol):
    async def send(self, text: str) -> None: ...


class TelegramSender:
    def __init__(self, token: str, chat_id: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str = "https://api.telegram.org", timeout: float = 15.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)

    @property
    def secret(self) -> str:
        """The bot token, so the delivery worker can redact errors from any sender."""
        return self._token

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> None:
        error: str | None = None
        try:
            error = await self._post(text)
        except Exception as exc:  # any failure, including httpx.InvalidURL, carries the URL
            error = redact(f"{exc.__class__.__name__}: {exc}", self._token)
        # Raised outside the except block so no exception context (which may hold the URL) is kept.
        if error is not None:
            raise TelegramError(error)

    async def _post(self, text: str) -> str | None:
        """Return None when Telegram accepted the message, otherwise a redacted error text."""
        response = await self._http.post(
            f"/bot{self._token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict) and body.get("ok"):
            return None
        if isinstance(body, dict):
            detail = body.get("description") or response.text[:200]
        else:
            detail = "unexpected answer from Telegram"
        return redact(f"HTTP {response.status_code}: {detail}", self._token)


class DeliveryWorker:
    def __init__(self, notifier: Notifier, sender: Sender, clock: Clock, *,
                 app_base_url: str | None = None, poll_seconds: float = 5.0) -> None:
        self._notifier = notifier
        self._sender = sender
        self._clock = clock
        self._base_url = app_base_url
        self._poll_seconds = poll_seconds
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def deliver_due(self) -> int:
        sent = 0
        for item in self._notifier.due_for_telegram(self._clock.now()):
            text = f"{item.text}\n{self._base_url}" if self._base_url else item.text
            try:
                await self._sender.send(text)
            except Exception as exc:  # any failure is this row's failed attempt; never stall the batch
                attempt = item.telegram_attempts + 1
                next_at = (self._clock.now() + timedelta(seconds=RETRY_DELAYS[attempt - 1])
                           if attempt <= len(RETRY_DELAYS) else None)
                # Senders other than TelegramSender may not expose a secret; they must not put one in errors.
                error = redact(str(exc), getattr(self._sender, "secret", None))
                self._notifier.telegram_failed(item.id, error, next_at)
                log.warning("Telegram delivery failed (attempt %d): %s", attempt, error)
            else:
                self._notifier.telegram_sent(item.id)
                sent += 1
        return sent

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.deliver_due()
            except Exception:
                log.exception("Telegram delivery loop failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass
