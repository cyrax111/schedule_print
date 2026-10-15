import logging
from datetime import timedelta

from app.notify.service import JOB_SKIPPED, Notifier


def test_notify_records_with_telegram_off(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=False)
    notifier.notify(JOB_SKIPPED, "Skipped", job_id=3)
    [row] = notifier.latest()
    assert (row.kind, row.text, row.job_id, row.read, row.telegram_status) == (
        JOB_SKIPPED, "Skipped", 3, False, "off")
    assert notifier.due_for_telegram(clock.now()) == []


def test_notify_queues_for_telegram_and_wakes_listener(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    woken = []
    notifier.add_listener(lambda: woken.append(1))
    notifier.notify("test", "hello")
    assert [n.text for n in notifier.due_for_telegram(clock.now())] == ["hello"]
    assert woken == [1]


def test_unread_and_mark_all_read(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=False)
    notifier.notify("a", "one")
    notifier.notify("b", "two")
    assert notifier.unread_count() == 2
    assert [n.text for n in notifier.latest()] == ["two", "one"]
    notifier.mark_all_read()
    assert notifier.unread_count() == 0


def test_notify_never_raises(db, clock, caplog):
    notifier = Notifier(db, clock, telegram_enabled=False)
    db.close()
    with caplog.at_level(logging.ERROR):
        notifier.notify("a", "still fine")  # logged, not raised
    assert any("could not record" in record.getMessage() for record in caplog.records)


def test_failed_attempts_reschedule_then_give_up(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    notifier.notify("a", "x")
    [row] = notifier.due_for_telegram(clock.now())
    later = clock.now() + timedelta(seconds=10)
    notifier.telegram_failed(row.id, "HTTP 502", next_at=later)
    assert notifier.due_for_telegram(clock.now()) == []
    [again] = notifier.due_for_telegram(later)
    assert (again.telegram_attempts, again.telegram_error) == (1, "HTTP 502")
    notifier.telegram_failed(row.id, "HTTP 502", next_at=None)
    assert notifier.latest()[0].telegram_status == "failed"


def test_sent_rows_are_not_due_again(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    notifier.notify("a", "x")
    [row] = notifier.due_for_telegram(clock.now())
    notifier.telegram_sent(row.id)
    assert notifier.due_for_telegram(clock.now()) == []
    assert notifier.latest()[0].telegram_status == "sent"
