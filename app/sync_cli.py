"""Live sync entrypoint: python -m app.sync_cli --source moodle --user EMAIL [--course ID]
                       python -m app.sync_cli --source moodle --all-users --enqueue

Moodle uses the user's own connected token (Settings → Moodle), falling back
to MOODLE_TOKEN only for MOODLE_TOKEN_OWNER (see app.moodle_tokens). Classroom reuses the Google refresh token
stored on the user row at login (falls back to GOOGLE_REFRESH_TOKEN in .env).
"""

from __future__ import annotations

import argparse

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import User


class NotConnectedError(RuntimeError):
    """The user has no credentials for this source (a job failure, not a crash)."""


def build_adapter(source: str, user: User):
    if source == "moodle":
        from app.moodle import MoodleAdapter, MoodleClient
        from app.moodle_tokens import token_for

        token = token_for(user)
        if not token:
            raise NotConnectedError(
                f"{user.email} has not connected Moodle — use Settings → Moodle"
            )
        return MoodleAdapter(MoodleClient(settings.moodle_base_url, token))
    if source == "classroom":
        from app.classroom import ClassroomAdapter, ClassroomClient, build_service

        refresh = user.google_refresh_token or settings.google_refresh_token
        if not refresh:
            raise NotConnectedError("no classroom refresh token — log in via Google first")
        service = build_service(
            settings.google_client_id, settings.google_client_secret, refresh
        )
        return ClassroomAdapter(ClassroomClient(service))
    raise ValueError(f"unknown source {source!r}")


def has_credentials(source: str, user: User) -> bool:
    if source == "moodle":
        from app.moodle_tokens import token_for

        return token_for(user) is not None
    return bool(user.google_refresh_token)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], required=True)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="owner email for synced courses")
    who.add_argument("--all-users", action="store_true",
                     help="every user with credentials for --source (use with --enqueue)")
    parser.add_argument("--course", default=None, help="source course id (default: all)")
    parser.add_argument("--enqueue", action="store_true",
                        help="queue a sync job for the worker instead of running inline")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.all_users:
            users = [u for u in session.exec(select(User)).all()
                     if has_credentials(args.source, u)]
            if not users:
                print(f"no users have connected {args.source}")
            for u in users:
                if args.enqueue:
                    from app.jobs import enqueue

                    job = enqueue(session, "sync", {
                        "source": args.source,
                        "course_id": args.course,
                        "user_email": u.email,
                    })
                    print(f"enqueued {job.id} for {u.email}")
                else:
                    _sync_inline(session, args.source, u, args.course)
            return

        user = session.exec(select(User).where(User.email == args.user)).first()
        if user is None:
            raise SystemExit(f"no such user {args.user} — log in via the web UI first")
        if args.enqueue:
            from app.jobs import enqueue

            job = enqueue(session, "sync", {
                "source": args.source,
                "course_id": args.course,
                "user_email": args.user,
            })
            print(f"enqueued {job.id} (run `python -m app.worker` to drain)")
            return

        _sync_inline(session, args.source, user, args.course)


def _sync_inline(session, source: str, user: User, course: str | None) -> None:
    from app.sync import sync_all, sync_course

    try:
        adapter = build_adapter(source, user)
    except NotConnectedError as e:
        raise SystemExit(str(e)) from None
    if course:
        stats = sync_course(session, adapter, course, user.id)
        print(user.email, course, stats.as_dict())
    else:
        for course_id, stats in sync_all(session, adapter, user.id).items():
            print(user.email, course_id, stats.as_dict())


if __name__ == "__main__":
    main()
