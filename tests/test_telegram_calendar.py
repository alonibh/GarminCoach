from datetime import datetime
from uuid import uuid4

from coach.advisory_snapshot import build_advisory_snapshot
from coach.renderers import render_calendar
from db import PlannedSession
from tenant_context import TenantIdentity, tenant_scope


def _now():
    return datetime(2026, 7, 27, 9, 0)


def test_telegram_calendar_shows_timed_icloud_event(monkeypatch):
    monkeypatch.setattr("coach.renderers.get_local_now", _now)
    rendered = render_calendar(
        {"state": "fresh", "events": [{
            "title": "Dentist", "start": "2026-07-27 10:30",
            "end": "11:00", "all_day": False,
        }]},
        [],
    )
    assert "Personal calendar:" in rendered
    assert "Mon 27 Jul 10:30: Dentist" in rendered


def test_telegram_calendar_shows_all_day_private_event(monkeypatch):
    monkeypatch.setattr("coach.renderers.get_local_now", _now)
    rendered = render_calendar(
        {"state": "fresh", "events": [{
            "title": "Public holiday", "start": "2026-07-28",
            "end": "", "all_day": True,
        }]},
        [],
    )
    assert "Tue 28 Jul (all day): Public holiday" in rendered


def test_telegram_calendar_merges_private_and_workouts_chronologically(monkeypatch):
    monkeypatch.setattr("coach.renderers.get_local_now", _now)
    rendered = render_calendar(
        {"state": "fresh", "events": [{
            "title": "Lunch", "start": "2026-07-27 12:00",
            "end": "13:00", "all_day": False,
        }]},
        [{
            "title": "Morning run", "date": "2026-07-27", "start_time": "09:00",
        }],
    )
    assert "GarminCoach workouts:" in rendered
    assert rendered.index("Morning run") < rendered.index("Lunch")


def test_telegram_calendar_keeps_workouts_when_private_calendar_fails(monkeypatch):
    monkeypatch.setattr("coach.renderers.get_local_now", _now)
    rendered = render_calendar(
        {"state": "error", "events": []},
        [{"title": "Strength", "date": "2026-07-28", "start_time": "18:00"}],
    )
    assert "Calendar temporarily unavailable" in rendered
    assert "Strength" in rendered


def test_telegram_calendar_reports_unconfigured_when_empty(monkeypatch):
    monkeypatch.setattr("coach.renderers.get_local_now", _now)
    rendered = render_calendar({"state": "unconfigured", "events": []}, [])
    assert "No private calendar connected" in rendered
    assert "No events in the next 7 days" in rendered


def test_advisory_snapshot_uses_tenant_scoped_cached_private_calendar(monkeypatch, session):
    import coach.calendar as calendar
    import coach.advisory_snapshot as snapshot

    now = datetime.now().astimezone()
    today = now.date()
    user_one, user_two = str(uuid4()), str(uuid4())
    session.add(PlannedSession(
        title="Own workout", target_date=today, suggested_time="18:00", status="approved"
    ))
    session.commit()
    monkeypatch.setattr(snapshot, "datetime", type("Clock", (), {
        "now": staticmethod(lambda _tz=None: now),
        "combine": datetime.combine,
        "strptime": datetime.strptime,
    }))
    with tenant_scope(TenantIdentity(user_one, timezone="UTC")):
        calendar._store_schedule_cache({"state": "fresh", "error": None, "events": [{
            "title": "Own private", "start": f"{today.isoformat()} 09:00",
            "end": "10:00", "all_day": False,
        }]})
        own = build_advisory_snapshot(session)["calendar_next_7_days"]["items"]
    with tenant_scope(TenantIdentity(user_two, timezone="UTC")):
        calendar._store_schedule_cache({"state": "fresh", "error": None, "events": [{
            "title": "Other private", "start": f"{today.isoformat()} 09:00",
            "end": "10:00", "all_day": False,
        }]})
        other = build_advisory_snapshot(session)["calendar_next_7_days"]["items"]
    assert {entry["title"] for entry in own} == {"Own private", "Own workout"}
    assert "Other private" not in {entry["title"] for entry in own}
    assert "Other private" in {entry["title"] for entry in other}


def test_private_calendar_url_is_never_logged(monkeypatch, caplog):
    import config
    import coach.calendar as calendar

    private_url = "https://calendar.google.com/calendar/private-token.ics"
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)
    monkeypatch.setattr(config, "ICS_CALENDAR_URL", private_url)
    monkeypatch.setattr(calendar, "events", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError()))
    assert calendar.get_upcoming_schedule_result(days=7)["state"] == "error"
    assert private_url not in "\n".join(record.getMessage() for record in caplog.records)
