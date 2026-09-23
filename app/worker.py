"""Background worker: send queued notifications + daily review-due check.

Usage: python -m app.worker [--loop SECONDS]
One pass = check review-due for every user, then send all unsent events.
Schedule with cron (daily) or run --loop for a persistent worker.
"""

from __future__ import annotations

import argparse
import time
import urllib.request

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.jobs import run_due
from app.models import User
from app.notify import check_review_due, send_pending


def ping_healthcheck() -> None:
    """Notify a dead-man's-switch monitor (e.g. healthchecks.io) on success.
    Monitoring must never fail the run."""
    url = settings.healthcheck_ping_url
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception:
        pass


def run_once() -> dict:
    summary: dict = {"review_due_events": 0}
    with Session(engine) as session:
        summary["jobs"] = run_due(session)  # queued sync/notify jobs first
        for user in session.exec(select(User)).all():
            if check_review_due(session, user.id) is not None:
                summary["review_due_events"] += 1
        summary["send"] = send_pending(
            session, settings.resend_api_key, settings.email_from,
            settings.email_to,  # fallback only; owned events go to User.email
        )
    ping_healthcheck()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                        help="repeat every N seconds (0 = single pass)")
    args = parser.parse_args()
    while True:
        print(run_once(), flush=True)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
