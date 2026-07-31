from datetime import date, datetime, timedelta
from math import inf, nan

import pytest

from db import DailyHealth, Sleep
from metrics.recovery_trends import (
    NumericObservation,
    TrendCoverage,
    TrendDirection,
    build_recovery_health_trend_report,
    build_sleep_timing_trend,
    build_trend,
    clock_time_from_offset_minutes,
    window_stats,
)


END = date(2026, 7, 28)


def observations(days, value):
    return [NumericObservation(END - timedelta(days=offset), value) for offset in days]


def test_exact_calendar_windows_median_and_missing_days_are_preserved():
    trend = build_trend(
        "sleep_score", "Sleep Score", "points",
        observations(range(0, 4), 80) + observations(range(7, 17), 70), end_day=END,
    )
    assert (trend.recent.start_day, trend.recent.end_day) == (END - timedelta(days=6), END)
    assert (trend.baseline.start_day, trend.baseline.end_day) == (END - timedelta(days=27), END - timedelta(days=7))
    assert (trend.recent.valid_days, trend.baseline.valid_days) == (4, 10)
    assert (trend.recent.median, trend.baseline.median) == (80.0, 70.0)
    assert trend.direction is TrendDirection.HIGHER
    assert trend.coverage is TrendCoverage.PARTIAL


def test_invalid_values_duplicate_days_and_coverage_gates_fail_closed():
    malformed = [
        NumericObservation(END, True), NumericObservation(END - timedelta(days=1), nan),
        NumericObservation(END - timedelta(days=2), inf), NumericObservation(END - timedelta(days=3), -1),
    ]
    insufficient_recent = build_trend("hrv_overnight", "HRV", "ms", malformed + observations((4, 5, 6), 40) + observations(range(7, 17), 40), end_day=END)
    assert insufficient_recent.recent.valid_days == 3
    assert insufficient_recent.direction is TrendDirection.INSUFFICIENT_DATA
    duplicate = build_trend("steps", "Steps", "steps", observations(range(0, 4), 1000) + [NumericObservation(END, 1100)] + observations(range(7, 17), 1000), end_day=END)
    assert duplicate.recent.valid_days == 3
    assert duplicate.direction is TrendDirection.INSUFFICIENT_DATA


def test_thresholds_percentages_and_zero_baseline_are_deterministic():
    hrv = build_trend("hrv_overnight", "HRV", "ms", observations(range(0, 4), 103) + observations(range(7, 17), 100), end_day=END)
    assert hrv.meaningful_threshold == 5
    assert hrv.direction is TrendDirection.STABLE
    steps = build_trend("steps", "Steps", "steps", observations(range(0, 4), 1600) + observations(range(7, 17), 1000), end_day=END)
    assert steps.meaningful_threshold == 500
    assert steps.direction is TrendDirection.HIGHER
    stress = build_trend("stress_avg", "Stress", "points", observations(range(0, 4), 3) + observations(range(7, 17), 0), end_day=END)
    assert stress.delta_percent is None
    assert stress.direction is TrendDirection.HIGHER


def test_sleep_timing_uses_midpoint_mad_and_validates_intervals():
    rows = []
    for offset in range(28):
        day = END - timedelta(days=offset)
        start = datetime.combine(day - timedelta(days=1), datetime.min.time()) + timedelta(hours=23, minutes=offset % 2 * 10)
        rows.append(Sleep(day=day, sleep_start_time=start, sleep_end_time=start + timedelta(hours=8)))
    rows.append(Sleep(day=END - timedelta(days=1), sleep_start_time=datetime(2026, 7, 1, 8), sleep_end_time=datetime(2026, 7, 1, 8)))
    trend = build_sleep_timing_trend(rows, end_day=END)
    assert trend is not None
    assert trend.recent.valid_days == 7
    assert trend.baseline.valid_days == 21
    assert trend.recent.median == 0
    assert trend.direction is TrendDirection.STABLE
    assert trend.recent_bedtime is not None and trend.recent_wake_time is not None


def test_orm_builder_respects_partial_day_endpoints_and_keeps_sources_separate(session):
    for offset in range(28):
        day = END - timedelta(days=offset)
        session.add(Sleep(day=day, total_s=8 * 3600, score=80))
        session.add(DailyHealth(
            day=day, hrv_overnight=50, resting_hr=55, stress_avg=30, steps=5000,
            body_battery_high=80, body_battery_low=25, body_battery_charged=45,
            body_battery_drained=55, recovery_time_minutes=120,
            hrv_status="balanced" if offset == 1 else None,
            hrv_baseline_low=42 if offset == 1 else None,
            hrv_baseline_high=58 if offset == 1 else None,
        ))
    session.commit()
    report = build_recovery_health_trend_report(session, as_of_day=END, overnight_today_ready=False)
    by_key = {trend.key: trend for trend in report.trends}
    assert report.overnight_end_day == END - timedelta(days=1)
    assert report.full_day_end_day == END - timedelta(days=1)
    assert by_key["sleep_duration"].recent.end_day == END - timedelta(days=1)
    assert by_key["stress_avg"].recent.end_day == END - timedelta(days=1)
    assert by_key["body_battery_high"].recent.median == 80
    assert by_key["body_battery_low"].recent.median == 25
    assert by_key["hrv_overnight"].source_status == "balanced"
    assert by_key["hrv_overnight"].source_baseline_low == 42
    assert not session.new and not session.dirty


def test_orm_builder_can_include_completed_local_overnight_but_excludes_current_full_day(session):
    for offset in range(28):
        day = END - timedelta(days=offset)
        session.add(Sleep(day=day, total_s=8 * 3600, score=80))
        session.add(DailyHealth(day=day, hrv_overnight=50, resting_hr=55, stress_avg=30, body_battery_high=80, body_battery_low=25, body_battery_charged=45, body_battery_drained=55, steps=5000))
    session.get(DailyHealth, END).stress_avg = 99
    session.commit()
    report = build_recovery_health_trend_report(session, as_of_day=END, overnight_today_ready=True)
    by_key = {trend.key: trend for trend in report.trends}
    assert by_key["sleep_duration"].recent.end_day == END
    assert by_key["hrv_overnight"].recent.valid_days == 7
    assert by_key["stress_avg"].recent.end_day == END - timedelta(days=1)
    assert by_key["stress_avg"].latest_value == 30


@pytest.mark.parametrize("key,baseline,threshold", [
    ("sleep_duration", 8, .25), ("sleep_score", 80, 3),
    ("hrv_overnight", 100, 5), ("resting_hr", 55, 2), ("stress_avg", 30, 3),
    ("body_battery_high", 80, 5), ("body_battery_low", 20, 5),
    ("body_battery_charged", 40, 5), ("body_battery_drained", 50, 5),
    ("recovery_time", 120, 60), ("steps", 10_000, 500),
])
def test_every_threshold_is_stable_only_below_and_directional_at_or_above(key, baseline, threshold):
    def trend(delta):
        return build_trend(key, key, "unit", observations(range(4), baseline + delta) + observations(range(7, 17), baseline), end_day=END)
    below = threshold - 1 if key in {"body_battery_charged", "body_battery_drained", "recovery_time", "steps"} else threshold - .01
    above = threshold + 1 if key in {"body_battery_charged", "body_battery_drained", "recovery_time", "steps"} else threshold + .01
    assert trend(below).direction is TrendDirection.STABLE
    assert trend(threshold).direction is TrendDirection.HIGHER
    assert trend(above).direction is TrendDirection.HIGHER
    assert trend(-threshold).direction is TrendDirection.LOWER


def test_hrv_and_steps_minimum_threshold_rules_and_all_coverage_categories():
    hrv = build_trend("hrv_overnight", "HRV", "ms", observations(range(4), 12) + observations(range(7, 17), 10), end_day=END)
    steps = build_trend("steps", "Steps", "steps", observations(range(4), 600) + observations(range(7, 17), 100), end_day=END)
    assert hrv.meaningful_threshold == 2
    assert steps.meaningful_threshold == 500
    assert build_trend("stress_avg", "Stress", "points", observations(range(7), 20) + observations(range(7, 20), 20), end_day=END).coverage is TrendCoverage.SUFFICIENT
    assert build_trend("stress_avg", "Stress", "points", observations(range(4), 20) + observations(range(7, 17), 20), end_day=END).coverage is TrendCoverage.PARTIAL
    assert build_trend("stress_avg", "Stress", "points", observations(range(5), 20), end_day=END).coverage is TrendCoverage.SPARSE
    assert build_trend("stress_avg", "Stress", "points", [], end_day=END).coverage is TrendCoverage.NONE


def test_validity_bounds_and_input_order_are_deterministic():
    bounds = {
        "sleep_duration": (.1, 24, 0, 25), "sleep_score": (0, 100, -1, 101),
        "hrv_overnight": (1, 999, 0, -1), "resting_hr": (1, 999, 0, -1),
        "stress_avg": (0, 100, -1, 101), "body_battery_high": (0, 100, -1, 101),
        "body_battery_low": (0, 100, -1, 101), "body_battery_charged": (0, 999, -1, 1.5),
        "body_battery_drained": (0, 999, -1, 1.5), "recovery_time": (0, 999, -1, 1.5),
        "steps": (0, 999, -1, 1.5), "intensity_minutes": (0, 999, -1, -2),
    }
    for key, (lower, upper, invalid_low, invalid_high) in bounds.items():
        stats = window_stats([NumericObservation(END, lower), NumericObservation(END - timedelta(days=1), upper), NumericObservation(END - timedelta(days=2), invalid_low), NumericObservation(END - timedelta(days=3), invalid_high)], key, END - timedelta(days=6), END)
        assert stats.valid_days == 2
    ordered = observations(range(4), 30) + observations(range(7, 17), 20)
    assert build_trend("stress_avg", "Stress", "points", ordered, end_day=END) == build_trend("stress_avg", "Stress", "points", list(reversed(ordered)), end_day=END)


def test_clock_time_offsets_and_midnight_sleep_presentation_are_day_relative():
    assert clock_time_from_offset_minutes(-30).strftime("%H:%M") == "23:30"
    assert clock_time_from_offset_minutes(1455).strftime("%H:%M") == "00:15"
    assert clock_time_from_offset_minutes(1439.5).strftime("%H:%M") == "00:00"
    rows = []
    for offset in range(4):
        day = END - timedelta(days=offset)
        start = (
            datetime.combine(day - timedelta(days=1), datetime.min.time()) + timedelta(hours=23, minutes=50)
            if offset % 2 else datetime.combine(day, datetime.min.time()) + timedelta(minutes=10)
        )
        rows.append(Sleep(day=day, sleep_start_time=start, sleep_end_time=start + timedelta(hours=7)))
    trend = build_sleep_timing_trend(rows, end_day=END)
    assert trend is not None
    assert trend.recent_bedtime.strftime("%H:%M") == "00:00"
    assert trend.recent_wake_time.strftime("%H:%M") == "07:00"


def test_hrv_source_is_bounded_by_overnight_endpoint_and_baselines_are_safe(session):
    yesterday = END - timedelta(days=1)
    session.add_all([
        DailyHealth(day=yesterday, hrv_status="  balanced  ", hrv_baseline_low=42, hrv_baseline_high=58),
        DailyHealth(day=END, hrv_status="today", hrv_baseline_low=60, hrv_baseline_high=40),
        DailyHealth(day=END + timedelta(days=1), hrv_status="future", hrv_baseline_low=1, hrv_baseline_high=2),
    ])
    session.commit()
    not_ready = build_recovery_health_trend_report(session, as_of_day=END, overnight_today_ready=False)
    hrv = next(trend for trend in not_ready.trends if trend.key == "hrv_overnight")
    assert (hrv.source_status, hrv.source_day, hrv.source_baseline_low, hrv.source_baseline_high) == ("balanced", yesterday, 42, 58)
    ready = build_recovery_health_trend_report(session, as_of_day=END, overnight_today_ready=True)
    ready_hrv = next(trend for trend in ready.trends if trend.key == "hrv_overnight")
    assert ready_hrv.source_status == "today"
    assert ready_hrv.source_baseline_low is None and ready_hrv.source_baseline_high is None
    assert ready_hrv.source_day <= ready.overnight_end_day


def test_hrv_source_skips_whitespace_and_returns_none_when_no_eligible_status(session):
    session.add_all([
        DailyHealth(day=END - timedelta(days=2), hrv_status="baseline"),
        DailyHealth(day=END - timedelta(days=1), hrv_status="   "),
    ])
    session.commit()
    report = build_recovery_health_trend_report(session, as_of_day=END, overnight_today_ready=False)
    hrv = next(trend for trend in report.trends if trend.key == "hrv_overnight")
    assert hrv.source_status == "baseline"
    session.query(DailyHealth).delete()
    session.commit()
    report = build_recovery_health_trend_report(session, as_of_day=END, overnight_today_ready=False)
    hrv = next(trend for trend in report.trends if trend.key == "hrv_overnight")
    assert (hrv.source_status, hrv.source_day, hrv.source_baseline_low, hrv.source_baseline_high) == (None, None, None, None)
