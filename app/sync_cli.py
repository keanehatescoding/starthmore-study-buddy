"""Live sync entrypoint: python -m app.sync_cli --source moodle [--course ID]

Reads credentials from .env (see .env.example). For Moodle, --course is the
Moodle course id; omit it to sync every enrolled course.
"""

from __future__ import annotations

import argparse

from sqlmodel import Session

from app.config import settings
from app.db import engine


def build_adapter(source: str):
    if source == "moodle":
        from app.moodle import MoodleAdapter, MoodleClient

        return MoodleAdapter(
            MoodleClient(settings.moodle_base_url, settings.moodle_token)
        )
    if source == "classroom":
        from app.classroom import ClassroomAdapter, ClassroomClient, build_service

        service = build_service(
            settings.google_client_id,
            settings.google_client_secret,
            settings.google_refresh_token,
        )
        return ClassroomAdapter(ClassroomClient(service))
    raise ValueError(f"unknown source {source!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], required=True)
    parser.add_argument("--course", default=None, help="source course id (default: all)")
    args = parser.parse_args()

    from app.sync import sync_all, sync_course

    adapter = build_adapter(args.source)
    with Session(engine) as session:
        if args.course:
            stats = sync_course(session, adapter, args.course)
            print(args.course, stats.as_dict())
        else:
            for course_id, stats in sync_all(session, adapter).items():
                print(course_id, stats.as_dict())


if __name__ == "__main__":
    main()
