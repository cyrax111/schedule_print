"""Full notification feed."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from .deps import render, services

router = APIRouter()


@router.get("/notifications")
async def notifications_page(request: Request) -> Response:
    return render(request, "notifications.html",
                  {"notifications": services(request).notifier.latest(200)})


@router.post("/notifications/read-all")
async def read_all(request: Request) -> Response:
    services(request).notifier.mark_all_read()
    return RedirectResponse("/notifications", status_code=303)
