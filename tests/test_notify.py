"""Phase 6 tests: batching, threshold, dedupe, delivery (fake sender)."""

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
    course = Course(source="moodle", source_id="c1", name="Data Structures", code=code)
    s.add_all([user, course])
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


def test_send_marks_sent_and_renders(session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        notify, "send_email",
        lambda api, frm, to, subj, body: sent.append((subj, body)),
    )
    user, course = _course_with_items(session, n_chunks=1)
    notify.enqueue_new_material(session, course.id, 2)
    out = notify.send_pending(session, "key", "from@x", "to@x")
    assert out == {"sent": 1, "failed": 0}
    assert sent[0][0] == "New study material: CS 301"
    assert "2 new quiz items" in sent[0][1]
    event = session.exec(select(NotificationEvent)).one()
    assert event.sent is True and event.sent_at is not None


def test_send_failure_stays_queued(session, monkeypatch):
    def boom(api, frm, to, subj, body):
        raise notify.EmailError("no key")

    monkeypatch.setattr(notify, "send_email", boom)
    user, course = _course_with_items(session, n_chunks=1)
    notify.enqueue_new_material(session, course.id, 2)
    out = notify.send_pending(session, "", "from@x", "to@x")
    assert out == {"sent": 0, "failed": 1}
    assert session.exec(select(NotificationEvent)).one().sent is False


def test_send_goes_to_event_owner_not_fallback(session, monkeypatch):
    seen = []
    monkeypatch.setattr(
        notify, "send_email",
        lambda api, frm, to, subj, body: seen.append(to),
    )
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
    assert out == {"sent": 1, "failed": 0}
    assert seen == ["other@x.edu"]


def test_send_without_recipient_stays_queued(session, monkeypatch):
    monkeypatch.setattr(
        notify, "send_email",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    # stale reference: user row gone, no fallback -> cannot deliver
    orphan = NotificationEvent(user_id=uuid.UUID(int=0),
                               type="review_due", payload={"due_count": 9})
    session.add(orphan)
    session.commit()
    out = notify.send_pending(session, "key", "from@x", "")
    assert out == {"sent": 0, "failed": 1}
    assert session.exec(select(NotificationEvent)).one().sent is False


def test_render_review_due():
    event = NotificationEvent(user_id="00000000-0000-0000-0000-000000000000",
                              type="review_due", payload={"due_count": 5})
    subject, body = notify.render(event)
    assert subject == "5 reviews due" and "5 quiz items" in body
