"""FastAPI application factory."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.printer.models import Temperature
from app.timeutil import format_local

from . import auth, routes_dashboard, routes_files, routes_jobs, routes_notifications
from .deps import Services, services
from .progress import FileEstimates, format_span

WEB_DIR = Path(__file__).parent


def _common_context(request: Request) -> dict[str, Any]:
    logged_in = bool(request.session.get("auth"))
    return {"logged_in": logged_in,
            "unread": services(request).notifier.unread_count() if logged_in else 0}


def _temp(t: Temperature) -> str:
    if t.current is None:
        return "–"
    text = f"{t.current:.0f}°C"
    return f"{text} → {t.target:.0f}°C" if t.target else text


def _duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _templates(svc: Services) -> Jinja2Templates:
    templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[_common_context])
    templates.env.globals["temp"] = _temp
    templates.env.filters["local"] = lambda moment: format_local(moment, svc.tz) if moment else ""
    templates.env.filters["duration"] = _duration
    templates.env.filters["span"] = format_span
    return templates


def create_app(svc: Services, *, secret_key: str,
               lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None) -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    app.state.templates = _templates(svc)
    app.state.limiter = auth.LoginLimiter(svc.clock)
    app.state.estimates = FileEstimates(svc.printer)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.middleware("http")(auth.require_login)
    # Added last so it wraps everything: require_login reads the session.
    app.add_middleware(SessionMiddleware, secret_key=secret_key, session_cookie="sp_session",
                       same_site="strict", https_only=False, max_age=30 * 24 * 3600)
    app.include_router(auth.router)
    app.include_router(routes_dashboard.router)
    app.include_router(routes_notifications.router)
    app.include_router(routes_files.router)
    app.include_router(routes_jobs.router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        dead = sorted(svc.dead_tasks())
        body = {"ok": not dead, "printer_online": svc.status().online, "dead": dead}
        return JSONResponse(body, status_code=503 if dead else 200)

    return app
