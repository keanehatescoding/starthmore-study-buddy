"""Classroom adapter: pagination, untopic'd materials, Drive change markers."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.classroom import (
    UNTAGGED_TITLE,
    UNTAGGED_TOPIC_ID,
    ClassroomAdapter,
    ClassroomClient,
)
from app.models import Resource, Topic, User
from app.sync import content_hash, sync_course


class _Request:
    def __init__(self, collection, params, page):
        self.collection, self.params, self.page = collection, params, page

    def execute(self):
        self.collection.calls.append(self.params)
        return self.collection.pages[self.page]


class _Collection:
    """Mimics a googleapiclient collection: list() + list_next()."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def list(self, **params):
        return _Request(self, params, 0)

    def list_next(self, request, resp):
        token = resp.get("nextPageToken")
        if not token:
            return None
        return _Request(self, {**request.params, "pageToken": token}, request.page + 1)


class _Courses(_Collection):
    def __init__(self, courses, topics, materials, coursework):
        super().__init__(courses)
        self._topics, self._materials, self._work = topics, materials, coursework

    def topics(self):
        return self._topics

    def courseWorkMaterials(self):
        return self._materials

    def courseWork(self):
        return self._work


class _Service:
    def __init__(self, topics=(), materials=(), courses=None, coursework=None):
        self._courses = _Courses(
            courses or [{"courses": [{"id": "c1", "name": "Algorithms"}]}],
            _Collection([{"topic": list(topics)}]),
            _Collection([{"courseWorkMaterial": list(materials)}]),
            _Collection(coursework or [{}]),
        )

    def courses(self):
        return self._courses


def _drive(file_id, title="notes.pdf", link="https://drive/x"):
    return {"driveFile": {"driveFile": {"id": file_id, "title": title,
                                        "alternateLink": link},
                          "shareMode": "VIEW"}}


def _material(mid, topic_id=None, materials=None, title="M"):
    m = {"id": mid, "title": title,
         "materials": materials or [{"link": {"url": f"https://ex.com/{mid}"}}]}
    if topic_id is not None:
        m["topicId"] = topic_id
    return m


def test_pagination_follows_list_next_without_initial_none_token():
    work = _Collection([
        {"courseWork": [{"id": "w1"}], "nextPageToken": "p2"},
        {"courseWork": [{"id": "w2"}]},
    ])
    service = _Service()
    service._courses._work = work
    out = ClassroomClient(service).list_coursework("c1")
    assert [w["id"] for w in out] == ["w1", "w2"]
    assert work.calls == [
        {"pageSize": 100, "courseId": "c1"},  # no pageToken=None on the first call
        {"pageSize": 100, "courseId": "c1", "pageToken": "p2"},
    ]


def test_list_topics_reads_singular_topic_field():
    service = _Service(topics=[{"topicId": "t1", "name": "Week 1"}])
    assert ClassroomClient(service).list_topics("c1") == [{"topicId": "t1", "name": "Week 1"}]


def test_untopicd_and_orphaned_materials_go_to_untagged_topic():
    service = _Service(
        topics=[{"topicId": "t1", "name": "Week 1"}],
        materials=[_material("m1", "t1"), _material("m2"),
                   _material("m3", "deleted-topic")],
    )
    adapter = ClassroomAdapter(ClassroomClient(service))
    topics = adapter.fetch_topics("c1")
    assert [(t.source_id, t.title, t.order) for t in topics] == [
        ("t1", "Week 1", 0), (UNTAGGED_TOPIC_ID, UNTAGGED_TITLE, 1)]
    assert [r.source_id for r in adapter.fetch_resources("c1", "t1")] == ["m1:0"]
    assert [r.source_id for r in adapter.fetch_resources("c1", UNTAGGED_TOPIC_ID)] == [
        "m2:0", "m3:0"]
    assert len(service.courses().courseWorkMaterials().calls) == 1  # listed once


def test_no_untagged_topic_when_every_material_has_one():
    service = _Service(topics=[{"topicId": "t1", "name": "W"}],
                       materials=[_material("m1", "t1")])
    topics = ClassroomAdapter(ClassroomClient(service)).fetch_topics("c1")
    assert [t.source_id for t in topics] == ["t1"]


def test_drive_file_uses_inner_title_and_id_marker():
    service = _Service(materials=[_material("m1", materials=[_drive("F1", link=None)])])
    adapter = ClassroomAdapter(ClassroomClient(service))
    adapter.fetch_topics("c1")
    [r] = adapter.fetch_resources("c1", UNTAGGED_TOPIC_ID)
    assert r.title == "notes.pdf" and r.raw_url is None
    assert content_hash(r) is not None  # no link, still hashable by id

    swapped = _Service(materials=[_material("m1", materials=[_drive("F2", link=None)])])
    adapter = ClassroomAdapter(ClassroomClient(swapped))
    adapter.fetch_topics("c1")
    [r2] = adapter.fetch_resources("c1", UNTAGGED_TOPIC_ID)
    assert content_hash(r2) != content_hash(r)  # replaced attachment is a change


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_sync_keeps_untopicd_materials(session):
    user = User(email="s@x.edu")
    session.add(user)
    session.commit()
    service = _Service(materials=[_material("m1"), _material("m2", materials=[_drive("F1")])])
    stats = sync_course(session, ClassroomAdapter(ClassroomClient(service)), "c1", user.id)
    assert stats.resources_new == 2
    topic = session.exec(select(Topic)).one()
    assert (topic.source_id, topic.title) == (UNTAGGED_TOPIC_ID, UNTAGGED_TITLE)
    titles = sorted(r.title for r in session.exec(select(Resource)))
    assert titles == ["M", "notes.pdf"]


def test_material_moved_to_other_materials_is_moved_not_duplicated(session):
    user = User(email="s@x.edu")
    session.add(user)
    session.commit()
    service = _Service(topics=[{"topicId": "T1", "name": "Week 1"}],
                       materials=[_material("m1", topic_id="T1")])
    sync_course(session, ClassroomAdapter(ClassroomClient(service)), "c1", user.id)
    before = session.exec(select(Resource)).one()

    # teacher removes the topic from the material: it now sits under "Other materials"
    service = _Service(topics=[{"topicId": "T1", "name": "Week 1"}],
                       materials=[_material("m1")])
    stats = sync_course(session, ClassroomAdapter(ClassroomClient(service)), "c1", user.id)

    assert (stats.resources_new, stats.resources_updated) == (0, 1)
    after = session.exec(select(Resource)).one()
    assert after.id == before.id
    assert session.get(Topic, after.topic_id).source_id == UNTAGGED_TOPIC_ID


class _Coursework:
    def __init__(self, work):
        self.work = work

    def list_coursework(self, course_id):
        return [self.work]


@pytest.mark.parametrize("due_time, expected", [
    ({"hours": 14}, (14, 0, 0)),
    ({}, (0, 0, 0)),
    ({"hours": 14, "minutes": 30, "seconds": 45}, (14, 30, 45)),
    ({"minutes": 5}, (0, 5, 0)),
    (None, (23, 59, 59)),  # no dueTime at all: end of the due day
])
def test_due_time_omitted_fields_are_zero(due_time, expected):
    work = {"id": "w1", "dueDate": {"year": 2026, "month": 10, "day": 1}}
    if due_time is not None:
        work["dueTime"] = due_time
    [a] = ClassroomAdapter(_Coursework(work)).fetch_assignments("c1")
    assert a.due_date == datetime(2026, 10, 1, *expected, tzinfo=timezone.utc)
