"""Diff-based sync. Source-agnostic: adapters yield normalized dataclasses,
this module upserts them. Implements the plan's 6-step sync per course:

  new resource  -> insert (pending, or extracted if text already present)
  hash changed  -> reset for re-extract/re-chunk and drop derived chunks,
                   quiz items and review states (Phase 2/3 regenerates)
  meta changed  -> update title/url/type/mime in place, no reset
  topic changed -> move the row to the new topic, no reset
  unchanged     -> skip (hash check avoids re-running expensive LLM steps)
Assignments are upserted separately and never become Resources.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlmodel import Session, delete, func, select, update

from app.models import Assignment, Chunk, Course, QuizItem, Resource, ReviewState, Topic

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
    # Change marker from source metadata (e.g. url|size|mtime) used instead of
    # content, so sync never has to download files. Takes precedence when set.
    fingerprint: str | None = None


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


FINGERPRINT_PREFIX = "fp:"


def content_hash(data: ResourceData) -> str | None:
    if data.fingerprint is not None:
        digest = hashlib.sha256(data.fingerprint.encode("utf-8")).hexdigest()
        return FINGERPRINT_PREFIX + digest
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
    resources_removed: int = 0
    assignments_new: int = 0
    assignments_updated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


def _upsert_course(
    session: Session, source: str, user_id, data: CourseData
) -> tuple[Course, bool]:
    same = select(Course).where(
        Course.source == source, Course.source_id == data.source_id
    )
    # NULL never equals NULL in SQL, so unowned rows need IS NULL to match.
    unowned = same.where(Course.user_id.is_(None))
    if user_id is None:
        course = session.exec(unowned).first()
    else:
        mine = same.where(Course.user_id == user_id)
        course = session.exec(mine).first()
        if course is None:
            # Adopt a pre-auth row (synced before owners existed) instead of
            # duplicating it; syncing proves this user is enrolled.
            course = _adopt_unowned(session, unowned, user_id)
        if course is None:
            # A lost claim may have gone to another sync for this same user.
            course = session.exec(mine).first()
    if course is None:
        course = Course(user_id=user_id, source=source, source_id=data.source_id,
                        name=data.name, code=data.code)
        session.add(course)
        session.commit()
        session.refresh(course)
        return course, True
    if course.name != data.name or course.code != data.code:
        course.name, course.code = data.name, data.code
        session.add(course)
        session.commit()
    return course, False


def _adopt_unowned(session: Session, unowned, user_id) -> Course | None:
    """Claim an unowned course row for user_id, or None if there is none.

    The UPDATE only matches while the row is still unowned, so when two
    syncs race for it exactly one wins."""
    course = session.exec(unowned).first()
    if course is None:
        return None
    claimed = session.exec(
        update(Course)
        .where(Course.id == course.id, Course.user_id.is_(None))
        .values(user_id=user_id)
    )
    session.commit()
    if claimed.rowcount != 1:
        return None
    session.refresh(course)
    return course


def _purge_derived(session: Session, resource_id) -> None:
    """Drop chunks, quiz items and review states built from a resource's old
    content. Explicit (not just ON DELETE CASCADE) so it also holds on SQLite."""
    chunk_ids = select(Chunk.id).where(Chunk.resource_id == resource_id)
    item_ids = select(QuizItem.id).where(QuizItem.chunk_id.in_(chunk_ids))
    session.exec(delete(ReviewState).where(ReviewState.quiz_item_id.in_(item_ids)))
    session.exec(delete(QuizItem).where(QuizItem.chunk_id.in_(chunk_ids)))
    session.exec(delete(Chunk).where(Chunk.resource_id == resource_id))


def _progress(session: Session, resource_id) -> tuple[int, int]:
    """(review states, chunks) built from a resource: what retiring it loses."""
    chunk_ids = select(Chunk.id).where(Chunk.resource_id == resource_id)
    item_ids = select(QuizItem.id).where(QuizItem.chunk_id.in_(chunk_ids))
    reviews = session.exec(
        select(func.count()).select_from(ReviewState)
        .where(ReviewState.quiz_item_id.in_(item_ids))
    ).one()
    chunks = session.exec(
        select(func.count()).select_from(Chunk).where(Chunk.resource_id == resource_id)
    ).one()
    return reviews, chunks


def _pick_copy(session: Session, rows: list[Resource], topic_id) -> Resource | None:
    """The copy to keep among a resource's rows: the most review progress,
    then the most derived chunks, then the one already in topic_id."""
    if len(rows) <= 1:
        return rows[0] if rows else None
    return max(rows, key=lambda x: (*_progress(session, x.id), x.topic_id == topic_id))


def _retire(session: Session, resource: Resource) -> None:
    _purge_derived(session, resource.id)
    session.delete(resource)


def _legacy_hash_matches(adapter, data: ResourceData, legacy: str) -> bool | None:
    """Whether the file's current bytes still hash to a pre-fingerprint
    content hash. None when that can't be checked right now."""
    fetch = getattr(adapter, "fetch_content", None)
    if fetch is None:
        return None
    try:
        blob = fetch(data)
    except Exception:
        return None
    return hashlib.sha256(blob).hexdigest() == legacy


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo on read; compare everything as aware UTC.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def sync_course(
    session: Session, adapter: SourceAdapter, course_source_id: str, user_id,
    course_data: CourseData | None = None,
) -> SyncStats:
    stats = SyncStats()
    source = adapter.source

    if course_data is None:
        course_data = next(
            (c for c in adapter.fetch_courses() if c.source_id == course_source_id), None
        )
    if course_data is None:
        raise ValueError(f"course {course_source_id!r} not found in source {source!r}")
    course, is_new = _upsert_course(session, source, user_id, course_data)
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

    # Resources are matched per course, not per topic: a material the source
    # moved to another topic is moved here too (keeping its derived data)
    # instead of being inserted again beside the old row.
    by_source_id: dict[str, list[Resource]] = {}
    for row in session.exec(
        select(Resource).join(Topic).where(Topic.course_id == course.id)
    ):
        by_source_id.setdefault(row.source_id, []).append(row)
    kept: set[Any] = set()

    for topic_source_id, topic_id in topic_id_by_source.items():
        for r in adapter.fetch_resources(course_source_id, topic_source_id):
            digest = content_hash(r)
            rows = by_source_id.get(r.source_id, [])
            existing = _pick_copy(session, [x for x in rows if x.id not in kept], topic_id)
            moved = existing is not None and existing.topic_id != topic_id
            if moved:
                # A leftover copy may already sit in the destination topic with
                # less progress; retire it first so the move doesn't collide
                # with the (topic_id, source_id) unique constraint.
                for blocking in [x for x in rows
                                 if x.topic_id == topic_id and x.id not in kept]:
                    _retire(session, blocking)
                    rows.remove(blocking)
                    stats.resources_removed += 1
                session.commit()
                existing.topic_id = topic_id
            if existing is not None:
                kept.add(existing.id)
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
                continue

            meta = {"title": r.title, "raw_url": r.raw_url, "type": r.type,
                    "mime_type": r.mime_type}
            meta_changed = any(getattr(existing, k) != v for k, v in meta.items())
            # no digest = content unknown this run (e.g. fetch failed): keep it
            hash_changed = digest is not None and existing.content_hash != digest
            if (hash_changed and digest.startswith(FINGERPRINT_PREFIX)
                    and existing.content_hash
                    and not existing.content_hash.startswith(FINGERPRINT_PREFIX)):
                # One-time move from a legacy full-content hash: verify against
                # the file's bytes. Same bytes -> adopt the fingerprint and keep
                # derived data; different -> normal reset; unverifiable (e.g.
                # download failed) -> keep the legacy hash and retry next sync.
                matches = _legacy_hash_matches(adapter, r, existing.content_hash)
                if matches is not False:
                    if matches:
                        existing.content_hash = digest
                        session.add(existing)
                        session.commit()
                    hash_changed = False

            if hash_changed:
                _purge_derived(session, existing.id)
                for k, v in meta.items():
                    setattr(existing, k, v)
                existing.content_hash = digest
                existing.extracted_text = r.text
                existing.status = "extracted" if r.text else "pending"
                existing.error = None
                existing.attempts = 0  # new content: no backoff carried over
                existing.retry_after = None
                session.add(existing)
                session.commit()
                stats.resources_updated += 1
            elif meta_changed or moved:
                for k, v in meta.items():
                    setattr(existing, k, v)
                session.add(existing)
                session.commit()
                stats.resources_updated += 1
            else:
                stats.resources_skipped += 1

    # Copies of a resource the source still lists, left under other topics by
    # syncs before resources could move: retire them so each shows up once.
    for source_id, rows in by_source_id.items():
        if not any(x.id in kept for x in rows):
            continue  # not listed this sync; leave it alone
        for stale in rows:
            if stale.id not in kept:
                _retire(session, stale)
                stats.resources_removed += 1
    session.commit()

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
        elif (existing.title, existing.topic_id, _as_utc(existing.due_date),
              existing.description) != (a.title, topic_id, _as_utc(a.due_date),
                                        a.description):
            existing.title = a.title
            existing.topic_id = topic_id
            existing.due_date = a.due_date
            existing.description = a.description
            session.add(existing)
            session.commit()
            stats.assignments_updated += 1

    return stats


def sync_all(session: Session, adapter: SourceAdapter, user_id) -> dict[str, SyncStats]:
    return {c.source_id: sync_course(session, adapter, c.source_id, user_id, c)
            for c in adapter.fetch_courses()}
