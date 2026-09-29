"""Admin chores: python -m app.admin_cli claim-unowned EMAIL

Courses synced before sign-in existed have no owner and are hidden from
everyone. They are claimed automatically when MOODLE_TOKEN_OWNER signs in;
this assigns them by hand.
"""

from __future__ import annotations

import argparse

from sqlmodel import Session

from app.auth import claim_unowned, find_user
from app.db import engine


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim-unowned", help="assign all unowned courses to a user")
    claim.add_argument("email")
    args = parser.parse_args()

    with Session(engine) as session:
        user = find_user(session, args.email)
        if user is None:
            raise SystemExit(f"no such user {args.email} — log in via the web UI first")
        claimed, skipped = claim_unowned(session, user)
        print(f"assigned {claimed} unowned course(s) to {user.email}")
        if skipped:
            print(f"skipped {skipped} they already have their own copy of")


if __name__ == "__main__":
    main()
