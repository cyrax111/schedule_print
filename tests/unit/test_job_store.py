from datetime import timedelta

import pytest

from app.jobs.models import HeadAssignment, JobStatus
from app.jobs.store import JobNotEditable, JobStore

HEADS = (HeadAssignment(logical=0, head=2, expected_type="PETG", expected_rgb="FF0000"),)


@pytest.fixture
def store(db, clock):
    return JobStore(db, clock)


def add(store, clock, minutes, filename="cube.gcode"):
    return store.create(filename=filename, file_size=1000, file_modified=1.5,
                        scheduled_at=clock.now() + timedelta(minutes=minutes), head_map=HEADS)


def test_create_and_read_back(store, clock):
    job = add(store, clock, 60)
    assert store.get(job.id) == job
    assert job.status is JobStatus.PENDING and job.head_map == HEADS
    assert job.scheduled_at == clock.now() + timedelta(minutes=60)


def test_pending_and_due_are_time_ordered(store, clock):
    add(store, clock, 60, "late.gcode")
    early = add(store, clock, 10, "early.gcode")
    assert [j.filename for j in store.pending()] == ["early.gcode", "late.gcode"]
    clock.advance(timedelta(minutes=30))
    assert [j.id for j in store.due(clock.now())] == [early.id]


def test_update_resets_reminder_and_only_for_pending(store, clock):  # Review Focus: finished job
    job = add(store, clock, 60)
    store.mark_reminder_sent(job.id)
    moved = store.update(job.id, scheduled_at=clock.now() + timedelta(hours=3), head_map=HEADS,
                         file_size=1000, file_modified=1.5)
    assert moved.reminder_sent is False
    store.set_status(job.id, JobStatus.SKIPPED, "no confirmation")
    with pytest.raises(JobNotEditable):
        store.update(job.id, scheduled_at=clock.now(), head_map=HEADS, file_size=1, file_modified=1.0)
    with pytest.raises(JobNotEditable):
        store.cancel(job.id)


def test_terminal_status_records_decision_time(store, clock):
    job = add(store, clock, 0)
    store.set_status(job.id, JobStatus.STARTING)
    assert store.get(job.id).decided_at is None
    store.set_status(job.id, JobStatus.FAILED, "interrupted")
    done = store.get(job.id)
    assert (done.status, done.outcome_reason, done.decided_at) == (
        JobStatus.FAILED, "interrupted", clock.now())
    assert store.in_status(JobStatus.FAILED) == [done]
    assert store.finished() == [done] and store.count_pending() == 0


def test_cancel_pending(store, clock):
    job = add(store, clock, 60)
    store.cancel(job.id)
    assert store.get(job.id).status is JobStatus.CANCELLED


def test_previous_pending(store, clock):
    a = add(store, clock, 60, "a.gcode")
    b = add(store, clock, 120, "b.gcode")
    assert store.previous_pending(clock.now() + timedelta(minutes=90)).id == a.id
    assert store.previous_pending(clock.now() + timedelta(minutes=130), exclude_id=b.id).id == a.id
    assert store.previous_pending(clock.now()) is None


def test_claim_only_from_pending_at_same_time(store, clock):
    job = add(store, clock, 0)
    assert store.claim(job.id, job.scheduled_at) is True
    assert store.get(job.id).status is JobStatus.STARTING
    assert store.claim(job.id, job.scheduled_at) is False

    cancelled = add(store, clock, 0)
    store.cancel(cancelled.id)
    assert store.claim(cancelled.id, cancelled.scheduled_at) is False

    edited = add(store, clock, 0)
    assert store.claim(edited.id, edited.scheduled_at + timedelta(minutes=5)) is False
    assert store.get(edited.id).status is JobStatus.PENDING


def test_set_status_leaves_finished_jobs_alone(store, clock):
    job = add(store, clock, 60)
    store.cancel(job.id)
    decided = store.get(job.id).decided_at
    clock.advance(timedelta(minutes=5))
    assert store.set_status(job.id, JobStatus.SKIPPED, "x") is False
    after = store.get(job.id)
    assert after.status is JobStatus.CANCELLED and after.decided_at == decided
