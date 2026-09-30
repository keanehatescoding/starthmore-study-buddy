"""Phase 5 tests: review queue, answering (MCQ), stats."""

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Chunk, Course, QuizItem, Resource, ReviewState, Topic, User


def _seed(Session):
    with Session() as s:
        user = User(email="s@x.edu")
        s.add(user)
        # owned by the signed-in fixture user (test@x.edu); unowned courses are hidden
        owner = s.exec(select(User).where(User.email == "test@x.edu")).one()
        course = Course(user_id=owner.id, source="moodle", source_id="c1", name="C")
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
                    data={"answer": "1", "csrf_token": _token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/{item_id}/result"
    r = client.get(r.headers["location"])
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


def _other_users_item(Session):
    with Session() as s:
        other = User(email="o@x.edu")
        s.add(other)
        s.commit()
        course = Course(user_id=other.id, source="moodle", source_id="c2", name="Theirs")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t2", title="T2")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r2",
                       type="file", title="R2")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch2", content="t", order=0)
        s.add(chunk)
        s.commit()
        item = QuizItem(chunk_id=chunk.id, question="Theirs?", question_type="mcq",
                        options=["a", "b"], correct_answer="0", generation_key="g9")
        s.add(item)
        s.commit()
        s.refresh(item)
        return str(item.id)


def _short(Session):
    with Session() as s:
        from sqlmodel import select
        chunk = s.exec(select(Chunk)).first()
        item = QuizItem(chunk_id=chunk.id, question="Explain?", question_type="short_answer",
                        correct_answer="because", grading_criteria="says because",
                        generation_key="g2")
        s.add(item)
        s.commit()
        s.refresh(item)
        return str(item.id)


def test_stats_items_total_scoped(testapp):
    Session = testapp["Session"]
    _seed(Session)
    _other_users_item(Session)
    from app.stats import compute_stats
    with Session() as s:
        assert compute_stats(s, testapp["user_id"])["items_total"] == 1


def test_cannot_answer_other_users_item(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    theirs = _other_users_item(Session)
    r = client.post(f"/review/{theirs}/answer",
                    data={"answer": "0", "csrf_token": _token(client)})
    assert r.status_code == 404


def test_invalid_mcq_answer_rejected(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "9", "csrf_token": _token(client)})
    assert r.status_code == 400 and "Pick one of the listed options" in r.text
    with Session() as s:
        from sqlmodel import select
        assert s.exec(select(ReviewState)).first() is None


def test_grader_failure_keeps_answer(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    from app.llm import LLMError

    class Down:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            raise LLMError("upstream 502")

    monkeypatch.setattr(main, "LLMClient", Down)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "because <reasons>", "csrf_token": _token(client)})
    assert r.status_code == 503
    assert "grader is unavailable" in r.text
    assert "because &lt;reasons&gt;</textarea>" in r.text
    with Session() as s:
        from sqlmodel import select
        assert s.exec(select(ReviewState)).first() is None


def test_grader_misconfigured_keeps_answer(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    monkeypatch.setattr(main.settings, "llm_api_key", "")
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "my answer", "csrf_token": _token(client)})
    assert r.status_code == 503 and "my answer</textarea>" in r.text


def test_replayed_answer_not_regraded(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    token = _token(client)
    client.post(f"/review/{item_id}/answer", data={"answer": "1", "csrf_token": token})
    # back + resubmit with a different answer: shows the recorded result
    r = client.post(f"/review/{item_id}/answer", data={"answer": "0", "csrf_token": token})
    assert r.status_code == 200 and "Correct" in r.text
    with Session() as s:
        state = s.exec(select(ReviewState)).one()
        assert state.repetitions == 1 and state.lapses == 0


def test_result_page_needs_a_result(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.get(f"/review/{item_id}/result", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/take"


def test_long_short_answer_rejected(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    calls = []

    class Counting:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            calls.append(a)
            return {"correct": True}

    monkeypatch.setattr(main, "LLMClient", Counting)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "x" * 4001, "csrf_token": _token(client)})
    assert calls == []  # rejected before grading
    assert r.status_code == 400 and "limited to 4,000 characters" in r.text
    assert 'maxlength="4000"' in r.text


def test_grader_gets_short_timeout(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    seen = {}

    class Fast:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def complete_json(self, *a, **k):
            return {"correct": True, "feedback": "Nice."}

    monkeypatch.setattr(main, "LLMClient", Fast)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "because", "csrf_token": _token(client)})
    assert "Correct" in r.text and "Nice." in r.text
    assert seen["timeout"] <= 30 and seen["max_attempts"] <= 2


def test_long_feedback_survives_the_redirect(testapp, monkeypatch):
    # feedback lives in ReviewState, not the cookie-backed session
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    feedback = "🙂 Great answer. " * 300  # ~5k chars, ~50k once JSON-escaped

    class Chatty:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            return {"correct": True, "partial_credit": 1.0, "feedback": feedback}

    monkeypatch.setattr(main, "LLMClient", Chatty)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "because", "csrf_token": _token(client)})
    assert r.status_code == 200 and "Correct" in r.text
    assert feedback.strip() in r.text
    assert len(client.cookies.get("session", "")) < 4000


def test_new_item_past_cap_redirects_to_queue(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    from app import grade
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 0)
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/settings/moodle").text)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": token.group(1)}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(r.headers["location"], follow_redirects=False).headers["location"] \
        == "/review/take"
    with Session() as s:
        assert s.exec(select(ReviewState)).first() is None
