"""Phase 6 tests: batching, threshold, dedupe, delivery (fake sender)."""

import io
import urllib.error
import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.notify as notify
from app.models import Chunk, Course, NotificationEvent, QuizItem, Resource, Topic, User


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _course_with_items(s: Session, n_chunks=2, code="CS 301"):
    user = User(email="s@x.edu")
    s.add(user)
    s.commit()
    course = Course(user_id=user.id, source="moodle", source_id="c1",
                    name="Data Structures", code=code)
    s.add(course)
    s.commit()
    topic = Topic(course_id=course.id, source_id="t1", title="T")
    s.add(topic)
    s.commit()
    res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                   type="file", title="R", status="extracted", extracted_text="t")
    s.add(res)
    s.commit()
    for i in range(n_chunks):
        chunk = Chunk(resource_id=res.id, title=f"C{i}", content="t", order=i)
        s.add(chunk)
        s.commit()
        s.add(QuizItem(chunk_id=chunk.id, question=f"Q{i}?", question_type="mcq",
                       options=["a", "b", "c", "d"], correct_answer="0",
                       difficulty="recall", generation_key=f"g{i}"))
    s.commit()
    s.refresh(user)
    s.refresh(course)
    return user, course


def test_new_material_batched_per_course(session):
    user, course = _course_with_items(session)
    event = notify.enqueue_new_material(session, course.id, 5)
    assert event.type == "new_material"
    assert event.payload == {"course": "Data Structures", "code": "CS 301", "new_items": 5}
    assert event.user_id == user.id and event.sent is False
    assert notify.enqueue_new_material(session, course.id, 0) is None


def test_review_due_not_resent_hourly_after_delivery(session, monkeypatch):
    _fake_batches(monkeypatch)
    user, _ = _course_with_items(session, n_chunks=3)  # 3 due >= threshold
    first = notify.check_review_due(session, user.id, threshold=3)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 1
    # the hourly worker runs again with the same backlog: no new email
    assert notify.check_review_due(session, user.id, threshold=3) is None
    # after the cooldown the (still due) user is reminded again
    first.created_at = first.created_at - notify.REVIEW_DUE_COOLDOWN
    session.add(first)
    session.commit()
    again = notify.check_review_due(session, user.id, threshold=3)
    assert again is not None and again.id != first.id


def test_review_due_threshold_and_dedupe(session):
    user, _ = _course_with_items(session, n_chunks=2)  # 2 due < 3
    assert notify.check_review_due(session, user.id) is None
    # one more item in the same course -> 3 due
    chunk = session.exec(select(Chunk)).first()
    session.add(QuizItem(chunk_id=chunk.id, question="Q3?",
                         question_type="mcq", options=["a", "b", "c", "d"],
                         correct_answer="0", difficulty="recall",
                         generation_key="g-extra"))
    session.commit()
    event = notify.check_review_due(session, user.id, threshold=3)
    assert event is not None and event.payload["due_count"] >= 3
    assert notify.check_review_due(session, user.id, threshold=3) is None  # deduped


class _Calls(list):
    """Emails of each send_batch call; `.keys` holds the Idempotency-Keys."""

    def __init__(self):
        super().__init__()
        self.keys = []


def _fake_batches(monkeypatch, fail=lambda emails: None):
    """Record each send_batch call; `fail` may raise to simulate errors."""
    calls = _Calls()

    def fake(api_key, emails, idempotency_key, sleep=None):
        fail(emails)
        calls.append(emails)
        calls.keys.append(idempotency_key)

    monkeypatch.setattr(notify, "send_batch", fake)
    return calls


def test_send_marks_sent_and_renders(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    user, course = _course_with_items(session, n_chunks=1)
    notify.enqueue_new_material(session, course.id, 2)
    out = notify.send_pending(session, "key", "from@x", "to@x")
    assert out == {"sent": 1, "failed": 0, "errors": {}}
    [[email]] = calls
    assert email["subject"] == "New study material: CS 301"
    assert "2 new quiz items" in email["text"]
    assert email["to"] == ["s@x.edu"] and email["from"] == "from@x"
    event = session.exec(select(NotificationEvent)).one()
    assert event.sent is True and event.sent_at is not None


def test_send_failure_stays_queued(session, monkeypatch):
    def boom(emails):
        raise notify.EmailError("resend returned 500", "http_500")

    _fake_batches(monkeypatch, boom)
    user, course = _course_with_items(session, n_chunks=1)
    notify.enqueue_new_material(session, course.id, 2)
    out = notify.send_pending(session, "key", "from@x", "to@x")
    assert out == {"sent": 0, "failed": 1, "errors": {"http_500": 1}}
    assert session.exec(select(NotificationEvent)).one().sent is False


def test_send_goes_to_event_owner_not_fallback(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    other = User(email="other@x.edu")
    session.add(other)
    session.commit()
    session.refresh(other)
    owned = Course(user_id=other.id, source="moodle", source_id="owned",
                   name="Owned", code="OWN")
    session.add(owned)
    session.commit()
    session.refresh(owned)
    event = notify.enqueue_new_material(session, owned.id, 3)
    assert event.user_id == other.id
    out = notify.send_pending(session, "key", "from@x", "fallback@x")
    assert out == {"sent": 1, "failed": 0, "errors": {}}
    assert [e["to"] for e in calls[0]] == [["other@x.edu"]]


def test_send_without_recipient_stays_queued(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    # stale reference: user row gone, no fallback -> cannot deliver
    orphan = NotificationEvent(user_id=uuid.UUID(int=0),
                               type="review_due", payload={"due_count": 9})
    session.add(orphan)
    session.commit()
    out = notify.send_pending(session, "key", "from@x", "")
    assert out == {"sent": 0, "failed": 1, "errors": {"no_recipient": 1}}
    assert calls == []
    assert session.exec(select(NotificationEvent)).one().sent is False


def test_render_review_due():
    event = NotificationEvent(user_id="00000000-0000-0000-0000-000000000000",
                              type="review_due", payload={"due_count": 5})
    subject, body = notify.render(event, "https://sb.example.com/")
    assert subject == "5 reviews due" and "5 quiz items" in body
    assert "https://sb.example.com/review" in body and "localhost" not in body


def test_render_uses_app_base_url_setting(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "app_base_url", "https://prod.example.com")
    event = NotificationEvent(user_id=uuid.UUID(int=0), type="new_material",
                              payload={"course": "DS", "code": "CS", "new_items": 2})
    _, body = notify.render(event)
    assert body.endswith("Review them: https://prod.example.com/review")


def test_unowned_course_enqueues_nothing_and_creates_no_user(session):
    course = Course(source="moodle", source_id="orphan", name="Orphan", code="ORP")
    session.add(course)
    session.commit()
    assert notify.enqueue_new_material(session, course.id, 4) is None
    assert notify.enqueue_new_material(session, uuid.uuid4(), 4) is None  # missing
    assert session.exec(select(User)).all() == []
    assert session.exec(select(NotificationEvent)).all() == []


def test_review_due_counts_all_due_items(session, monkeypatch):
    from app import grade
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 100)
    user, _ = _course_with_items(session, n_chunks=25)  # > due_items' default page
    event = notify.check_review_due(session, user.id, threshold=3)
    assert event.payload == {"due_count": 25}


def test_review_due_respects_daily_new_cap(session):
    from app import grade
    user, _ = _course_with_items(session, n_chunks=25)
    event = notify.check_review_due(session, user.id, threshold=3)
    assert event.payload == {"due_count": grade.NEW_ITEMS_PER_DAY}


def _owned_events(session, n):
    user = User(email="u@x.edu")
    session.add(user)
    session.commit()
    for i in range(n):
        session.add(NotificationEvent(user_id=user.id, type="review_due",
                                      payload={"due_count": i}))
    session.commit()


def test_send_batches_requests(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    _owned_events(session, notify.BATCH_SIZE + 5)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": notify.BATCH_SIZE + 5, "failed": 0, "errors": {}}
    assert [len(c) for c in calls] == [notify.BATCH_SIZE, 5]
    assert all(e.sent for e in session.exec(select(NotificationEvent)))


def test_bad_email_in_batch_does_not_block_others(session, monkeypatch):
    def reject_bad(emails):
        if any(e["subject"] == "1 reviews due" for e in emails):
            raise notify.EmailError("resend returned 422", "http_422:validation_error")

    calls = _fake_batches(monkeypatch, reject_bad)
    _owned_events(session, 3)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 2, "failed": 1,
                   "errors": {"http_422:validation_error": 1}}
    assert [len(c) for c in calls] == [1, 1]  # the batch retried one by one
    unsent = session.exec(
        select(NotificationEvent).where(NotificationEvent.sent == False)  # noqa: E712
    ).all()
    assert [e.payload["due_count"] for e in unsent] == [1]


def test_rate_limit_stops_run_and_leaves_rest_queued(session, monkeypatch):
    def limited(emails):
        raise notify.RateLimitedError("429")

    _fake_batches(monkeypatch, limited)
    _owned_events(session, notify.BATCH_SIZE + 1)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": notify.BATCH_SIZE + 1,
                   "errors": {"rate_limited": notify.BATCH_SIZE + 1}}
    assert not any(e.sent for e in session.exec(select(NotificationEvent)))


def test_no_api_key_sends_nothing(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    _owned_events(session, 2)
    assert notify.send_pending(session, "", "from@x") == {
        "sent": 0, "failed": 2, "errors": {"no_api_key": 2}}
    assert calls == []


def _http_429(retry_after="1.5"):
    return urllib.error.HTTPError(
        notify.RESEND_BATCH_URL, 429, "Too Many Requests",
        {"Retry-After": retry_after}, io.BytesIO(b""),
    )


def _raise_on_urlopen(monkeypatch, err):
    def urlopen(req, timeout):
        raise err

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)


class _OK:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_batch_backs_off_on_429_then_succeeds(monkeypatch):
    replies = [_http_429(), _http_429("nope"), _OK()]

    def urlopen(req, timeout):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    slept = []
    notify.send_batch("key", [{"to": ["a@x"]}], "k", sleep=slept.append)
    assert slept == [1.5, 2.0] and replies == []  # Retry-After, then 2**attempt


def test_send_batch_gives_up_after_retries(monkeypatch):
    def urlopen(req, timeout):
        raise _http_429("0")

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    slept = []
    with pytest.raises(notify.RateLimitedError):
        notify.send_batch("key", [{"to": ["a@x"]}], "k", sleep=slept.append)
    assert len(slept) == notify.MAX_RETRIES


def test_retry_after_parses_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    wait = notify._retry_after(_http_429(format_datetime(when, usegmt=True)), 0)
    assert 25 <= wait <= 30
    past = format_datetime(when - timedelta(hours=1), usegmt=True)
    assert notify._retry_after(_http_429(past), 0) == 0.0


def test_send_batch_gives_up_when_retry_after_exceeds_cap(monkeypatch):
    _raise_on_urlopen(monkeypatch, _http_429(str(notify.MAX_RETRY_WAIT + 1)))
    slept = []
    with pytest.raises(notify.RateLimitedError) as exc:
        notify.send_batch("key", [{"to": ["a@x"]}], "k", sleep=slept.append)
    assert slept == [] and exc.value.reason == "rate_limited"


def test_send_batch_reason_uses_resend_error_name_not_message(monkeypatch):
    body = b'{"statusCode":422,"name":"validation_error","message":"bad a@x.edu"}'
    _raise_on_urlopen(monkeypatch, urllib.error.HTTPError(
        notify.RESEND_BATCH_URL, 422, "Unprocessable", {}, io.BytesIO(body)))
    with pytest.raises(notify.EmailError) as exc:
        notify.send_batch("key", [{"to": ["a@x.edu"]}], "k")
    assert exc.value.reason == "http_422:validation_error"
    assert "a@x.edu" not in str(exc.value)


def test_send_batch_reason_drops_unexpected_error_names(monkeypatch):
    body = b'{"name":"Bad <script> a@x.edu"}'
    _raise_on_urlopen(monkeypatch, urllib.error.HTTPError(
        notify.RESEND_BATCH_URL, 500, "Server Error", {}, io.BytesIO(body)))
    with pytest.raises(notify.EmailError) as exc:
        notify.send_batch("key", [{"to": ["a@x"]}], "k")
    assert exc.value.reason == "http_500"


def test_single_send_uses_stable_event_key(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    _owned_events(session, 1)
    notify.send_pending(session, "key", "from@x")
    event = session.exec(select(NotificationEvent)).one()
    assert calls.keys == [notify._idempotency_key([(event, calls[0][0])])]
    assert event.batch_key is None


def test_ambiguous_batch_failure_resends_same_batch_same_key(session, monkeypatch):
    keys = []

    def lost(emails):
        raise notify.EmailError("send failed: timed out", "network:TimeoutError")

    calls = _fake_batches(monkeypatch, lost)
    real_key = notify._idempotency_key
    monkeypatch.setattr(notify, "_idempotency_key", lambda b: keys.append(real_key(b)) or keys[-1])
    _owned_events(session, 3)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": 3, "errors": {"network:TimeoutError": 3}}
    assert len(calls.keys) == 0  # fake raised before recording
    events = session.exec(select(NotificationEvent)).all()
    [key] = {e.batch_key for e in events}
    assert key and key.startswith("batch-")  # persisted, not split into singles
    [first_key] = keys

    # a newer event arrives; the next pass resends the old batch unchanged
    session.add(NotificationEvent(user_id=events[0].user_id, type="review_due",
                                  payload={"due_count": 99}))
    session.commit()
    calls = _fake_batches(monkeypatch)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 4, "failed": 0, "errors": {}}
    assert calls.keys[0] == first_key  # same events, same body -> same key
    assert calls.keys[1] != first_key
    assert [len(c) for c in calls] == [3, 1]


def _newest(session):
    return session.exec(
        select(NotificationEvent).order_by(NotificationEvent.created_at.desc())
    ).first()


def test_server_error_does_not_split_batch(session, monkeypatch):
    def boom(emails):
        raise notify.EmailError("resend returned http_500", "http_500")

    attempts = []
    _fake_batches(monkeypatch, lambda emails: (attempts.append(len(emails)), boom(emails)))
    _owned_events(session, 3)
    out = notify.send_pending(session, "key", "from@x")
    assert out["errors"] == {"http_500": 3} and attempts == [3]  # no singles


def test_validation_split_clears_batch_key(session, monkeypatch):
    def reject_batches(emails):
        if len(emails) > 1:
            raise notify.EmailError("422", notify.BATCH_REJECTED)

    calls = _fake_batches(monkeypatch, reject_batches)
    _owned_events(session, 2)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 2
    events = session.exec(select(NotificationEvent)).all()
    assert all(e.batch_key is None for e in events)
    assert len(set(calls.keys)) == 2  # one key per single send


def test_send_batch_sends_idempotency_key(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append(req.get_header("Idempotency-key"))
        return _OK()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    notify.send_batch("key", [{"to": ["a@x"]}], "batch-abc")
    assert seen == ["batch-abc"]


def test_changed_batch_body_gets_new_idempotency_key(session, monkeypatch):
    """A kept batch resent with a different body must not reuse the old key
    (Resend answers 409 for 24h); an unchanged resend must reuse it."""
    def lost(emails):
        raise notify.EmailError("send failed: timed out", "network:TimeoutError")

    keys = []
    real_key = notify._idempotency_key
    monkeypatch.setattr(notify, "_idempotency_key",
                        lambda b: keys.append(real_key(b)) or keys[-1])
    _fake_batches(monkeypatch, lost)
    _owned_events(session, 2)
    notify.send_pending(session, "key", "from@x", base_url="https://a.example")
    notify.send_pending(session, "key", "from@x", base_url="https://a.example")
    notify.send_pending(session, "key", "from@x", base_url="https://b.example")
    assert keys[0] == keys[1] and keys[2] != keys[0]
    assert len({e.batch_key for e in session.exec(select(NotificationEvent))}) == 1


def test_identical_emails_for_different_events_get_different_keys():
    body = {"from": "f@x", "to": ["a@x"], "subject": "s", "text": "t"}
    one = NotificationEvent(user_id=uuid.UUID(int=0), type="review_due", payload={})
    two = NotificationEvent(user_id=uuid.UUID(int=0), type="review_due", payload={})
    assert notify._idempotency_key([(one, body)]) != notify._idempotency_key([(two, body)])
