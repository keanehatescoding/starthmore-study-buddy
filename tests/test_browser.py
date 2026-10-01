"""Phase 1.5 tests: course browser renders synced data (SQLite + TestClient)."""

from app.models import Course, Resource, Topic


def test_browser_flow(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"], source="moodle", source_id="c1",
                        name="CS 301", code="CS 301")
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


def test_unowned_course_hidden(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(source="moodle", source_id="c9", name="Pre-auth course")
        s.add(course)
        s.commit()
        course_id = course.id
    home = client.get("/")
    assert home.status_code == 200 and "Pre-auth course" not in home.text
    assert client.get(f"/courses/{course_id}").status_code == 404


def _resource_with_text(testapp, text, owned=True):
    Session = testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"] if owned else None,
                        source="moodle", source_id="c2", name="CS 302")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t2", title="Graphs", order=0)
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r2", type="file",
                       title="graphs.pdf", status="extracted", extracted_text=text)
        s.add(res)
        s.commit()
        return str(res.id)


def test_long_text_preview_notes_truncation_and_serves_full_text(testapp):
    from app.main import RESOURCE_PREVIEW_CHARS

    client = testapp["client"]
    text = "a" * RESOURCE_PREVIEW_CHARS + "TAIL"
    rid = _resource_with_text(testapp, text)

    page = client.get(f"/resources/{rid}").text
    assert "TAIL" not in page
    assert f"of {len(text):,} characters" in page
    assert f'data-full-text-url="/resources/{rid}/text"' in page

    r = client.get(f"/resources/{rid}/text")
    assert r.status_code == 200 and r.text == text
    assert r.headers["content-type"].startswith("text/plain")


def test_short_text_has_no_truncation_note(testapp):
    client = testapp["client"]
    rid = _resource_with_text(testapp, "short notes")
    page = client.get(f"/resources/{rid}").text
    assert "short notes" in page
    assert "Showing the first" not in page and "data-full-text-url" not in page


def test_full_text_hidden_from_other_users(testapp):
    client = testapp["client"]
    rid = _resource_with_text(testapp, "secret", owned=False)
    assert client.get(f"/resources/{rid}/text").status_code == 404
