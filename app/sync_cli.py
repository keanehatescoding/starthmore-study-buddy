"""Live sync entrypoint: python -m app.sync_cli --source moodle --user EMAIL [--course ID]

Moodle uses the token from .env. Classroom reuses the Google refresh token
stored on the user row at login (falls back to GOOGLE_REFRESH_TOKEN in .env).
"""

from __future__ import annotations

import argparse

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import User


def build_adapter(source: str, user: User):
    if source == "moodle":
        from app.moodle import MoodleAdapter, MoodleClient

        return MoodleAdapter(
            MoodleClient(settings.moodle_base_url, settings.moodle_token)
        )
    if source == "classroom":
        from app.classroom import ClassroomAdapter, ClassroomClient, build_service

        refresh = user.google_refresh_token or settings.google_refresh_token
        if not refresh:
            raise SystemExit("no classroom refresh token — log in via Google first")
        service = build_service(
            settings.google_client_id, settings.google_client_secret, refresh
        )
        return ClassroomAdapter(ClassroomClient(service))
    raise ValueError(f"unknown source {source!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], required=True)
    parser.add_argument("--user", required=True, help="owner email for synced courses")
    parser.add_argument("--course", default=None, help="source course id (default: all)")
    args = parser.parse_args()

    from app.sync import sync_all, sync_course

    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == args.user)).first()
        if user is None:
            raise SystemExit(f"no such user {args.user} — log in via the web UI first")
        adapter = build_adapter(args.source, user)
        if args.course:
            stats = sync_course(session, adapter, args.course, user.id)
            print(args.course, stats.as_dict())
        else:
            for course_id, stats in sync_all(session, adapter, user.id).items():
                print(course_id, stats.as_dict())


if __name__ == "__main__":
    main()
