"""Diff-based sync. Source-agnostic: adapters yield normalized dataclasses,
this module upserts them. Implements the plan's 6-step sync per course:

  new resource -> insert (pending, or extracted if text already present)
  hash changed -> reset for re-extract/re-chunk (Phase 2/3 regenerates)
  unchanged    -> skip (hash check avoids re-running expensive LLM steps)
Assignments are upserted separately and never become Resources.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlmodel import Session, select

from app.models import Assignment, Course, Resource, Topic

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def is_youtube(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname in YOUTUBE_HOSTS
    except Exception:
        return False


def link_type(url: str) -> str:
    return "video" if is_youtube(url) else "link"


# -- normalized payloads -------------------------------------------------------


@dataclass
class CourseData:
    source_id: str
    name: str
    code: str | None = None


@dataclass
class TopicData:
    source_id: str
    title: str
    order: int = 0


@dataclass
class ResourceData:
    topic_source_id: str
    source_id: str
    type: str  # file|link|page_text|video
    title: str
    raw_url: str | None = None
    mime_type: str | None = None
    content_bytes: bytes | None = None  # file content (hash only; not stored)
    text: str | None = None  # page_text / link-URL stub (stored as extracted_text)


@dataclass
class AssignmentData:
    source_id: str
    title: str
    topic_source_id: str | None = None
    due_date: datetime | None = None
    description: str | None = None


class SourceAdapter(Protocol):
    source: str  # "moodle" | "classroom"

    def fetch_courses(self) -> list[CourseData]: ...
    def fetch_topics(self, course_source_id: str) -> list[TopicData]: ...
    def fetch_resources(
        self, course_source_id: str, topic_source_id: str
    ) -> list[ResourceData]: ...
    def fetch_assignments(self, course_source_id: str) -> list[AssignmentData]: ...


def content_hash(data: ResourceData) -> str | None:
    blob = data.content_bytes
    if blob is None and data.text is not None:
        blob = data.text.encode("utf-8")
    if blob is None:
        return None
    return hashlib.sha256(blob).hexdigest()


# -- sync ----------------------------------------------------------------------


@dataclass
class SyncStats:
    courses_new: int = 0
    topics_new: int = 0
    resources_new: int = 0
    resources_updated: int = 0
    resources_skipped: int = 0
    assignments_new: int = 0
    assignments_updated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


def _upsert_course(session: Session, source: str, data: CourseData) -> tuple[Course, bool]:
    course = session.exec(
        select(Course).where(Course.source == source, Course.source_id == data.source_id)
    ).first()
    if course is None:
        course = Course(source=source, source_id=data.source_id, name=data.name, code=data.code)
        session.add(course)
        session.commit()
        session.refresh(course)
        return course, True
    if course.name != data.name or course.code != data.code:
        course.name, course.code = data.name, data.code
        session.add(course)
        session.commit()
    return course, False


def sync_course(
    session: Session, adapter: SourceAdapter, course_source_id: str
) -> SyncStats:
    stats = SyncStats()
    source = adapter.source

    course_data = next(
        (c for c in adapter.fetch_courses() if c.source_id == course_source_id), None
    )
    if course_data is None:
        raise ValueError(f"course {course_source_id!r} not found in source {source!r}")
    course, is_new = _upsert_course(session, source, course_data)
    stats.courses_new += is_new

    topic_id_by_source: dict[str, Any] = {}
    for t in adapter.fetch_topics(course_source_id):
        topic = session.exec(
            select(Topic).where(Topic.course_id == course.id, Topic.source_id == t.source_id)
        ).first()
        if topic is None:
            topic = Topic(
                course_id=course.id, source_id=t.source_id, title=t.title, order=t.order
            )
            session.add(topic)
            session.commit()
            session.refresh(topic)
            stats.topics_new += 1
        elif topic.title != t.title or topic.order != t.order:
            topic.title, topic.order = t.title, t.order
            session.add(topic)
            session.commit()
        topic_id_by_source[t.source_id] = topic.id

    for topic_source_id, topic_id in topic_id_by_source.items():
        for r in adapter.fetch_resources(course_source_id, topic_source_id):
            digest = content_hash(r)
            existing = session.exec(
                select(Resource).where(
                    Resource.topic_id == topic_id, Resource.source_id == r.source_id
                )
            ).first()
            if existing is None:
                session.add(
                    Resource(
                        topic_id=topic_id, source=source, source_id=r.source_id,
                        type=r.type, title=r.title, raw_url=r.raw_url,
                        extracted_text=r.text, content_hash=digest,
                        status="extracted" if r.text else "pending",
                        mime_type=r.mime_type,
                    )
                )
                session.commit()
                stats.resources_new += 1
            elif digest is not None and existing.content_hash != digest:
                existing.content_hash = digest
                existing.title = r.title
                existing.raw_url = r.raw_url
                existing.mime_type = r.mime_type
                existing.extracted_text = r.text
                existing.status = "extracted" if r.text else "pending"
                existing.error = None
                session.add(existing)
                session.commit()
                stats.resources_updated += 1
            else:
                stats.resources_skipped += 1

    for a in adapter.fetch_assignments(course_source_id):
        topic_id = topic_id_by_source.get(a.topic_source_id) if a.topic_source_id else None
        existing = session.exec(
            select(Assignment).where(
                Assignment.course_id == course.id, Assignment.source_id == a.source_id
            )
        ).first()
        if existing is None:
            session.add(
                Assignment(
                    course_id=course.id, topic_id=topic_id, source=source,
                    source_id=a.source_id, title=a.title,
                    due_date=a.due_date, description=a.description,
                )
            )
            session.commit()
            stats.assignments_new += 1
        else:
            existing.title = a.title
            existing.topic_id = topic_id
            existing.due_date = a.due_date
            existing.description = a.description
            session.add(existing)
            session.commit()
            stats.assignments_updated += 1

    return stats


def sync_all(session: Session, adapter: SourceAdapter) -> dict[str, SyncStats]:
    return {c.source_id: sync_course(session, adapter, c.source_id)
            for c in adapter.fetch_courses()}
