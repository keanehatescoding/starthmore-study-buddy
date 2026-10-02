"""Phase 2 pipeline: extract pending resources, then chunk extracted ones.

Usage: python -m app.pipeline --source moodle [--course ID]
       [--extract-only] [--chunk-only]
Chunking needs LLM_* in .env; extraction runs without it.

A resource whose download or chunking fails is deferred with exponential
backoff (Resource.attempts / retry_after, 1h doubling to 24h) rather than
retried on every run; chunking gives up ("failed") after MAX_CHUNK_ATTEMPTS.

Each stage first copies another user's results for the same material
(app.share), counted as "shared", and only then downloads or calls the LLM.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, or_, select

from app.chunk import chunk_resource, needs_llm
from app.config import settings
from app.db import engine
from app.drive import DriveError
from app.extract import ExtractError, SkipResource, extract_resource_text
from app.llm import QuotaExhaustedError
from app.models import Chunk, Course, Resource, Topic, User
from app.moodle import ForeignURLError, MoodleError
from app.quiz import chunk_needs_quiz, generate_for_chunk
from app.share import copy_chunks, copy_extraction, copy_quiz


@dataclass
class StageResult:
    """Integer tallies per outcome; a quota stop is a separate flag."""

    counts: Counter[str] = field(default_factory=Counter)
    quota_exhausted: bool = False

    def __str__(self) -> str:
        out = ", ".join(f"{k}={v}" for k, v in self.counts.items())
        return out + (" (quota_exhausted: stopped, re-run to resume)"
                      if self.quota_exhausted else "")


def _scoped(q, course_id, source):
    """Restrict a Resource-joinable query to one course and/or source (None = all).

    The source filter matters for extraction: a Moodle downloader must never
    see a Classroom URL, since it appends the Moodle token to whatever it fetches.
    """
    if source is not None:
        q = q.where(Resource.source == source)
    if course_id is None:
        return q
    return q.join(Topic, Topic.id == Resource.topic_id).where(Topic.course_id == course_id)


MAX_CHUNK_ATTEMPTS = 3
RETRY_BASE = timedelta(hours=1)
RETRY_CAP = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _defer(r: Resource, error: str) -> None:
    """Count a failed try and push the next one out: 1h, 2h, 4h ... 24h."""
    r.attempts = (r.attempts or 0) + 1
    r.retry_after = _now() + min(RETRY_BASE * 2 ** (r.attempts - 1), RETRY_CAP)
    r.error = error[:500]


def _succeeded(r: Resource) -> None:
    r.attempts = 0
    r.retry_after = None


def _due():
    return or_(Resource.retry_after.is_(None), Resource.retry_after <= _now())


def pending_resource_ids(session: Session, course_id=None, source=None) -> list:
    q = (select(Resource.id).where(Resource.status == "pending", _due())
         .order_by(Resource.id))
    return session.exec(_scoped(q, course_id, source)).all()


def chunkable_resource_ids(session: Session, course_id=None, source=None) -> list:
    # ids only: loading every extracted_text up front is what blew memory
    q = (select(Resource.id)
         .where(Resource.extracted_text.is_not(None),
                Resource.status.not_in(["failed", "skipped"]), _due())
         .order_by(Resource.id))
    return session.exec(_scoped(q, course_id, source)).all()


def quiz_chunk_ids(session: Session, course_id=None, source=None) -> list:
    q = (select(Chunk.id).join(Resource, Resource.id == Chunk.resource_id)
         .order_by(Chunk.resource_id, Chunk.order))
    return session.exec(_scoped(q, course_id, source)).all()


def _owner_downloader_for(session: Session, make):
    """Per-resource downloader acting as the course owner: `make(user)`
    returns one, or None when the owner has no usable token (their file
    resources then stay pending until they connect). Unowned pre-auth
    courses get none: shared tokens belong to their configured owner alone.
    """
    cache: dict = {}

    def for_resource(r: Resource):
        topic = session.get(Topic, r.topic_id)
        course = session.get(Course, topic.course_id) if topic else None
        owner_id = course.user_id if course else None
        if owner_id not in cache:
            user = session.get(User, owner_id) if owner_id else None
            cache[owner_id] = make(user) if user else None
        return cache[owner_id]

    return for_resource


def moodle_downloader_for(session: Session):
    from app.moodle import MoodleClient
    from app.moodle_tokens import token_for

    def make(user):
        token = token_for(user)
        return MoodleClient(settings.moodle_base_url, token).download if token else None

    return _owner_downloader_for(session, make)


def classroom_downloader_for(session: Session):
    """Drive downloads under the owner's Google refresh token (app.drive)."""
    from app.auth import classroom_token_for
    from app.drive import DriveClient, build_service

    def make(user):
        token = classroom_token_for(user)
        if not token:
            return None
        return DriveClient(build_service(
            settings.google_client_id, settings.google_client_secret, token)).download

    return _owner_downloader_for(session, make)


DOWNLOADERS_FOR = {"moodle": moodle_downloader_for, "classroom": classroom_downloader_for}


def run_extraction(session: Session, downloader, course_id=None,
                   downloader_for=None, source=None) -> StageResult:
    result = StageResult(Counter(extracted=0, skipped=0, failed=0))
    counts = result.counts
    ids = pending_resource_ids(session, course_id, source)
    for i, rid in enumerate(ids, 1):
        r = session.get(Resource, rid)
        dl = downloader_for(r) if downloader_for else downloader
        if dl is None and r.type == "file" and downloader_for:
            # owner hasn't connected this source: leave pending, retry once they do
            counts["no_token"] += 1
            continue
        if copy_extraction(session, r):
            _succeeded(r)
            session.add(r)
            session.commit()
            counts["shared"] += 1
            print(f"  extract {i}/{len(ids)} shared: {r.title[:60]}", flush=True)
            continue
        try:
            r.extracted_text = extract_resource_text(r, dl)
            _succeeded(r)
            if r.extracted_text and r.extracted_text.strip():
                r.status = "extracted"
                r.error = None
                counts["extracted"] += 1
            else:  # nothing to chunk or quiz on; don't bill a chunker call
                r.status = "skipped"
                r.error = "no text extracted"
                counts["skipped"] += 1
        except SkipResource:
            r.status = "skipped"
            r.error = None
            counts["skipped"] += 1
        except ForeignURLError as e:
            r.status = "failed"  # not a Moodle file; retrying can't help
            r.error = str(e)[:500]
            counts["failed"] += 1
        except (MoodleError, DriveError) as e:
            # network blip, rejected token or missing Drive grant: keep
            # pending, retried after a backoff instead of on every run
            _defer(r, str(e))
            counts["download_errors"] += 1
        except ExtractError as e:
            r.status = "failed"
            r.error = str(e)[:500]
            counts["failed"] += 1
        except Exception as e:  # parser crash on one bad file must not end the run
            r.status = "failed"
            r.error = f"{type(e).__name__}: {e}"[:500]
            counts["failed"] += 1
        session.add(r)
        session.commit()
        print(f"  extract {i}/{len(ids)} {r.status}: {r.title[:60]}", flush=True)
    return result


def run_chunking(session: Session, llm, course_id=None, pace: float = 0.0,
                 source=None) -> StageResult:
    result = StageResult(Counter(chunks=0, cached=0, resources=0))
    counts = result.counts
    ids = chunkable_resource_ids(session, course_id, source)
    for i, rid in enumerate(ids, 1):
        r = session.get(Resource, rid)
        has_chunks = session.exec(
            select(func.count()).select_from(Chunk).where(Chunk.resource_id == r.id)
        ).one()
        if has_chunks and r.status == "extracted":
            counts["cached"] += 1
            continue
        shared = copy_chunks(session, r)
        if shared:
            if r.attempts or r.retry_after:
                _succeeded(r)
                session.add(r)
                session.commit()
            counts["chunks"] += shared
            counts["resources"] += 1
            counts["shared"] += 1
            print(f"  chunk {i}/{len(ids)} shared +{shared}: {r.title[:60]}", flush=True)
            continue
        if llm is None and needs_llm(r):
            counts["needs_llm"] += 1  # left untouched for a run with LLM_* set
            continue
        try:
            n = chunk_resource(session, r, llm)
        except QuotaExhaustedError as e:
            print(f"  quota exhausted, stopping run (resumable): {str(e)[:120]}",
                  flush=True)
            result.quota_exhausted = True
            break
        except Exception as e:
            session.rollback()
            r = session.get(Resource, rid)
            _defer(r, f"{type(e).__name__}: {e}")
            if r.attempts >= MAX_CHUNK_ATTEMPTS:
                r.status = "failed"  # existing chunks, if any, are kept
            session.add(r)
            session.commit()
            counts["errors"] += 1
            print(f"  error on resource {rid} (try {r.attempts}): {str(e)[:120]}",
                  flush=True)
            continue
        if r.attempts or r.retry_after:
            _succeeded(r)
            session.add(r)
            session.commit()
        counts["chunks"] += n
        print(f"  chunk {i}/{len(ids)} +{n}: {r.title[:60]}", flush=True)
        if n:
            counts["resources"] += 1
            if pace and needs_llm(r):
                time.sleep(pace)
    return result


def run_quiz(session: Session, llm, course_id=None, attempt: int = 1,
             pace: float = 0.0, source=None) -> StageResult:
    from uuid import UUID

    from app.notify import course_of_chunk, enqueue_new_material

    result = StageResult(Counter(items=0, chunks=0, skipped=0))
    counts = result.counts
    per_course: Counter[str] = Counter()
    ids = quiz_chunk_ids(session, course_id, source)
    for chunk_id in ids:
        if not chunk_needs_quiz(session, chunk_id, attempt):
            counts["skipped"] += 1
            continue
        chunk = session.get(Chunk, chunk_id)
        try:
            items = copy_quiz(session, chunk, attempt)
            shared = items is not None
            if shared:
                counts["shared"] += 1
            else:
                items = generate_for_chunk(session, chunk, llm, attempt)
        except QuotaExhaustedError as e:
            print(f"  quota exhausted, stopping run (resumable): {str(e)[:120]}",
                  flush=True)
            result.quota_exhausted = True
            break
        except Exception as e:
            session.rollback()
            counts["errors"] += 1
            print(f"  error on chunk {chunk_id}: {str(e)[:120]}", flush=True)
            continue
        counts["items"] += len(items)
        counts["chunks"] += 1
        print(f"  +{len(items)} items ({counts['chunks']}/{len(ids)} chunks)",
              flush=True)
        if items:
            course = course_of_chunk(session, chunk)
            if course is not None:
                per_course[str(course.id)] += len(items)
        if pace and not shared:
            time.sleep(pace)
    for cid, n in per_course.items():
        if enqueue_new_material(session, UUID(cid), n) is not None:
            counts["events"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], default="moodle")
    parser.add_argument("--course", default=None)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--chunk-only", action="store_true")
    parser.add_argument("--quiz-only", action="store_true")
    parser.add_argument("--pace", type=float, default=settings.llm_pace,
                        help="seconds between LLM calls (default LLM_PACE; "
                             "raise it for free-tier rate limits)")
    args = parser.parse_args()

    chunk_llm = quiz_llm = None
    if not args.extract_only and not args.quiz_only:
        from app.llm import LLMClient

        chunk_llm = LLMClient(
            settings.llm_base_url, settings.llm_api_key, settings.llm_chunk_model
        )
    if not args.extract_only and not args.chunk_only:
        from app.llm import LLMClient

        quiz_llm = LLMClient(
            settings.llm_base_url, settings.llm_api_key, settings.llm_quiz_model
        )

    with Session(engine) as session:
        course_ids = [None]
        if args.course:
            # every user enrolled in the course has their own copy of it
            course_ids = session.exec(
                select(Course.id).where(
                    Course.source == args.source, Course.source_id == args.course,
                    Course.user_id.is_not(None),
                ).order_by(Course.id)
            ).all()
            if not course_ids:
                raise SystemExit(f"course {args.course} not synced — run sync_cli first")
        src = args.source
        for course_id in course_ids:
            if not args.chunk_only and not args.quiz_only:
                downloader_for = DOWNLOADERS_FOR[src](session)
                print("extraction:", run_extraction(session, None, course_id,
                                                    downloader_for, source=src))
            if not args.extract_only and not args.quiz_only:
                print("chunking:", run_chunking(session, chunk_llm, course_id,
                                                pace=args.pace, source=src))
            if not args.extract_only and not args.chunk_only:
                print("quiz:", run_quiz(session, quiz_llm, course_id,
                                        pace=args.pace, source=src))


if __name__ == "__main__":
    main()
