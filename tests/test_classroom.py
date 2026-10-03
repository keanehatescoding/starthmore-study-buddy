"""Classroom adapter: pagination, untopic'd materials, Drive change markers,
coursework and announcement attachments."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app.classroom import (
    ANNOUNCEMENTS_TITLE,
    ANNOUNCEMENTS_TOPIC_ID,
    UNTAGGED_TITLE,
    UNTAGGED_TOPIC_ID,
    ClassroomAdapter,
    ClassroomClient,
)
from app.models import Assignment, Resource, Topic, User
from app.sync import content_hash, sync_course
from tests.dbutil import make_engine


class _Request:
    def __init__(self, collection, params, page):
        self.collection, self.params, self.page = collection, params, page

    def execute(self, num_retries=0):
        self.collection.retries = num_retries
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
    def __init__(self, courses, topics, materials, coursework, announcements):
        super().__init__(courses)
        self._topics, self._materials, self._work = topics, materials, coursework
        self._announcements = announcements

    def topics(self):
        return self._topics

    def courseWorkMaterials(self):
        return self._materials

    def courseWork(self):
        return self._work

    def announcements(self):
        return self._announcements


class _Service:
    def __init__(self, topics=(), materials=(), courses=None, coursework=None,
                 announcements=()):
        self._courses = _Courses(
            courses or [{"courses": [{"id": "c1", "name": "Algorithms"}]}],
            _Collection([{"topic": list(topics)}]),
            _Collection([{"courseWorkMaterial": list(materials)}]),
            _Collection(coursework or [{}]),
            _Collection([{"announcements": list(announcements)}]),
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
    assert r.title == "notes.pdf"
    assert r.raw_url == "https://drive.google.com/file/d/F1/view"  # built from the id
    assert content_hash(r) is not None  # no link, still hashable by id

    swapped = _Service(materials=[_material("m1", materials=[_drive("F2", link=None)])])
    adapter = ClassroomAdapter(ClassroomClient(swapped))
    adapter.fetch_topics("c1")
    [r2] = adapter.fetch_resources("c1", UNTAGGED_TOPIC_ID)
    assert content_hash(r2) != content_hash(r)  # replaced attachment is a change


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
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


def test_classroom_calls_retry_transient_errors():
    service = _Service()
    ClassroomClient(service).list_courses()
    assert service._courses.retries == 3


def _work(wid, topic_id=None, materials=None, title="Assignment 1", **extra):
    w = {"id": wid, "title": title, **extra}
    if materials is not None:
        w["materials"] = materials
    if topic_id is not None:
        w["topicId"] = topic_id
    return w


def _announcement(aid, text="Slides for today\nSee you in class", materials=None):
    a = {"id": aid, "text": text}
    if materials is not None:
        a["materials"] = materials
    return a


def test_coursework_attachments_sync_under_their_topic():
    service = _Service(
        topics=[{"topicId": "t1", "name": "Week 1"}],
        coursework=[{"courseWork": [
            _work("w1", "t1", [_drive("F1", title="lab1.pdf")]),
            _work("w2", materials=[{"link": {"url": "https://ex.com/spec"}}]),
            _work("w3", "t1"),  # no attachments: an assignment only
        ]}],
    )
    adapter = ClassroomAdapter(ClassroomClient(service))
    assert [t.source_id for t in adapter.fetch_topics("c1")] == ["t1", UNTAGGED_TOPIC_ID]
    [lab] = adapter.fetch_resources("c1", "t1")
    assert (lab.source_id, lab.title, lab.type) == ("work:w1:0", "lab1.pdf", "file")
    [spec] = adapter.fetch_resources("c1", UNTAGGED_TOPIC_ID)
    assert (spec.source_id, spec.title) == ("work:w2:0", "Assignment 1")
    # the assignment list reuses the courseWork page fetch_topics read
    assert [a.source_id for a in adapter.fetch_assignments("c1")] == ["w1", "w2", "w3"]
    assert len(service.courses().courseWork().calls) == 1


def test_announcement_attachments_sync_under_announcements_topic():
    service = _Service(
        materials=[_material("m1", "t1")],
        topics=[{"topicId": "t1", "name": "Week 1"}],
        announcements=[
            _announcement("a1", materials=[_drive("F1", title="")]),
            _announcement("a2"),  # text only: nothing to study
            _announcement("a3", text="", materials=[
                {"youtubeVideo": {"id": "yt1"}}]),
        ],
    )
    adapter = ClassroomAdapter(ClassroomClient(service))
    topics = adapter.fetch_topics("c1")
    assert [(t.source_id, t.title, t.order) for t in topics] == [
        ("t1", "Week 1", 0), (ANNOUNCEMENTS_TOPIC_ID, ANNOUNCEMENTS_TITLE, 1)]
    out = adapter.fetch_resources("c1", ANNOUNCEMENTS_TOPIC_ID)
    assert [(r.source_id, r.title, r.type) for r in out] == [
        ("ann:a1:0", "Slides for today", "file"),  # first line of the text
        ("ann:a3:0", "Announcement", "video"),
    ]


def test_long_announcement_title_is_truncated():
    service = _Service(announcements=[
        _announcement("a1", text="x" * 200, materials=[{"link": {"url": "https://e"}}])])
    adapter = ClassroomAdapter(ClassroomClient(service))
    adapter.fetch_topics("c1")
    [r] = adapter.fetch_resources("c1", ANNOUNCEMENTS_TOPIC_ID)
    assert len(r.title) == 80 and r.title.endswith("…")


class _HttpError(Exception):
    def __init__(self, status, content):
        self.resp = type("Resp", (), {"status": status})()
        self.content = content


class _Failing(_Collection):
    def __init__(self, error):
        super().__init__([])
        self.error = error

    def list(self, **params):
        error = self.error

        class _Raise:
            def execute(self, num_retries=0):
                raise error
        return _Raise()


def test_token_without_announcements_scope_skips_announcements():
    service = _Service(materials=[_material("m1")])
    service._courses._announcements = _Failing(_HttpError(
        403, b'{"error": {"message": "Request had insufficient authentication scopes."}}'))
    adapter = ClassroomAdapter(ClassroomClient(service))
    assert [t.source_id for t in adapter.fetch_topics("c1")] == [UNTAGGED_TOPIC_ID]


def test_other_announcement_errors_propagate():
    service = _Service()
    service._courses._announcements = _Failing(_HttpError(500, b"backend error"))
    with pytest.raises(_HttpError):
        ClassroomAdapter(ClassroomClient(service)).fetch_topics("c1")


def test_sync_stores_coursework_and_announcement_attachments(session):
    user = User(email="s@x.edu")
    session.add(user)
    session.commit()
    service = _Service(
        materials=[_material("m1")],
        coursework=[{"courseWork": [_work("w1", materials=[_drive("F1")])]}],
        announcements=[_announcement("a1", materials=[_drive("F2", title="wk2.pdf")])],
    )
    stats = sync_course(session, ClassroomAdapter(ClassroomClient(service)), "c1", user.id)
    assert stats.resources_new == 3
    rows = {r.source_id: session.get(Topic, r.topic_id).source_id
            for r in session.exec(select(Resource))}
    assert rows == {"m1:0": UNTAGGED_TOPIC_ID, "work:w1:0": UNTAGGED_TOPIC_ID,
                    "ann:a1:0": ANNOUNCEMENTS_TOPIC_ID}
    assert [a.source_id for a in session.exec(select(Assignment))] == ["w1"]



def test_gem_and_notebook_attachments_sync_as_links():
    service = _Service(announcements=[_announcement("a1", materials=[
        {"gem": {"id": "g1", "title": "Tutor", "url": "https://gemini.google.com/gem/g1"}},
        {"notebook": {"id": "n1", "url": "https://notebooklm.google.com/notebook/n1"}},
    ])])
    adapter = ClassroomAdapter(ClassroomClient(service))
    adapter.fetch_topics("c1")
    out = adapter.fetch_resources("c1", ANNOUNCEMENTS_TOPIC_ID)
    assert [(r.source_id, r.title, r.type, r.raw_url) for r in out] == [
        ("ann:a1:0", "Tutor", "link", "https://gemini.google.com/gem/g1"),
        ("ann:a1:1", "Slides for today", "link",
         "https://notebooklm.google.com/notebook/n1"),
    ]


def test_unsupported_attachments_add_no_synthetic_topic_or_renumber():
    service = _Service(
        materials=[_material("m1", materials=[
            {"somethingNew": {}}, {"link": {"url": "https://ex.com/a"}}])],
        announcements=[_announcement("a1", materials=[{"somethingNew": {}}])],
    )
    adapter = ClassroomAdapter(ClassroomClient(service))
    assert [t.source_id for t in adapter.fetch_topics("c1")] == [UNTAGGED_TOPIC_ID]
    [r] = adapter.fetch_resources("c1", UNTAGGED_TOPIC_ID)
    assert r.source_id == "m1:1"  # keeps its index past the skipped attachment
