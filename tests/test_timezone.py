"""Per-user time zone: study days start at the user's local midnight (issue #33)."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import select

from app import grade
from app.config import Settings, settings
from app.grade import _new_allowance, submit_answer
from app.models import Assignment, Course, ReviewState, User
from app.srs import next_review_date
from app.stats import compute_stats
from tests.test_grade import setup  # noqa: F401  (fixture)

NAIROBI = ZoneInfo("Africa/Nairobi")  # UTC+3, no DST
NEW_YORK = ZoneInfo("America/New_York")


def test_next_review_snaps_to_local_midnight():
    evening = datetime(2026, 9, 30, 21, 30, tzinfo=NAIROBI)
    due = next_review_date(1, evening, NAIROBI)
    assert due == datetime(2026, 10, 1, 0, 0, tzinfo=NAIROBI)
    assert due.tzinfo == timezone.utc
    # 01:00 in Nairobi is still the 29th in UTC; the local date decides
    early = datetime(2026, 9, 30, 1, 0, tzinfo=NAIROBI)
    assert next_review_date(3, early, NAIROBI) == datetime(2026, 10, 3, tzinfo=NAIROBI)


def test_next_review_across_dst_lands_on_midnight():
    # New York leaves DST on 2026-11-01: still local midnight, 25 hours later
    due = next_review_date(1, datetime(2026, 10, 31, 15, 0, tzinfo=NEW_YORK), NEW_YORK)
    assert due.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 11, 1)
    due = next_review_date(1, datetime(2026, 11, 1, 15, 0, tzinfo=NEW_YORK), NEW_YORK)
    assert due.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 11, 2)


def test_answer_schedules_for_the_users_local_midnight(setup):  # noqa: F811
    s, user, mcq, _ = setup
    user.timezone = "America/New_York"
    s.add(user)
    s.commit()
    out = submit_answer(s, user.id, mcq.id, "1")
    local = out["next_review_date"].astimezone(NEW_YORK)
    assert (local.hour, local.minute) == (0, 0)
    assert local.date() == datetime.now(NEW_YORK).date() + timedelta(days=1)


def test_unset_timezone_uses_the_app_default(setup):  # noqa: F811
    s, user, mcq, _ = setup
    local = submit_answer(s, user.id, mcq.id, "1")["next_review_date"].astimezone(settings.tz)
    assert (local.hour, local.minute) == (0, 0)


def test_new_item_cap_resets_at_local_midnight(setup, monkeypatch):  # noqa: F811
    s, user, mcq, _ = setup
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 1)
    user.timezone = "Africa/Nairobi"
    s.add(user)
    submit_answer(s, user.id, mcq.id, "1")
    state = s.exec(select(ReviewState)).one()
    # answered 23:30 on the 30th in Nairobi (20:30 UTC, same UTC day as "now")
    # stored as UTC, like the app does: SQLite keeps the wall time and drops the zone
    state.first_answered_at = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
    s.add(state)
    s.commit()
    assert _new_allowance(s, user.id, datetime(2026, 9, 30, 23, 45, tzinfo=NAIROBI)) == 0
    # 00:15 on the 1st in Nairobi is still the 30th in UTC, but a new local day
    assert _new_allowance(s, user.id, datetime(2026, 10, 1, 0, 15, tzinfo=NAIROBI)) == 1


def test_stats_streak_uses_the_users_zone(setup):  # noqa: F811
    from app.models import ReviewLog

    s, user, mcq, _ = setup
    for at in (datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc),  # 30th, 02:00 Nairobi
               datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)):
        s.add(ReviewLog(user_id=user.id, quiz_item_id=mcq.id, verdict="correct",
                        partial_credit=1.0, answered_at=at))
    user.timezone = "UTC"
    s.add(user)
    s.commit()
    now = datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc)
    assert compute_stats(s, user.id, now=now)["streak_days"] == 1
    user.timezone = "Africa/Nairobi"
    s.add(user)
    s.commit()
    assert compute_stats(s, user.id, now=now)["streak_days"] == 2


def test_unknown_zone_falls_back_to_the_default():
    conf = Settings(_env_file=None)
    assert conf.zone("Mars/Olympus") == conf.zone(None) == conf.tz
    assert conf.zone("Asia/Tokyo") == ZoneInfo("Asia/Tokyo")


def _settings_token(client) -> str:
    page = client.get("/settings/moodle")
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _stored_zone(testapp):
    with testapp["Session"]() as s:
        return s.get(User, testapp["user_id"]).timezone


def test_settings_saves_the_timezone(testapp):
    client = testapp["client"]
    token = _settings_token(client)
    assert '<option value="Africa/Nairobi" selected>' in client.get("/settings/moodle").text
    r = client.post("/settings/timezone", data={"csrf_token": token, "timezone": "Asia/Tokyo"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _stored_zone(testapp) == "Asia/Tokyo"
    page = client.get("/settings/moodle").text
    assert '<option value="Asia/Tokyo" selected>' in page and "Asia/Tokyo" in page
    # picking the app default stores NULL, so it follows a later TIMEZONE change
    client.post("/settings/timezone", data={"csrf_token": token, "timezone": settings.timezone})
    assert _stored_zone(testapp) is None


def test_settings_rejects_unknown_timezone(testapp):
    client = testapp["client"]
    token = _settings_token(client)
    for bogus in ("Mars/Olympus", "EST", ""):
        r = client.post("/settings/timezone", data={"csrf_token": token, "timezone": bogus})
        assert _stored_zone(testapp) is None
        assert "Pick a time zone" in r.text  # flash shown after the redirect
    assert client.post("/settings/timezone",
                       data={"csrf_token": "bogus", "timezone": "Asia/Tokyo"}).status_code == 403


def test_assignment_due_time_shown_in_users_zone(testapp):
    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        user.timezone = "Asia/Tokyo"  # UTC+9
        course = Course(user_id=user.id, source="moodle", source_id="c1", name="C")
        s.add_all([user, course])
        s.commit()
        s.add(Assignment(course_id=course.id, source_id="a1", title="Essay",
                         due_date=datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)))
        s.commit()
        course_id = course.id
    page = testapp["client"].get(f"/courses/{course_id}").text
    assert "due 2026-10-02 05:00 JST" in page
