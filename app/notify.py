"""Notifications (Phase 6): batched new-material + threshold review-due, via email.

- Generation (what to send) is decoupled from delivery: NotificationEvent
  rows with sent=False ARE the queue; the worker sends them.
- new_material: one event per course per generation run that produced items
  ("8 new quiz items from CS 301"), never per-item.
- review_due: created only when due count >= threshold (avoids fatigue),
  and never duplicated while an unsent one exists.
- Delivery: Resend REST API (stdlib only, free tier). No key -> events stay
  queued; nothing is fake-marked sent.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.grade import due_items
from app.models import Chunk, Course, NotificationEvent, Resource, Topic, User

REVIEW_DUE_THRESHOLD = 3


class EmailError(RuntimeError):
    pass


def send_email(api_key: str, from_addr: str, to_addr: str, subject: str, body: str) -> None:
    if not api_key:
        raise EmailError("RESEND_API_KEY is empty — set it in .env")
    payload = json.dumps(
        {"from": from_addr, "to": [to_addr], "subject": subject, "text": body}
    ).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # Resend's edge filter 403s the default urllib agent
            "User-Agent": "study-buddy/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201, 202):
                raise EmailError(f"resend returned {resp.status}")
    except EmailError:
        raise
    except Exception as e:
        raise EmailError(f"send failed: {e}") from e


def enqueue_new_material(session: Session, course_id, new_items: int) -> NotificationEvent | None:
    """One batched event per course. Returns None when nothing new."""
    if new_items <= 0:
        return None
    course = session.get(Course, course_id)
    event = NotificationEvent(
        user_id=_first_user_id(session),
        type="new_material",
        payload={"course": course.name if course else str(course_id),
                 "code": course.code if course else None,
                 "new_items": new_items},
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def _first_user_id(session: Session):
    user = session.exec(select(User)).first()
    if user is None:
        from app.stats import get_or_create_user

        user = get_or_create_user(session)
    return user.id


def check_review_due(
    session: Session, user_id, threshold: int = REVIEW_DUE_THRESHOLD
) -> NotificationEvent | None:
    """Create a review_due event if due >= threshold and none unsent exists."""
    due = due_items(session, user_id, limit=1000)
    if len(due) < threshold:
        return None
    pending = session.exec(
        select(NotificationEvent).where(
            NotificationEvent.user_id == user_id,
            NotificationEvent.type == "review_due",
            NotificationEvent.sent == False,  # noqa: E712
        )
    ).first()
    if pending:
        return None
    event = NotificationEvent(
        user_id=user_id, type="review_due", payload={"due_count": len(due)}
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def render(event: NotificationEvent) -> tuple[str, str]:
    if event.type == "new_material":
        p = event.payload
        return (
            f"New study material: {p.get('code') or p.get('course')}",
            f"{p['new_items']} new quiz items from {p.get('course')}.\n"
            f"Review them: http://localhost:8000/review",
        )
    if event.type == "review_due":
        n = event.payload.get("due_count", 0)
        return (
            f"{n} reviews due",
            f"You have {n} quiz items due for review.\n"
            f"Catch up: http://localhost:8000/review",
        )
    raise EmailError(f"unknown event type {event.type!r}")


def send_pending(session: Session, api_key: str, from_addr: str, to_addr: str) -> dict:
    """Send all unsent events. Failures stay queued for the next pass."""
    counts = {"sent": 0, "failed": 0}
    events = session.exec(
        select(NotificationEvent).where(NotificationEvent.sent == False)  # noqa: E712
    ).all()
    for event in events:
        try:
            subject, body = render(event)
            send_email(api_key, from_addr, to_addr, subject, body)
        except EmailError:
            counts["failed"] += 1
            continue
        event.sent = True
        event.sent_at = datetime.now(timezone.utc)
        session.add(event)
        session.commit()
        counts["sent"] += 1
    return counts


def course_of_chunk(session: Session, chunk) -> object | None:
    resource = session.get(Resource, chunk.resource_id)
    topic = session.get(Topic, resource.topic_id) if resource else None
    return session.get(Course, topic.course_id) if topic else None
