"""One shared password, a signed session cookie, a login lockout and an origin check."""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from app.clock import Clock

from .deps import render, services

router = APIRouter()
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz")


class LoginLimiter:
    """Locks a client out for a while after too many wrong passwords."""

    def __init__(self, clock: Clock, max_failures: int = 5,
                 lockout: timedelta = timedelta(seconds=60)) -> None:
        self._clock = clock
        self._max = max_failures
        self._lockout = lockout
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, datetime] = {}

    def locked(self, client: str) -> bool:
        until = self._locked_until.get(client)
        return until is not None and self._clock.now() < until

    def failure(self, client: str) -> None:
        count = self._failures.get(client, 0) + 1
        if count >= self._max:
            self._locked_until[client] = self._clock.now() + self._lockout
            count = 0
        self._failures[client] = count

    def success(self, client: str) -> None:
        self._failures.pop(client, None)
        self._locked_until.pop(client, None)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


async def require_login(request: Request, call_next) -> Response:
    if request.method == "POST" and not _same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    if request.url.path.startswith(PUBLIC_PREFIXES) or request.session.get("auth"):
        return await call_next(request)
    if request.method == "GET" and "hx-request" not in request.headers:
        return RedirectResponse("/login", status_code=303)
    return PlainTextResponse("Login required.", status_code=401, headers={"HX-Redirect": "/login"})


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
async def login_form(request: Request) -> Response:
    return render(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, password: str = Form("")) -> Response:
    limiter: LoginLimiter = request.app.state.limiter
    client = _client(request)
    if limiter.locked(client):
        return render(request, "login.html",
                      {"error": "Too many wrong passwords. Try again in a minute."}, 429)
    expected = services(request).settings.app_password
    if password and hmac.compare_digest(password.encode(), expected.encode()):
        limiter.success(client)
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    limiter.failure(client)
    return render(request, "login.html", {"error": "Wrong password."}, 401)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
