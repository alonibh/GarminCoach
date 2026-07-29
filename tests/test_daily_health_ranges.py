from datetime import date, timedelta

from db import DailyHealth
import sync.sync_service as svc


def test_range_chunks_deduplicate_split_gaps_and_cap_at_28_days():
    start = date(2026, 1, 1)
    days = [start + timedelta(days=n) for n in range(29)] + [start, start + timedelta(days=31)]

    chunks = svc._range_chunks(days)

    assert chunks == [
        (start, start + timedelta(days=27)),
        (start + timedelta(days=28), start + timedelta(days=28)),
        (start + timedelta(days=31), start + timedelta(days=31)),
    ]
    assert all((end - begin).days < 28 for begin, end in chunks)


def test_steps_range_maps_only_requested_dates_and_preserves_invalid_values(session, monkeypatch):
    first, second = date(2026, 7, 1), date(2026, 7, 2)
    session.add(DailyHealth(day=second, steps=99, step_goal=999))
    monkeypatch.setattr(svc.client, "daily_steps", lambda *_: [
        {"calendarDate": second.isoformat(), "totalSteps": 0, "stepGoal": 8000},
        {"calendarDate": first.isoformat(), "totalSteps": 123, "stepGoal": "bad"},
        {"calendarDate": second.isoformat(), "totalSteps": True, "stepGoal": float("inf")},
        {"calendarDate": "2026-08-01", "totalSteps": 1, "stepGoal": 1},
    ])

    assert svc._apply_steps_range(session, [first, second]) == {first, second}
    session.flush()
    assert (session.get(DailyHealth, first).steps, session.get(DailyHealth, first).step_goal) == (123, None)
    # Duplicate days use the final Garmin response-order entry; malformed final
    # values never erase a previously stored observation.
    assert (session.get(DailyHealth, second).steps, session.get(DailyHealth, second).step_goal) == (99, 999)


def test_body_battery_range_uses_last_duplicate_and_response_order_current(session, monkeypatch):
    day = date(2026, 7, 1)
    monkeypatch.setattr(svc.client, "body_battery", lambda *_: [
        {"date": day.isoformat(), "bodyBatteryValuesArray": [[1, 1]]},
        {"date": day.isoformat(), "bodyBatteryValuesArray": [[1, 0], [2, 90], [3, True], [4, float("nan")], [5, 42]]},
    ])

    assert svc._apply_body_battery_range(session, [day]) == {day}
    session.flush()
    row = session.get(DailyHealth, day)
    assert (row.body_battery_low, row.body_battery_high, row.body_battery_current) == (0, 90, 42)


def test_body_battery_range_stores_charged_and_drained_and_hrv_coverage_backfills(session, monkeypatch):
    day = date(2026, 7, 1)
    for offset in range(1, 7):
        session.add(DailyHealth(day=day + timedelta(days=offset), hrv_overnight=40 + offset))
    monkeypatch.setattr(svc.client, "body_battery", lambda *_: [{
        "date": day.isoformat(), "charged": 0, "drained": 31,
        "bodyBatteryValuesArray": [[1, 0], [2, 50]],
    }])
    assert svc._apply_body_battery_range(session, [day]) == {day}
    row = session.get(DailyHealth, day)
    assert (row.body_battery_charged, row.body_battery_drained) == (0, 31)

    row.hrv_overnight = 42
    svc._recompute_hrv_coverage(session, day)
    assert session.get(DailyHealth, day).hrv_7d_coverage_days == 1
    assert session.get(DailyHealth, day + timedelta(days=6)).hrv_7d_coverage_days == 7


def test_invalid_steps_shape_does_not_block_body_battery_or_advance_cursor(session, monkeypatch):
    day = date(2026, 7, 1)
    monkeypatch.setattr(svc, "_sync_daily_health_core", lambda *_args, **_kwargs: (True, True, True))
    monkeypatch.setattr(svc.client, "daily_steps", lambda *_: {"not": "a list"})
    monkeypatch.setattr(svc.client, "body_battery", lambda *_: [{
        "date": day.isoformat(), "bodyBatteryValuesArray": [[1, 50]],
    }])

    completed, gap = svc._sync_daily_health_window(session, day, day, current_optional=False)

    assert (completed, gap) == (0, day)
    session.flush()
    assert session.get(DailyHealth, day).body_battery_current == 50


def test_stage1_seven_day_health_request_budget_is_14_or_16(session, monkeypatch):
    today = date.today()
    start = today - timedelta(days=6)
    calls = []
    monkeypatch.setattr(svc.client, "hrv", lambda day: calls.append(("hrv", day)) or {})
    monkeypatch.setattr(svc.client, "user_summary", lambda day: calls.append(("summary", day)) or {
        "restingHeartRate": 50, "averageStressLevel": 20, "totalSteps": 100,
        "dailyStepGoal": 1000, "bodyBatteryHighestValue": 80,
        "bodyBatteryLowestValue": 20, "totalKilocalories": 2000,
    })

    assert svc._sync_daily_health_window(session, start, today, current_optional=False) == (7, None)
    assert len(calls) == 14  # 42 pre-optimization calls -> 14 combined-summary calls

    calls.clear()
    monkeypatch.setattr(svc.client, "user_summary", lambda day: calls.append(("summary", day)) or {
        "restingHeartRate": 50, "averageStressLevel": 20, "totalKilocalories": 2000,
    })
    monkeypatch.setattr(svc.client, "daily_steps", lambda begin, end: calls.append(("steps", begin, end)) or [])
    monkeypatch.setattr(svc.client, "body_battery", lambda begin, end: calls.append(("battery", begin, end)) or [])

    assert svc._sync_daily_health_window(session, start, today, current_optional=False) == (7, None)
    assert len(calls) == 16  # 42 pre-optimization calls -> 16 range-fallback calls
    assert ("steps", start, today) in calls
    assert ("battery", start, today) in calls
