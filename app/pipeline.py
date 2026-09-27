"""Phase 2 pipeline: extract pending resources, then chunk extracted ones.

Usage: python -m app.pipeline --source moodle [--course ID]
       [--extract-only] [--chunk-only]
Chunking needs LLM_* in .env; extraction runs without it.
"""

from __future__ import annotations

import argparse

from sqlmodel import Session, select

from app.chunk import chunk_resource
from app.config import settings
from app.db import engine
from app.extract import ExtractError, SkipResource, extract_resource_text
from app.llm import QuotaExhaustedError
from app.models import Chunk, Course, QuizItem, Resource, Topic, User
from app.quiz import chunk_needs_quiz, generate_for_chunk


def pending_resources(session: Session, course_id=None):
    q = select(Resource).where(Resource.status == "pending")
    resources = session.exec(q).all()
    if course_id is None:
        return resources
    topic_ids = {
        t.id for t in session.exec(select(Topic).where(Topic.course_id == course_id)).all()
    }
    return [r for r in resources if r.topic_id in topic_ids]


def chunkable_resources(session: Session, course_id=None):
    q = select(Resource).where(Resource.extracted_text.is_not(None))
    resources = session.exec(q).all()
    if course_id is None:
        return resources
    topic_ids = {
        t.id for t in session.exec(select(Topic).where(Topic.course_id == course_id)).all()
    }
    return [r for r in resources if r.topic_id in topic_ids]


def moodle_downloader_for(session: Session):
    """Per-resource Moodle downloader acting as the course owner.

    Returns a function resource -> downloader, or None when the owner has no
    usable token (their file resources then stay pending until they connect).
    Legacy courses without an owner use the global MOODLE_TOKEN.
    """
    from app.moodle import MoodleClient
    from app.moodle_tokens import token_for

    cache: dict = {}

    def for_resource(r: Resource):
        topic = session.get(Topic, r.topic_id)
        course = session.get(Course, topic.course_id) if topic else None
        owner_id = course.user_id if course else None
        if owner_id not in cache:
            user = session.get(User, owner_id) if owner_id else None
            token = token_for(user) if user else settings.moodle_token
            cache[owner_id] = (
                MoodleClient(settings.moodle_base_url, token).download if token else None
            )
        return cache[owner_id]

    return for_resource


def run_extraction(session: Session, downloader, course_id=None, downloader_for=None) -> dict:
    counts = {"extracted": 0, "skipped": 0, "failed": 0}
    resources = pending_resources(session, course_id)
    for i, r in enumerate(resources, 1):
        dl = downloader_for(r) if downloader_for else downloader
        if dl is None and r.type == "file" and downloader_for:
            # owner hasn't connected Moodle: leave pending, retry once they do
            counts["no_token"] = counts.get("no_token", 0) + 1
            continue
        try:
            r.extracted_text = extract_resource_text(r, dl)
            r.status = "extracted"
            r.error = None
            counts["extracted"] += 1
        except SkipResource:
            r.status = "skipped"
            r.error = None
            counts["skipped"] += 1
        except ExtractError as e:
            r.status = "failed"
            r.error = str(e)[:500]
            counts["failed"] += 1
        session.add(r)
        session.commit()
        print(f"  extract {i}/{len(resources)} {r.status}: {r.title[:60]}", flush=True)
    return counts


def run_chunking(session: Session, llm, course_id=None, pace: float = 4.0) -> dict:
    counts = {"chunks": 0, "cached": 0, "resources": 0}
    resources = chunkable_resources(session, course_id)
    for i, r in enumerate(resources, 1):
        n_existing = len(
            session.exec(select(Chunk).where(Chunk.resource_id == r.id)).all()
        )
        try:
            n = chunk_resource(session, r, llm)
        except QuotaExhaustedError as e:
            print(f"  quota exhausted, stopping run (resumable): {str(e)[:120]}",
                  flush=True)
            counts["quota_exhausted"] = True
            break
        except Exception as e:
            counts["errors"] = counts.get("errors", 0) + 1
            print(f"  error on resource {r.id}: {str(e)[:120]}", flush=True)
            continue
        counts["chunks"] += n
        print(f"  chunk {i}/{len(resources)} +{n}: {r.title[:60]}", flush=True)
        if n == 0 and n_existing:
            counts["cached"] += 1
        elif n:
            counts["resources"] += 1
            if pace:
                import time

                time.sleep(pace)
    return counts


def run_quiz(session: Session, llm, course_id=None, attempt: int = 1,
             pace: float = 8.0) -> dict:
    from uuid import UUID

    from app.notify import course_of_chunk, enqueue_new_material

    counts = {"items": 0, "chunks": 0, "skipped": 0}
    per_course: dict[str, int] = {}
    q = select(Chunk)
    chunks = session.exec(q).all()
    if course_id is not None:
        topic_ids = {
            t.id for t in session.exec(select(Topic).where(Topic.course_id == course_id)).all()
        }
        res_ids = {
            r.id for r in session.exec(select(Resource)).all() if r.topic_id in topic_ids
        }
        chunks = [c for c in chunks if c.resource_id in res_ids]
    for chunk in chunks:
        if not chunk_needs_quiz(session, chunk.id, attempt):
            counts["skipped"] += 1
            continue
        try:
            items = generate_for_chunk(session, chunk, llm, attempt)
        except QuotaExhaustedError as e:
            print(f"  quota exhausted, stopping run (resumable): {str(e)[:120]}",
                  flush=True)
            counts["quota_exhausted"] = True
            break
        except Exception as e:
            counts["errors"] = counts.get("errors", 0) + 1
            print(f"  error on chunk {chunk.id}: {str(e)[:120]}", flush=True)
            continue
        counts["items"] += len(items)
        counts["chunks"] += 1
        print(f"  +{len(items)} items ({counts['chunks']}/{len(chunks)} chunks)",
              flush=True)
        if items:
            course = course_of_chunk(session, chunk)
            if course is not None:
                per_course[str(course.id)] = per_course.get(str(course.id), 0) + len(items)
        if pace:
            import time

            time.sleep(pace)
    for cid, n in per_course.items():
        enqueue_new_material(session, UUID(cid), n)
        counts["events"] = counts.get("events", 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], default="moodle")
    parser.add_argument("--course", default=None)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--chunk-only", action="store_true")
    parser.add_argument("--quiz-only", action="store_true")
    parser.add_argument("--pace", type=float, default=8.0,
                        help="seconds between LLM calls (free-tier rate limits)")
    args = parser.parse_args()

    downloader = None
    if args.source != "moodle":
        def downloader(_url):
            raise ExtractError("classroom drive download needs a drive scope (v1 gap)")

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
        course_id = None
        if args.course:
            course = session.exec(
                select(Course).where(
                    Course.source == args.source, Course.source_id == args.course
                )
            ).first()
            if course is None:
                raise SystemExit(f"course {args.course} not synced — run sync_cli first")
            course_id = course.id
        if not args.chunk_only and not args.quiz_only:
            downloader_for = moodle_downloader_for(session) if args.source == "moodle" else None
            print("extraction:", run_extraction(session, downloader, course_id, downloader_for))
        if not args.extract_only and not args.quiz_only:
            print("chunking:", run_chunking(session, chunk_llm, course_id))
        if not args.extract_only and not args.chunk_only:
            print("quiz:", run_quiz(session, quiz_llm, course_id, pace=args.pace))


if __name__ == "__main__":
    main()
