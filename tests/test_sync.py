"""Phase 1 tests: diff-based sync via a FakeAdapter (no network)."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Assignment, Chunk, Course, QuizItem, Resource, ReviewState, Topic, User
from app.sync import (
    AssignmentData,
    CourseData,
    ResourceData,
    TopicData,
    content_hash,
    link_type,
    sync_all,
    sync_course,
)


class FakeAdapter:
    source = "moodle"

    def __init__(self):
        self.courses = [CourseData("c1", "CS 301", "CS 301")]
        self.topics = {"c1": [TopicData("t1", "Trees", 0)]}
        self.resources = {
            ("c1", "t1"): [
                ResourceData("t1", "r-file", "file", "trees.pdf",
                             raw_url="https://x/f.pdf", content_bytes=b"v1"),
                ResourceData("t1", "r-page", "page_text", "Overview",
                             text="A tree is..."),
                ResourceData("t1", "r-link", "link", "Docs",
                             raw_url="https://example.com",
                             content_bytes=b"https://example.com"),
            ]
        }
        self.assignments = {
            "c1": [AssignmentData("a1", "Assignment 1", "t1",
                                  datetime(2026, 10, 1, tzinfo=timezone.utc), "Do trees")]
        }

    def fetch_courses(self):
        self.course_fetches = getattr(self, "course_fetches", 0) + 1
        return self.courses
    def fetch_topics(self, cid): return self.topics[cid]
    def fetch_resources(self, cid, tid): return self.resources[(cid, tid)]
    def fetch_assignments(self, cid): return self.assignments[cid]


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def user_id(session):
    user = User(email="s@x.edu")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


def test_first_sync_inserts(session, user_id):
    stats = sync_course(session, FakeAdapter(), "c1", user_id)
    assert (stats.courses_new, stats.topics_new) == (1, 1)
    assert stats.resources_new == 3
    assert stats.assignments_new == 1
    assert session.exec(select(Resource)).all().__len__() == 3
    page = session.exec(select(Resource).where(Resource.source_id == "r-page")).one()
    assert page.status == "extracted" and page.extracted_text == "A tree is..."
    asg = session.exec(select(Assignment)).one()
    assert asg.title == "Assignment 1"  # deadlines never land in resources


def test_second_sync_skips_everything(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_new == 0 and stats.resources_updated == 0
    assert stats.resources_skipped == 3


def test_changed_content_requeues(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    adapter.resources[("c1", "t1")][0] = ResourceData(
        "t1", "r-file", "file", "trees.pdf",
        raw_url="https://x/f.pdf",
        content_bytes="v2 — lecturer updated slides".encode("utf-8"))
    stats = sync_course(session, adapter, "c1", user_id)
    assert (stats.resources_updated, stats.resources_skipped) == (1, 2)
    updated = session.exec(select(Resource).where(Resource.source_id == "r-file")).one()
    assert updated.status == "pending" and updated.extracted_text is None


def test_unknown_course_raises(session, user_id):
    with pytest.raises(ValueError):
        sync_course(session, FakeAdapter(), "nope", user_id)


def test_same_source_course_per_user(session, user_id):
    other = User(email="other@x.edu")
    session.add(other)
    session.commit()
    session.refresh(other)
    sync_course(session, FakeAdapter(), "c1", user_id)
    stats = sync_course(session, FakeAdapter(), "c1", other.id)
    assert stats.courses_new == 1  # no unique-clash across users
    mine = session.exec(
        select(Course).where(Course.user_id == user_id)).all()
    assert len(mine) == 1


def test_unowned_sync_reuses_unowned_course(session):
    sync_course(session, FakeAdapter(), "c1", None)
    stats = sync_course(session, FakeAdapter(), "c1", None)
    assert stats.courses_new == 0
    assert len(session.exec(select(Course)).all()) == 1


def test_user_sync_adopts_pre_auth_course(session, user_id):
    sync_course(session, FakeAdapter(), "c1", None)
    stats = sync_course(session, FakeAdapter(), "c1", user_id)
    assert stats.courses_new == 0 and stats.resources_new == 0
    course = session.exec(select(Course)).one()
    assert course.user_id == user_id


def test_link_type():
    assert link_type("https://www.youtube.com/watch?v=abc") == "video"
    assert link_type("https://youtu.be/abc") == "video"
    assert link_type("https://example.com/notes") == "link"
    assert content_hash(ResourceData("t", "s", "link", "L")) is None


def _derive(session, resource, user_id, tag):
    chunk = Chunk(resource_id=resource.id, title=tag, content=tag)
    session.add(chunk)
    session.commit()
    item = QuizItem(chunk_id=chunk.id, question="q", question_type="mcq",
                    correct_answer="0", generation_key=f"{tag}:1:0")
    session.add(item)
    session.commit()
    session.add(ReviewState(user_id=user_id, quiz_item_id=item.id,
                            next_review_date=datetime.now(timezone.utc)))
    session.commit()


def _resource(session, source_id):
    return session.exec(select(Resource).where(Resource.source_id == source_id)).one()


def test_changed_content_purges_derived_rows(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    _derive(session, _resource(session, "r-file"), user_id, "file")
    _derive(session, _resource(session, "r-page"), user_id, "page")
    adapter.resources[("c1", "t1")][0].content_bytes = b"v2"
    sync_course(session, adapter, "c1", user_id)
    session.expire_all()
    assert [c.title for c in session.exec(select(Chunk)).all()] == ["page"]
    assert len(session.exec(select(QuizItem)).all()) == 1
    assert len(session.exec(select(ReviewState)).all()) == 1


def test_title_only_change_updates_without_reset(session, user_id):
    adapter = FakeAdapter()
    adapter.resources[("c1", "t1")].append(
        ResourceData("t1", "r-nohash", "link", "Old", raw_url="https://a"))
    sync_course(session, adapter, "c1", user_id)
    _derive(session, _resource(session, "r-page"), user_id, "page")
    res = adapter.resources[("c1", "t1")]
    res[1].title = "Overview (revised)"
    res[3].title, res[3].raw_url = "New", "https://b"
    stats = sync_course(session, adapter, "c1", user_id)
    assert (stats.resources_updated, stats.resources_skipped) == (2, 2)
    session.expire_all()
    page = _resource(session, "r-page")
    assert page.title == "Overview (revised)"
    assert page.status == "extracted" and page.extracted_text == "A tree is..."
    assert len(session.exec(select(Chunk)).all()) == 1  # not purged
    nohash = _resource(session, "r-nohash")
    assert (nohash.title, nohash.raw_url) == ("New", "https://b")


def test_unchanged_assignment_not_counted(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    assert sync_course(session, adapter, "c1", user_id).assignments_updated == 0
    adapter.assignments["c1"][0].title = "Assignment 1 (extended)"
    assert sync_course(session, adapter, "c1", user_id).assignments_updated == 1


def _to_fingerprint(session, user_id, fetch_content):
    """Sync with a legacy content hash, then switch the file to a fingerprint."""
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)  # legacy full-content hash (b"v1")
    _derive(session, _resource(session, "r-file"), user_id, "file")
    f = adapter.resources[("c1", "t1")][0]
    f.content_bytes, f.fingerprint = None, "https://x/content/1/f.pdf|100|1700000000"
    adapter.fetch_content = fetch_content
    return adapter, f


def test_legacy_hash_verified_same_bytes_keeps_progress(session, user_id):
    adapter, f = _to_fingerprint(session, user_id, lambda r: b"v1")
    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_updated == 0
    assert len(session.exec(select(Chunk)).all()) == 1
    assert _resource(session, "r-file").content_hash.startswith("fp:")
    f.fingerprint = "https://x/content/2/f.pdf|100|1700000000"  # revision bump
    assert sync_course(session, adapter, "c1", user_id).resources_updated == 1
    session.expire_all()
    assert _resource(session, "r-file").status == "pending"
    assert session.exec(select(Chunk)).all() == []


def test_legacy_hash_verified_changed_bytes_resets(session, user_id):
    adapter, _ = _to_fingerprint(session, user_id, lambda r: b"v2 new slides")
    assert sync_course(session, adapter, "c1", user_id).resources_updated == 1
    session.expire_all()
    res = _resource(session, "r-file")
    assert res.status == "pending" and res.content_hash.startswith("fp:")
    assert session.exec(select(Chunk)).all() == []


def test_legacy_hash_unverifiable_keeps_legacy_and_retries(session, user_id):
    def down(r):
        raise RuntimeError("moodle down")

    adapter, _ = _to_fingerprint(session, user_id, down)
    legacy = _resource(session, "r-file").content_hash
    assert sync_course(session, adapter, "c1", user_id).resources_updated == 0
    assert _resource(session, "r-file").content_hash == legacy
    assert len(session.exec(select(Chunk)).all()) == 1
    adapter.fetch_content = lambda r: b"v1"  # next sync can verify
    sync_course(session, adapter, "c1", user_id)
    assert _resource(session, "r-file").content_hash.startswith("fp:")


def test_sync_all_fetches_courses_once(session, user_id):
    adapter = FakeAdapter()
    adapter.courses.append(CourseData("c2", "CS 302"))
    adapter.topics["c2"] = []
    adapter.assignments["c2"] = []
    assert set(sync_all(session, adapter, user_id)) == {"c1", "c2"}
    assert adapter.course_fetches == 1


class FakeMoodleClient:
    def __init__(self):
        self.calls: list[str] = []

    def site_info(self):
        return {"userid": 7}

    def get_users_courses(self, userid):
        return [{"id": 5, "fullname": "CS 301", "shortname": "CS301"}]

    def get_course_contents(self, courseid):
        self.calls.append("contents")
        file = {"type": "file", "filename": "a.pdf", "filepath": "/",
                "fileurl": "https://m/a.pdf", "filesize": 10, "timemodified": 1,
                "mimetype": "application/pdf"}
        return [
            {"id": s, "name": f"S{s}", "modules": [
                {"id": s * 10, "modname": "resource", "name": "A", "contents": [file]},
                {"id": s * 10 + 1, "modname": "page", "name": "P"},
                {"id": s * 10 + 2, "modname": "page", "name": "Q"},
            ]}
            for s in (1, 2)
        ]

    def get_assignments(self, courseid):
        return {"courses": []}

    def call(self, function, **params):
        self.calls.append(function)
        return {"pages": [{"coursemodule": 11, "content": "page 11"}]}

    def download(self, fileurl):
        raise AssertionError("sync must not download files")


def test_moodle_adapter_fetches_contents_once_and_never_downloads(session, user_id):
    from app.moodle import MoodleAdapter

    client = FakeMoodleClient()
    stats = sync_all(session, MoodleAdapter(client), user_id)["5"]
    assert stats.resources_new == 6
    assert client.calls == ["contents", "mod_page_get_pages_by_courses"]
    f = _resource(session, "10")
    assert f.content_hash.startswith("fp:") and f.mime_type == "application/pdf"
    assert _resource(session, "11").extracted_text == "page 11"
