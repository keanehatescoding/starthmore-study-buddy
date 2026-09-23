"""Phase 1 tests: diff-based sync via a FakeAdapter (no network)."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Assignment, Course, Resource, Topic
from app.sync import (
    AssignmentData,
    CourseData,
    ResourceData,
    TopicData,
    content_hash,
    link_type,
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

    def fetch_courses(self): return self.courses
    def fetch_topics(self, cid): return self.topics[cid]
    def fetch_resources(self, cid, tid): return self.resources[(cid, tid)]
    def fetch_assignments(self, cid): return self.assignments[cid]


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_first_sync_inserts(session):
    stats = sync_course(session, FakeAdapter(), "c1")
    assert (stats.courses_new, stats.topics_new) == (1, 1)
    assert stats.resources_new == 3
    assert stats.assignments_new == 1
    assert session.exec(select(Resource)).all().__len__() == 3
    page = session.exec(select(Resource).where(Resource.source_id == "r-page")).one()
    assert page.status == "extracted" and page.extracted_text == "A tree is..."
    asg = session.exec(select(Assignment)).one()
    assert asg.title == "Assignment 1"  # deadlines never land in resources


def test_second_sync_skips_everything(session):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1")
    stats = sync_course(session, adapter, "c1")
    assert stats.resources_new == 0 and stats.resources_updated == 0
    assert stats.resources_skipped == 3


def test_changed_content_requeues(session):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1")
    adapter.resources[("c1", "t1")][0] = ResourceData(
        "t1", "r-file", "file", "trees.pdf",
        raw_url="https://x/f.pdf",
        content_bytes="v2 — lecturer updated slides".encode("utf-8"))
    stats = sync_course(session, adapter, "c1")
    assert (stats.resources_updated, stats.resources_skipped) == (1, 2)
    updated = session.exec(select(Resource).where(Resource.source_id == "r-file")).one()
    assert updated.status == "pending" and updated.extracted_text is None


def test_unknown_course_raises(session):
    with pytest.raises(ValueError):
        sync_course(session, FakeAdapter(), "nope")


def test_link_type():
    assert link_type("https://www.youtube.com/watch?v=abc") == "video"
    assert link_type("https://youtu.be/abc") == "video"
    assert link_type("https://example.com/notes") == "link"
    assert content_hash(ResourceData("t", "s", "link", "L")) is None
