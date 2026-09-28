"""Background worker: drain the job queue, including one notify pass.

Usage: python -m app.worker [--loop SECONDS]
One pass = enqueue a send_notifications job (unless one is already queued),
then run due jobs until none are left. Queued syncs run before the notify
job, so their new-material events go out in the same pass.
Schedule with cron (daily) or run --loop for a persistent worker.
"""

from __future__ import annotations

import argparse
import time
import urllib.request

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.jobs import enqueue, run_due
from app.models import Job


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
    totals: dict = {"completed": 0, "failed": 0, "retried": 0}
    with Session(engine) as session:
        queued = session.exec(
            select(Job).where(
                Job.type == "send_notifications",
                Job.status.in_(("pending", "running")),
            )
        ).first()
        if queued is None:
            enqueue(session, "send_notifications", max_attempts=1)
        while True:  # drain; failed jobs back off, so this terminates
            batch = run_due(session)
            for key, n in batch.items():
                totals[key] = totals.get(key, 0) + n
            if not any(batch.values()):
                break
    ping_healthcheck()
    return totals


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
