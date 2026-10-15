import dataclasses
from datetime import datetime, timezone

import pytest

from app.jobs.models import HeadAssignment, JobStatus
from app.printer.models import PrinterStatus
from tests.factories import make_file


def form(**overrides) -> dict:
    values = {"filename": "cube.gcode", "scheduled_local": "2026-10-15T02:00", "head_0": "0"}
    values.update(overrides)
    return values


def test_new_job_form_preselects_suggested_head(web):
    web.login()
    page = web.client.get("/jobs/new", params={"file": "cube.gcode"})
    assert page.status_code == 200
    assert '<option value="0" selected' in page.text  # white PLA file → white PLA head
    assert "Times are in Europe/Berlin" in page.text


def test_create_job_stores_utc_and_wakes_runner(web):
    web.login()
    response = web.client.post("/jobs", data=form(), follow_redirects=False)
    assert response.status_code == 303
    [job] = web.jobs.pending()
    assert job.scheduled_at == datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    assert job.head_map == (HeadAssignment(0, 0, "PLA", "FFFFFF"),)
    assert web.woken


@pytest.mark.parametrize("value, message", [
    ("2026-10-14T19:00", "That time is in the past."),
    ("", "Pick a date and time."),
    ("2027-03-28T02:30", "does not exist"),
])
def test_bad_times_are_form_errors(web, value, message):  # Review Focus: bad schedule input
    web.login()
    response = web.client.post("/jobs", data=form(scheduled_local=value))
    assert response.status_code == 400 and message in response.text
    assert web.jobs.pending() == []


def test_empty_head_is_rejected(web):
    web.login()
    response = web.client.post("/jobs", data=form(head_0="3"))
    assert response.status_code == 400 and "Head 4 is empty." in response.text


def test_offline_printer_blocks_scheduling(web):  # Review Focus: printer errors
    web.holder.status = PrinterStatus.offline()
    web.login()
    response = web.client.post("/jobs", data=form())
    assert response.status_code == 400 and "printer is offline" in response.text


def test_overlap_warning_then_schedule_anyway(web):
    web.login()
    web.client.post("/jobs", data=form(scheduled_local="2026-10-15T01:30"))
    warned = web.client.post("/jobs", data=form())  # 30 min later; the file takes an hour
    assert "may still be printing then" in warned.text and len(web.jobs.pending()) == 1
    web.client.post("/jobs", data=form(confirm_overlap="1"))
    assert len(web.jobs.pending()) == 2


def test_edit_and_cancel(web):
    web.login()
    web.client.post("/jobs", data=form())
    [job] = web.jobs.pending()
    assert 'value="2026-10-15T02:00"' in web.client.get(f"/jobs/{job.id}/edit").text
    web.client.post(f"/jobs/{job.id}", data=form(scheduled_local="2026-10-15T03:00"))
    assert web.jobs.get(job.id).scheduled_at == datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)
    web.client.post(f"/jobs/{job.id}/cancel")
    assert web.jobs.get(job.id).status is JobStatus.CANCELLED


def test_acting_on_finished_job_is_refused(web):  # Review Focus: finished job
    web.login()
    web.client.post("/jobs", data=form())
    [job] = web.jobs.pending()
    web.jobs.set_status(job.id, JobStatus.STARTED)
    assert web.client.get(f"/jobs/{job.id}/edit").status_code == 409
    assert web.client.post(f"/jobs/{job.id}", data=form()).status_code == 409
    assert web.client.post(f"/jobs/{job.id}/cancel").status_code == 409
    assert web.jobs.get(job.id).status is JobStatus.STARTED


def test_jobs_page_escapes_file_names(web):  # Review Focus: names
    name = "<b>bold</b>.gcode"
    web.printer.files_by_name[name] = make_file(name)
    web.login()
    web.client.post("/jobs", data=form(filename=name))
    page = web.client.get("/jobs").text
    assert "&lt;b&gt;bold&lt;/b&gt;.gcode" in page and "<b>bold</b>" not in page


def test_history_shows_decided_time(web):
    web.login()
    web.client.post("/jobs", data=form())
    [job] = web.jobs.pending()
    web.jobs.set_status(job.id, JobStatus.SKIPPED, "bed not confirmed")
    decided = web.jobs.get(job.id).decided_at
    assert decided is not None
    from app.timeutil import format_local
    page = web.client.get("/jobs").text
    assert "skipped: bed not confirmed" in page
    assert format_local(decided, web.client.app.state.services.tz) in page


@pytest.mark.parametrize("overrides", [{"head_0": "²"}, {"scheduled_local": "0001-01-01T00:00"}])
def test_odd_input_is_a_form_error_not_a_crash(web, overrides):
    web.login()
    response = web.client.post("/jobs", data=form(**overrides))
    assert response.status_code == 400
    assert web.jobs.pending() == []


def test_file_without_colours_is_a_form_error(web):
    web.printer.files_by_name["cube.gcode"] = make_file(colours=())
    web.login()
    response = web.client.post("/jobs", data=form())
    assert response.status_code == 400 and "this file has no colour information" in response.text.lower()
    assert web.jobs.pending() == []


def test_unidentified_head_filament_is_a_form_error(web):
    from app.printer.models import HeadFilament
    status = web.holder.status
    heads = (HeadFilament(0, True),) + tuple(status.heads[1:])
    web.holder.status = dataclasses.replace(status, heads=heads)
    web.login()
    response = web.client.post("/jobs", data=form())
    assert response.status_code == 400 and "filament not identified" in response.text
    assert web.jobs.pending() == []
