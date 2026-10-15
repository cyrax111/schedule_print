"""Dashboard: printer status, bed confirmation, next job, latest notifications."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from app.bedcheck.service import ConfirmRefused, not_idle_reason
from app.notify.service import TEST
from app.printer.models import PrintState

from .deps import Services, render, services
from .progress import describe_progress, format_finish

router = APIRouter()


def bed_context(svc: Services, error: str | None = None) -> dict:
    status = svc.status()
    active = svc.bedcheck.active()
    blocked = None if status.online and status.idle else not_idle_reason(status)
    return {"active": active, "watching": svc.bedcheck.watching, "ended": None if active else svc.bedcheck.latest(),
            "blocked": blocked, "bed_error": error}


async def status_context(request: Request) -> dict:
    svc = services(request)
    status, now = svc.status(), svc.clock.now()
    estimates = request.app.state.estimates
    if status.state not in (PrintState.PRINTING, PrintState.PAUSED):
        estimates.forget()
        return {"status": status, "progress": None, "finish": None}
    view = describe_progress(status, await estimates.seconds(status.filename), now)
    finish = format_finish(view.finish_at, now, svc.tz) if view and view.finish_at else None
    return {"status": status, "progress": view, "finish": finish}


@router.get("/")
async def dashboard(request: Request) -> Response:
    svc = services(request)
    pending = svc.jobs.pending()
    return render(request, "dashboard.html", {
        **await status_context(request), **bed_context(svc),
        "next_job": pending[0] if pending else None,
        "notifications": svc.notifier.latest(5),
        "telegram_enabled": svc.settings.telegram_enabled,
    })


@router.get("/partials/status")
async def status_partial(request: Request) -> Response:
    return render(request, "_status.html", await status_context(request))


@router.get("/camera.jpg")
async def camera(request: Request) -> Response:
    """The camera picture, through the app so the page never talks to the printer itself."""
    cam = services(request).camera
    image = await cam.snapshot() if cam is not None else None
    if image is None:
        return PlainTextResponse("Camera not available.", status_code=503,
                                 headers={"Cache-Control": "no-store"})
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/partials/bed")
async def bed_partial(request: Request) -> Response:
    return render(request, "_bed.html", bed_context(services(request)))


@router.post("/bed/confirm")
async def confirm(request: Request) -> Response:
    svc = services(request)
    try:
        svc.bedcheck.confirm()
    except ConfirmRefused as exc:
        return render(request, "_bed.html", bed_context(svc, error=f"Not confirmed: {exc}."))
    return render(request, "_bed.html", bed_context(svc))


@router.post("/bed/withdraw")
async def withdraw(request: Request) -> Response:
    svc = services(request)
    svc.bedcheck.withdraw()
    return render(request, "_bed.html", bed_context(svc))


@router.post("/telegram/test")
async def telegram_test(request: Request) -> Response:
    svc = services(request)
    if not svc.settings.telegram_enabled:
        return PlainTextResponse("Telegram is not configured.", status_code=400)
    svc.notifier.notify(TEST, "Test message from Schedule Print")
    return RedirectResponse("/", status_code=303)
