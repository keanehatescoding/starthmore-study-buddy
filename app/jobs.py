"""Postgres-backed job queue (Phase 7 hardening): sync runs go through here
instead of blocking the CLI caller.

- `enqueue(session, type, payload)` -> Job (status pending).
- `run_due(session, limit)` claims the oldest due pending jobs one at a time
  (pending -> running -> completed/failed) and executes them via HANDLERS.
- Failed jobs retry with backoff until max_attempts, then stay failed with
  the error recorded. Nothing is silently dropped.
- Job types: "sync" (Moodle/Classroom course sync), "send_notifications".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models import Job, User

HANDLERS: dict[str, callable] = {}


def handler(job_type: str):
    def deco(fn):
        HANDLERS[job_type] = fn
        return fn

    return deco


def enqueue(
    session: Session, job_type: str, payload: dict | None = None,
    max_attempts: int = 3,
) -> Job:
    job = Job(type=job_type, payload=dict(payload or {}), max_attempts=max_attempts)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; Postgres
    # returns aware ones. Normalize so due-checks work on both.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def run_due(session: Session, limit: int = 5) -> dict:
    """Claim and execute up to `limit` due pending jobs. Returns a summary."""
    summary: dict = {"completed": 0, "failed": 0, "retried": 0}
    now = _utcnow()
    pending = session.exec(
        select(Job).where(Job.status == "pending").order_by(Job.created_at)
    ).all()
    due = [j for j in pending if _as_aware(j.available_at) <= now][:limit]
    for job in due:
        job.status = "running"
        job.attempts += 1
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
        fn = HANDLERS.get(job.type)
        try:
            if fn is None:
                raise ValueError(f"no handler for job type {job.type!r}")
            result = fn(session, job.payload)
            job.status = "completed"
            job.error = None
            job.payload = {**job.payload, "result": result}
            summary["completed"] += 1
        except Exception as e:  # noqa: BLE001 — recorded on the row, never dropped
            job.error = f"{type(e).__name__}: {e}"[:2000]
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                summary["failed"] += 1
            else:
                job.status = "pending"  # retry with backoff
                job.available_at = _utcnow() + timedelta(
                    seconds=60 * job.attempts
                )
                summary["retried"] += 1
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
    return summary


@handler("sync")
def run_sync_job(session: Session, payload: dict) -> dict:
    """payload: {source, course_id|None, user_email}."""
    from app.sync import sync_all, sync_course
    from app.sync_cli import build_adapter

    user = session.exec(
        select(User).where(User.email == payload["user_email"])
    ).first()
    if user is None:
        raise ValueError(f"no such user {payload['user_email']}")
    adapter = build_adapter(payload["source"], user)
    if payload.get("course_id"):
        stats = sync_course(session, adapter, payload["course_id"], user.id)
        return {payload["course_id"]: stats.as_dict()}
    return {
        course_id: stats.as_dict()
        for course_id, stats in sync_all(session, adapter, user.id).items()
    }


@handler("send_notifications")
def run_notify_job(session: Session, payload: dict) -> dict:
    from app.config import settings
    from app.notify import check_review_due, send_pending

    users = session.exec(select(User)).all()
    events = sum(
        1 for u in users if check_review_due(session, u.id) is not None
    )
    send = send_pending(
        session, settings.resend_api_key, settings.email_from,
        settings.email_to,
    )
    return {"review_due_events": events, "send": send}
