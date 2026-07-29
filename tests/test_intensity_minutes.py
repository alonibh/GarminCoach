from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import create_engine, inspect, text

from app import _intensity_minutes_summary
from coach.advisory_snapshot import build_advisory_snapshot
from db import DailyHealth, init_db
from sync import sync_service


def test_daily_summary_uses_verified_intensity_keys_and_keeps_zero():
    parsed = sync_service._parse_daily_summary({
        "moderateIntensityMinutes": 0,
        "vigorousIntensityMinutes": 8,
    })

    assert parsed == (
        {
            "daily_moderate_intensity_minutes": 0,
            "daily_vigorous_intensity_minutes": 8,
        },
        {"intensity_minutes"},
    )


def test_daily_summary_rejects_invalid_intensity_without_losing_siblings():
    parsed = sync_service._parse_daily_summary({
        "totalSteps": 123,
        "moderateIntensityMinutes": True,
        "vigorousIntensityMinutes": -1,
    })

    assert parsed is not None
    values, families = parsed
    assert families == {"steps", "intensity_minutes"}
    assert values == {"steps": 123}

    for invalid in ("bad", float("nan"), float("inf"), float("-inf"), -0.1, False):
        parsed = sync_service._parse_daily_summary({"moderateIntensityMinutes": invalid})
        assert parsed == ({}, {"intensity_minutes"})


def test_daily_health_window_stores_intensity_from_existing_summary_calls(session, monkeypatch):
    start = date(2026, 7, 1)
    calls = []
    monkeypatch.setattr(sync_service.client, "hrv", lambda _day: {})

    def summary(day):
        calls.append(day)
        return {
            "restingHeartRate": 50,
            "averageStressLevel": 20,
            "totalSteps": 100,
            "dailyStepGoal": 1000,
            "bodyBatteryHighestValue": 80,
            "bodyBatteryLowestValue": 20,
            "totalKilocalories": 2000,
            "moderateIntensityMinutes": 10 if day != start + timedelta(days=1) else None,
            "vigorousIntensityMinutes": 0,
        }

    monkeypatch.setattr(sync_service.client, "user_summary", summary)
    assert sync_service._sync_daily_health_window(
        session, start, start + timedelta(days=2), current_optional=False,
    ) == (3, None)

    assert calls == [start, start + timedelta(days=1), start + timedelta(days=2)]
    assert session.get(DailyHealth, start).daily_moderate_intensity_minutes == 10
    assert session.get(DailyHealth, start).daily_vigorous_intensity_minutes == 0
    assert session.get(DailyHealth, start + timedelta(days=1)).daily_moderate_intensity_minutes is None
    # A second resolved pass is safe and uses the same existing summary path.
    assert sync_service._sync_daily_health_window(
        session, start, start + timedelta(days=2), current_optional=False,
    ) == (3, None)


def test_local_windows_disclose_coverage_without_missing_day_imputation(session, monkeypatch):
    end = date(2026, 7, 28)
    session.add_all([
        DailyHealth(day=end, daily_moderate_intensity_minutes=0, daily_vigorous_intensity_minutes=5),
        DailyHealth(day=end - timedelta(days=2), daily_moderate_intensity_minutes=10),
        DailyHealth(day=end - timedelta(days=8), daily_vigorous_intensity_minutes=7),
    ])
    session.flush()
    monkeypatch.setattr(sync_service.client, "user_summary", lambda *_: (_ for _ in ()).throw(AssertionError("dashboard helper called Garmin")))

    seven = _intensity_minutes_summary(session, end, 7)
    twenty_eight = _intensity_minutes_summary(session, end, 28)

    assert seven == {"days": 7, "moderate_minutes": 10, "vigorous_minutes": 5, "coverage_days": 2}
    assert twenty_eight == {"days": 28, "moderate_minutes": 10, "vigorous_minutes": 12, "coverage_days": 3}


def test_ask_coach_snapshot_excludes_raw_intensity_history(session):
    today = date.today()
    session.add(DailyHealth(
        day=today,
        daily_moderate_intensity_minutes=20,
        daily_vigorous_intensity_minutes=5,
    ))
    session.commit()

    assert "intensity" not in str(build_advisory_snapshot(session)).lower()


def test_daily_intensity_migration_adds_columns_idempotently():
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE daily_health (day DATE PRIMARY KEY)"))

        init_db(engine)
        init_db(engine)

        columns = {column["name"] for column in inspect(engine).get_columns("daily_health")}
        assert {"daily_moderate_intensity_minutes", "daily_vigorous_intensity_minutes"} <= columns
    finally:
        engine.dispose()
