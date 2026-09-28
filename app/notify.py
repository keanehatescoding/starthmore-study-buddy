"""Notifications (Phase 6): batched new-material + threshold review-due, via email.

- Generation (what to send) is decoupled from delivery: NotificationEvent
  rows with sent=False ARE the queue; the worker sends them.
- new_material: one event per course per generation run that produced items
  ("8 new quiz items from CS 301"), never per-item.
- review_due: created only when due count >= threshold (avoids fatigue),
  and never duplicated while an unsent one exists.
- Delivery: Resend batch REST API (stdlib only, free tier), up to 100 emails
  per request, backing off on 429. No key -> events stay queued; nothing is
  fake-marked sent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.grade import due_count
from app.models import Course, NotificationEvent, Resource, Topic, User

REVIEW_DUE_THRESHOLD = 3
RESEND_BATCH_URL = "https://api.resend.com/emails/batch"
BATCH_SIZE = 100  # Resend's per-request batch limit
MAX_RETRIES = 4


class EmailError(RuntimeError):
    pass


class RateLimitedError(EmailError):
    """Resend kept answering 429 after all retries; stop and leave events queued."""


def _retry_after(err: urllib.error.HTTPError, attempt: int) -> float:
    try:
        return max(0.0, float(err.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return float(2 ** attempt)


def send_batch(api_key: str, emails: list[dict], sleep=time.sleep) -> None:
    """POST up to BATCH_SIZE emails in one request; retries 429 with backoff."""
    if not api_key:
        raise EmailError("RESEND_API_KEY is empty — set it in .env")
    req = urllib.request.Request(
        RESEND_BATCH_URL,
        data=json.dumps(emails).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # Resend's edge filter 403s the default urllib agent
            "User-Agent": "study-buddy/0.1",
        },
        method="POST",
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201, 202):
                    raise EmailError(f"resend returned {resp.status}")
                return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise EmailError(f"resend returned {e.code}") from e
            if attempt == MAX_RETRIES:
                raise RateLimitedError("resend rate limit, retries exhausted") from e
            sleep(_retry_after(e, attempt))
        except EmailError:
            raise
        except Exception as e:
            raise EmailError(f"send failed: {e}") from e


def enqueue_new_material(session: Session, course_id, new_items: int) -> NotificationEvent | None:
    """One batched event per course, owned by the course owner.

    Returns None when nothing is new or the course has no owner: there is
    nobody to tell, and guessing a recipient would misattribute the course.
    """
    if new_items <= 0:
        return None
    course = session.get(Course, course_id)
    if course is None or course.user_id is None:
        return None
    event = NotificationEvent(
        user_id=course.user_id,
        type="new_material",
        payload={"course": course.name, "code": course.code, "new_items": new_items},
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def check_review_due(
    session: Session, user_id, threshold: int = REVIEW_DUE_THRESHOLD
) -> NotificationEvent | None:
    """Create a review_due event if due >= threshold and none unsent exists."""
    due = due_count(session, user_id)
    if due < threshold:
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
        user_id=user_id, type="review_due", payload={"due_count": due}
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def render(event: NotificationEvent, base_url: str | None = None) -> tuple[str, str]:
    if base_url is None:
        from app.config import settings

        base_url = settings.app_base_url
    review_url = f"{base_url.rstrip('/')}/review"
    if event.type == "new_material":
        p = event.payload
        return (
            f"New study material: {p.get('code') or p.get('course')}",
            f"{p['new_items']} new quiz items from {p.get('course')}.\n"
            f"Review them: {review_url}",
        )
    if event.type == "review_due":
        n = event.payload.get("due_count", 0)
        return (
            f"{n} reviews due",
            f"You have {n} quiz items due for review.\n"
            f"Catch up: {review_url}",
        )
    raise EmailError(f"unknown event type {event.type!r}")


def recipient_for(session: Session, event: NotificationEvent, fallback: str = "") -> str:
    """Per-user recipient: the event owner's email, else the fallback."""
    if event.user_id is not None:
        user = session.get(User, event.user_id)
        if user is not None and user.email:
            return user.email
    return fallback


def send_pending(
    session: Session, api_key: str, from_addr: str, fallback_to: str = "",
    base_url: str | None = None, sleep=time.sleep,
) -> dict:
    """Send all unsent events, each to its owner's email, BATCH_SIZE per request.

    One commit per delivered batch. Failures stay queued; a rate limit that
    outlasts the retries stops the run so the rest wait for the next pass.
    """
    counts = {"sent": 0, "failed": 0}
    events = session.exec(
        select(NotificationEvent)
        .where(NotificationEvent.sent == False)  # noqa: E712
        .order_by(NotificationEvent.created_at)
    ).all()
    if not api_key:  # nothing can be delivered; don't fake-mark or split batches
        return {"sent": 0, "failed": len(events)}
    ready: list[tuple[NotificationEvent, dict]] = []
    for event in events:
        to_addr = recipient_for(session, event, fallback_to)
        try:
            if not to_addr:
                raise EmailError("no recipient")
            subject, body = render(event, base_url)
        except EmailError:
            counts["failed"] += 1
            continue
        ready.append((event, {"from": from_addr, "to": [to_addr],
                              "subject": subject, "text": body}))
    pending = [ready[i:i + BATCH_SIZE] for i in range(0, len(ready), BATCH_SIZE)]
    while pending:
        batch = pending.pop(0)
        try:
            _deliver(session, api_key, batch, sleep)
            counts["sent"] += len(batch)
        except RateLimitedError:
            counts["failed"] += len(batch) + sum(len(b) for b in pending)
            break
        except EmailError:
            if len(batch) == 1:
                counts["failed"] += 1
            else:
                # batch validation is all-or-nothing: one bad address must not
                # hold back the rest, so retry this batch one email at a time
                pending[:0] = [[one] for one in batch]
    return counts


def _deliver(session: Session, api_key: str, batch, sleep) -> None:
    send_batch(api_key, [email for _, email in batch], sleep=sleep)
    now = datetime.now(timezone.utc)
    for event, _ in batch:
        event.sent = True
        event.sent_at = now
        session.add(event)
    session.commit()


def course_of_chunk(session: Session, chunk) -> object | None:
    resource = session.get(Resource, chunk.resource_id)
    topic = session.get(Topic, resource.topic_id) if resource else None
    return session.get(Course, topic.course_id) if topic else None
