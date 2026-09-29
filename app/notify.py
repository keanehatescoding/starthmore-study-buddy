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
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlmodel import Session, select

from app.grade import due_count
from app.models import Course, NotificationEvent, Resource, Topic, User

REVIEW_DUE_THRESHOLD = 3
RESEND_BATCH_URL = "https://api.resend.com/emails/batch"
BATCH_SIZE = 100  # Resend's per-request batch limit
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0  # longer waits give up; the next worker pass retries


class EmailError(RuntimeError):
    """`reason` is a short fixed-vocabulary token, safe to persist in job results
    (no provider message text, which can echo addresses)."""

    def __init__(self, message: str, reason: str = "error"):
        super().__init__(message)
        self.reason = reason


class RateLimitedError(EmailError):
    """Resend kept answering 429; stop and leave events queued."""

    def __init__(self, message: str):
        super().__init__(message, "rate_limited")


def _retry_after(err: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait: Retry-After as delta-seconds or HTTP-date, else 2**attempt."""
    value = (err.headers.get("Retry-After") or "").strip() if err.headers else ""
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return float(2 ** attempt)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _http_reason(err: urllib.error.HTTPError) -> str:
    """`http_<code>[:<resend error name>]`, e.g. http_422:validation_error."""
    name = None
    try:
        body = json.loads(err.read(2048) or b"{}")
        name = body.get("name") if isinstance(body, dict) else None
    except (OSError, ValueError):  # unreadable or non-JSON body: status only
        pass
    if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,40}", name):
        return f"http_{err.code}:{name}"
    return f"http_{err.code}"


def send_batch(api_key: str, emails: list[dict], idempotency_key: str,
               sleep=time.sleep) -> None:
    """POST up to BATCH_SIZE emails in one request; retries 429 with backoff.

    Resend dedupes on Idempotency-Key for 24h, so resending the same batch
    with the same key after a lost response doesn't deliver twice.
    """
    if not api_key:
        raise EmailError("RESEND_API_KEY is empty — set it in .env", "no_api_key")
    req = urllib.request.Request(
        RESEND_BATCH_URL,
        data=json.dumps(emails).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": idempotency_key,
            # Resend's edge filter 403s the default urllib agent
            "User-Agent": "study-buddy/0.1",
        },
        method="POST",
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201, 202):
                    raise EmailError(f"resend returned {resp.status}",
                                     f"http_{resp.status}")
                return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                reason = _http_reason(e)
                raise EmailError(f"resend returned {reason}", reason) from e
            wait = _retry_after(e, attempt)
            if attempt == MAX_RETRIES or wait > MAX_RETRY_WAIT:
                raise RateLimitedError(f"resend rate limit (retry after {wait:.0f}s)") from e
            sleep(wait)
        except EmailError:
            raise
        except Exception as e:
            raise EmailError(f"send failed: {e}", f"network:{type(e).__name__}") from e


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
    raise EmailError(f"unknown event type {event.type!r}", "unknown_event_type")


def recipient_for(session: Session, event: NotificationEvent, fallback: str = "") -> str:
    """Per-user recipient: the event owner's email, else the fallback."""
    if event.user_id is not None:
        user = session.get(User, event.user_id)
        if user is not None and user.email:
            return user.email
    return fallback


# Resend rejected the batch before sending anything: safe to split and resend.
BATCH_REJECTED = "http_422:validation_error"


def send_pending(
    session: Session, api_key: str, from_addr: str, fallback_to: str = "",
    base_url: str | None = None, sleep=time.sleep,
) -> dict:
    """Send all unsent events, each to its owner's email, BATCH_SIZE per request.

    One commit per delivered batch. Failures stay queued; a rate limit that
    outlasts the retries stops the run so the rest wait for the next pass.
    `errors` counts failures by EmailError.reason for the job result.

    Duplicate safety: a multi-email batch gets a batch_key, committed before
    the request and sent as its Idempotency-Key. A failure that might have
    been delivered (network, 5xx, ...) leaves the batch as-is, so later passes
    resend the same events under the same key. Only a confirmed validation
    rejection splits it into single sends (key "event-<id>"). Resend keeps
    keys for 24h; a batch still unconfirmed after that may be delivered twice.
    """
    sent = 0
    errors: Counter[str] = Counter()  # reason token -> failed deliveries
    events = session.exec(
        select(NotificationEvent)
        .where(NotificationEvent.sent == False)  # noqa: E712
        .order_by(NotificationEvent.created_at)
    ).all()
    if not api_key:  # nothing can be delivered; don't fake-mark or split batches
        errors["no_api_key"] = len(events)
        return _result(sent, errors)
    keyed: dict[str, list] = {}  # batch_key -> earlier batch, resent unchanged
    fresh: list = []
    for event in events:
        to_addr = recipient_for(session, event, fallback_to)
        try:
            if not to_addr:
                raise EmailError("no recipient", "no_recipient")
            subject, body = render(event, base_url)
        except EmailError as e:
            errors[e.reason] += 1
            continue
        item = (event, {"from": from_addr, "to": [to_addr],
                        "subject": subject, "text": body})
        if event.batch_key:
            keyed.setdefault(event.batch_key, []).append(item)
        else:
            fresh.append(item)
    pending = [*keyed.values(),
               *(fresh[i:i + BATCH_SIZE] for i in range(0, len(fresh), BATCH_SIZE))]
    while pending:
        batch = pending.pop(0)
        try:
            _deliver(session, api_key, batch, sleep)
            sent += len(batch)
        except RateLimitedError as e:
            errors[e.reason] += len(batch) + sum(len(b) for b in pending)
            break
        except EmailError as e:
            if len(batch) > 1 and e.reason == BATCH_REJECTED:
                # all-or-nothing validation: nothing went out, so one bad
                # address must not hold back the rest; retry one at a time
                for event, _ in batch:
                    event.batch_key = None
                    session.add(event)
                session.commit()
                pending[:0] = [[one] for one in batch]
            else:
                errors[e.reason] += len(batch)
    return _result(sent, errors)


def _result(sent: int, errors: Counter) -> dict:
    return {"sent": sent, "failed": sum(errors.values()), "errors": dict(errors)}


def _deliver(session: Session, api_key: str, batch, sleep) -> None:
    key = batch[0][0].batch_key
    if key is None and len(batch) == 1:
        key = f"event-{batch[0][0].id}"  # stable across passes, nothing to store
    elif key is None:
        key = f"batch-{uuid.uuid4()}"
        for event, _ in batch:
            event.batch_key = key
            session.add(event)
        session.commit()  # before the request: a lost response must reuse it
    send_batch(api_key, [email for _, email in batch], key, sleep=sleep)
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
