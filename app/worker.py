"""Background worker: drain the job queue, including one notify pass.

Usage: python -m app.worker [--loop SECONDS]
One pass = reap orphaned jobs, enqueue a send_notifications job (unless one
is already queued), then run due jobs until none are left. Due syncs are
claimed before the notify job, so their new-material events go out in the
same pass.
Schedule with cron (daily) or run --loop for a persistent worker.
"""

from __future__ import annotations

import argparse
import time
import urllib.request

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.jobs import enqueue, reap_stale, run_due


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
        # Reap first: a notify job orphaned by a crashed worker would
        # otherwise block this pass's enqueue and then be failed unrun.
        reaped = reap_stale(session)
        if reaped:
            totals["reaped"] = reaped
        try:
            enqueue(session, "send_notifications", max_attempts=1)
        except IntegrityError:  # uq_jobs_active_notify: one is already queued
            session.rollback()
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
