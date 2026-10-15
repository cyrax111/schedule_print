"""G-code files on the printer: list, upload, thumbnails."""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from app.printer.client import PrinterError

from .deps import render, services

router = APIRouter()


async def _files_page(request: Request, upload_error: str | None = None,
                      status_code: int = 200) -> Response:
    svc = services(request)
    files, error = [], None
    try:
        for entry in await svc.printer.files():
            info = await svc.printer.file_info(entry.filename)
            if info is not None:
                files.append(info)
    except PrinterError as exc:
        error = f"Could not read files from the printer: {exc}"
    return render(request, "files.html",
                  {"files": files, "error": error, "upload_error": upload_error}, status_code)


@router.get("/files")
async def files_page(request: Request) -> Response:
    return await _files_page(request)


@router.post("/files/upload")
async def upload(request: Request, file: UploadFile) -> Response:
    name = PurePosixPath((file.filename or "").replace("\\", "/")).name
    if not name.lower().endswith(".gcode"):
        return await _files_page(request, "Choose a .gcode file.", 400)
    try:
        await services(request).printer.upload(name, await file.read())
    except PrinterError as exc:
        return await _files_page(request, f"Upload failed: {exc}", 502)
    return RedirectResponse("/files", status_code=303)


@router.get("/files/thumbnail")
async def thumbnail(request: Request, path: str) -> Response:
    if path.startswith("/") or ".." in path.split("/"):
        return Response(status_code=400)
    try:
        data = await services(request).printer.thumbnail(path)
    except PrinterError:
        return Response(status_code=502)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
