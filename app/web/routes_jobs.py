"""Scheduling, changing and cancelling jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from app.jobs.heads import HeadChoiceError, build_assignments, suggest_heads
from app.jobs.models import HeadAssignment, JobStatus
from app.jobs.store import JobNotEditable
from app.printer.client import PrinterError
from app.printer.models import FileInfo
from app.timeutil import (
    LocalTimeError, format_local, format_local_input, local_to_utc, parse_local_input,
)

from .deps import Services, render, services

router = APIRouter()
NOT_EDITABLE = "This job already ran or was cancelled, so it can't be changed."
OFFLINE = ("The printer is offline, so the loaded filament can't be checked. "
           "Try again when it is online.")


@dataclass
class Checked:
    when: datetime | None = None
    head_map: tuple[HeadAssignment, ...] = ()
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _message(request: Request, text: str, status_code: int) -> Response:
    return render(request, "message.html", {"message": text}, status_code)


def _choice(form: FormData, info: FileInfo) -> dict[int, int]:
    choice = {}
    for colour in info.colours:
        raw = str(form.get(f"head_{colour.logical}", ""))
        if raw.isascii() and raw.isdigit():
            choice[colour.logical] = int(raw)
    return choice


async def _load_file(svc: Services, filename: str) -> FileInfo | str:
    """The file's info, or an error message for the user."""
    try:
        info = await svc.printer.file_info(filename)
    except PrinterError as exc:
        return f"Could not read the file from the printer: {exc}"
    return info if info is not None else f"{filename} is not on the printer."


async def _overlap_warnings(svc: Services, when: datetime, job_id: int | None) -> list[str]:
    previous = svc.jobs.previous_pending(when, exclude_id=job_id)
    if previous is None:
        return []
    try:
        info = await svc.printer.file_info(previous.filename)
    except PrinterError:
        return []
    if info is None or not info.estimated_seconds:
        return []
    if previous.scheduled_at + timedelta(seconds=info.estimated_seconds) <= when:
        return []
    return [f"{previous.filename} starts at {format_local(previous.scheduled_at, svc.tz)} and may "
            "still be printing then. This job will be skipped unless you clear the bed and "
            "confirm it again in between."]


async def _check(svc: Services, form: FormData, info: FileInfo, job_id: int | None = None) -> Checked:
    checked = Checked()
    try:
        checked.when = local_to_utc(parse_local_input(str(form.get("scheduled_local", ""))), svc.tz)
        if checked.when <= svc.clock.now():
            checked.errors.append("That time is in the past.")
    except LocalTimeError as exc:
        checked.errors.append(str(exc))
    except OverflowError:
        checked.errors.append("Not a valid date and time.")
    status = svc.status()
    if not status.online:
        checked.errors.append(OFFLINE)
    else:
        try:
            checked.head_map = build_assignments(_choice(form, info), info.colours, status.heads)
        except HeadChoiceError as exc:
            text = str(exc)
            checked.errors.append(text[:1].upper() + text[1:].rstrip(".") + ".")
    if not checked.errors and checked.when is not None:
        checked.warnings = await _overlap_warnings(svc, checked.when, job_id)
    return checked


def _form(request: Request, *, info: FileInfo, action: str, title: str, scheduled_local: str,
          choice: dict[int, int], errors=(), warnings=(), status_code: int = 200) -> Response:
    svc = services(request)
    return render(request, "job_form.html", {
        "file": info, "action": action, "title": title, "scheduled_local": scheduled_local,
        "choice": choice, "heads": svc.status().heads, "errors": list(errors),
        "warnings": list(warnings), "tz_name": svc.settings.tz,
    }, status_code)


def _needs_another_look(form: FormData, checked: Checked) -> bool:
    return bool(checked.errors) or (bool(checked.warnings) and form.get("confirm_overlap") != "1")


def _rerender(request: Request, form: FormData, info: FileInfo, checked: Checked,
              action: str, title: str) -> Response:
    return _form(request, info=info, action=action, title=title,
                 scheduled_local=str(form.get("scheduled_local", "")), choice=_choice(form, info),
                 errors=checked.errors, warnings=checked.warnings,
                 status_code=400 if checked.errors else 200)


@router.get("/jobs")
async def jobs_page(request: Request) -> Response:
    svc = services(request)
    return render(request, "jobs.html", {"upcoming": svc.jobs.pending(), "finished": svc.jobs.finished(50)})


@router.get("/jobs/new")
async def new_job(request: Request, file: str = "") -> Response:
    svc = services(request)
    info = await _load_file(svc, file)
    if isinstance(info, str):
        return _message(request, info, 404)
    status = svc.status()
    return _form(request, info=info, action="/jobs", title="Schedule a print", scheduled_local="",
                 choice=suggest_heads(info.colours, status.heads),
                 errors=[] if status.online else [OFFLINE])


@router.post("/jobs")
async def create_job(request: Request) -> Response:
    svc = services(request)
    form = await request.form()
    info = await _load_file(svc, str(form.get("filename", "")))
    if isinstance(info, str):
        return _message(request, info, 400)
    checked = await _check(svc, form, info)
    if _needs_another_look(form, checked):
        return _rerender(request, form, info, checked, "/jobs", "Schedule a print")
    svc.jobs.create(filename=info.filename, file_size=info.size, file_modified=info.modified,
                    scheduled_at=checked.when, head_map=checked.head_map)
    svc.wake_runner()
    return RedirectResponse("/jobs", status_code=303)


@router.get("/jobs/{job_id}/edit")
async def edit_job(request: Request, job_id: int) -> Response:
    svc = services(request)
    job = svc.jobs.get(job_id)
    if job is None or job.status is not JobStatus.PENDING:
        return _message(request, NOT_EDITABLE, 409)
    info = await _load_file(svc, job.filename)
    if isinstance(info, str):
        return _message(request, info, 404)
    return _form(request, info=info, action=f"/jobs/{job_id}", title="Change a scheduled print",
                 scheduled_local=format_local_input(job.scheduled_at, svc.tz),
                 choice={a.logical: a.head for a in job.head_map})


@router.post("/jobs/{job_id}")
async def update_job(request: Request, job_id: int) -> Response:
    svc = services(request)
    job = svc.jobs.get(job_id)
    if job is None or job.status is not JobStatus.PENDING:
        return _message(request, NOT_EDITABLE, 409)
    form = await request.form()
    info = await _load_file(svc, job.filename)  # the job's own file, whatever the form says
    if isinstance(info, str):
        return _message(request, info, 400)
    checked = await _check(svc, form, info, job_id=job_id)
    if _needs_another_look(form, checked):
        return _rerender(request, form, info, checked, f"/jobs/{job_id}", "Change a scheduled print")
    try:
        svc.jobs.update(job_id, scheduled_at=checked.when, head_map=checked.head_map,
                        file_size=info.size, file_modified=info.modified)
    except JobNotEditable:
        return _message(request, NOT_EDITABLE, 409)
    svc.wake_runner()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int) -> Response:
    svc = services(request)
    try:
        svc.jobs.cancel(job_id)
    except JobNotEditable:
        return _message(request, NOT_EDITABLE, 409)
    svc.wake_runner()
    return RedirectResponse("/jobs", status_code=303)
