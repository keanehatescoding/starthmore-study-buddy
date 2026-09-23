"""Phase 1.5 tests: course browser renders synced data (SQLite + TestClient)."""

from app.models import Course, Resource, Topic


def test_browser_flow(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(source="moodle", source_id="c1", name="CS 301", code="CS 301")
        s.add(course)
        s.commit()
        s.refresh(course)
        topic = Topic(course_id=course.id, source_id="t1", title="Trees", order=0)
        s.add(topic)
        s.commit()
        s.refresh(topic)
        res = Resource(
            topic_id=topic.id, source="moodle", source_id="r1",
            type="file", title="trees.pdf", status="pending",
        )
        s.add(res)
        s.commit()
        s.refresh(res)
        cid, rid = str(course.id), str(res.id)

    r = client.get("/")
    assert r.status_code == 200 and "CS 301" in r.text

    r = client.get(f"/courses/{cid}")
    assert r.status_code == 200 and "Trees" in r.text and "trees.pdf" in r.text

    r = client.get(f"/resources/{rid}")
    assert r.status_code == 200 and "Phase 2" in r.text

    assert client.get("/courses/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}
