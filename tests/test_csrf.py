"""CSRF tests: answer POSTs require the per-session token."""

import re

from app.models import Chunk, Course, QuizItem, Resource, Topic


def _seed_due_item(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        user_id = testapp["user_id"]
        course = Course(user_id=user_id, source="moodle", source_id="c1",
                        name="CS", code="CS")
        s.add(course)
        s.commit()
        s.refresh(course)
        topic = Topic(course_id=course.id, source_id="t1", title="T", order=0)
        s.add(topic)
        s.commit()
        s.refresh(topic)
        res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                       type="file", title="R", status="extracted",
                       extracted_text="t")
        s.add(res)
        s.commit()
        s.refresh(res)
        chunk = Chunk(resource_id=res.id, title="C", content="t", order=0)
        s.add(chunk)
        s.commit()
        s.refresh(chunk)
        item = QuizItem(chunk_id=chunk.id, question="Q?", question_type="mcq",
                        options=["a", "b", "c", "d"], correct_answer="1",
                        difficulty="recall", generation_key="g-csrf")
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = str(item.id)
    return client, item_id


def test_answer_requires_csrf_token(testapp):
    client, item_id = _seed_due_item(testapp)
    # no token at all -> 403
    r = client.post(f"/review/{item_id}/answer", data={"answer": "1"})
    assert r.status_code == 403
    # wrong token -> 403
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": "bogus"})
    assert r.status_code == 403


def test_answer_with_valid_token_succeeds(testapp):
    client, item_id = _seed_due_item(testapp)
    page = client.get("/review/take")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": token})
    assert r.status_code == 200 and "Correct" in r.text
