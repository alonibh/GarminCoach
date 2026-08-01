from datetime import date, datetime, timedelta

from coach.advisory_aggregates import build_ask_coach_aggregate_context
from db import Activity, DailyHealth


def test_aggregate_windows_exclude_future_and_preserve_missing_movement(session):
    end = date(2026, 8, 1)
    session.add_all([
        Activity(id=1, activity_type="running", start_time=datetime(2026, 8, 1, 9), duration_s=1800),
        Activity(id=2, activity_type="strength_training", start_time=datetime(2026, 7, 26, 9), duration_s=0),
        Activity(id=3, activity_type="running", start_time=datetime(2026, 8, 2, 9), duration_s=1800),
        DailyHealth(day=end, steps=0, daily_moderate_intensity_minutes=10),
    ])
    session.commit()
    report = build_ask_coach_aggregate_context(session, as_of_day=end, overnight_today_ready=True)
    recent = report.recent_7_days
    assert (recent.start_day, recent.end_day) == (end - timedelta(days=6), end)
    assert recent.activity_count == 2 and recent.active_days == 2
    assert recent.duration_minutes == 30 and recent.duration_valid_activities == 2
    assert recent.steps_total == 0 and recent.steps_valid_days == 1
    assert report.prior_7_days.end_day == end - timedelta(days=7)
    assert report.recent_28_days.activity_count == 2
