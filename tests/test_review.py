"""Phase 5 tests: review queue, answering (MCQ), stats."""

import re
from datetime import datetime, timedelta, timezone

from app.models import Chunk, Course, QuizItem, Resource, ReviewState, Topic, User


def _seed(Session):
    with Session() as s:
        user = User(email="s@x.edu")
        s.add(user)
        course = Course(source="moodle", source_id="c1", name="C")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t1", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                       type="file", title="R", status="extracted", extracted_text="t")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content="t", order=0)
        s.add(chunk)
        s.commit()
        mcq = QuizItem(chunk_id=chunk.id, question="Which?", question_type="mcq",
                       options=["a", "b", "c", "d"], correct_answer="1",
                       explanation="Because b.", difficulty="recall",
                       generation_key="g1")
        s.add(mcq)
        s.commit()
        s.refresh(mcq)
        return str(mcq.id)


def _token(client):
    page = client.get("/review/take")
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_review_flow(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)

    r = client.get("/review")
    assert r.status_code == 200 and "Which?" in r.text

    r = client.get("/review/take")
    assert r.status_code == 200 and 'name="answer"' in r.text

    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": _token(client)})
    assert r.status_code == 200 and "Correct" in r.text and "Because b." in r.text

    r = client.get("/review")
    assert "due" in r.text and "Which?" not in r.text  # answered, not due

    r = client.get("/stats")
    assert r.status_code == 200 and "100.0%" in r.text and "1 day" in r.text


def test_review_wrong_answer(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "0", "csrf_token": _token(client)})
    assert "Incorrect" in r.text


def test_stats_streak_and_empty(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    r = client.get("/stats")
    assert "—" in r.text  # no answers yet
    # streak via direct helper
    from app.stats import compute_stats
    with Session() as s:
        from sqlmodel import select
        u = s.exec(select(User)).first()
        assert compute_stats(s, u.id)["streak_days"] == 0
