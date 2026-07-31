"""Read-only, deterministic 28-day recovery and health trend calculations.

This module deliberately has no sync, decision, notification, or web imports.
It compares stored observations only; the result is informational presentation
data and has no authority over workouts or coaching actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from math import isfinite
from statistics import median
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import DailyHealth, Sleep


class TrendDirection(str, Enum):
    HIGHER = "higher"
    LOWER = "lower"
    STABLE = "stable"
    INSUFFICIENT_DATA = "insufficient_data"


class TrendCoverage(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    SPARSE = "sparse"
    NONE = "none"


@dataclass(frozen=True)
class NumericObservation:
    day: date
    value: object


@dataclass(frozen=True)
class TrendWindowStats:
    start_day: date
    end_day: date
    valid_days: int
    calendar_days: int
    median: float | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class RecoveryHealthTrend:
    key: str
    label: str
    unit: str
    recent: TrendWindowStats
    baseline: TrendWindowStats
    direction: TrendDirection
    delta: float | None
    delta_percent: float | None
    meaningful_threshold: float | None
    coverage: TrendCoverage
    latest_value: float | None
    latest_day: date | None
    source_status: str | None
    source_day: date | None
    source_baseline_low: float | None
    source_baseline_high: float | None
    informational_note: str


@dataclass(frozen=True)
class SleepTimingTrend:
    recent: TrendWindowStats
    baseline: TrendWindowStats
    direction: TrendDirection
    delta: float | None
    coverage: TrendCoverage
    recent_bedtime: time | None
    recent_wake_time: time | None
    latest_day: date | None
    informational_note: str


@dataclass(frozen=True)
class BodyBatteryTrend:
    high: RecoveryHealthTrend
    low: RecoveryHealthTrend
    charged: RecoveryHealthTrend
    drained: RecoveryHealthTrend


@dataclass(frozen=True)
class RecoveryHealthTrendReport:
    as_of_day: date
    overnight_end_day: date
    full_day_end_day: date
    trends: tuple[RecoveryHealthTrend, ...]
    sleep_timing: SleepTimingTrend | None
    body_battery: BodyBatteryTrend | None


INFORMATIONAL_NOTE = "Trends are informational and do not change your workout."


def _finite_number(value: object) -> float | None:
    """Return a finite numeric value, explicitly excluding bools."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _integer_like_nonnegative(value: object) -> float | None:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return number


def _valid_value(key: str, value: object) -> float | None:
    number = _finite_number(value)
    if key == "sleep_duration":
        return number if number is not None and 0 < number <= 24 else None
    if key == "sleep_score":
        return number if number is not None and 0 <= number <= 100 else None
    if key in {"hrv_overnight", "resting_hr"}:
        return number if number is not None and number > 0 else None
    if key == "stress_avg":
        return number if number is not None and 0 <= number <= 100 else None
    if key in {"body_battery_high", "body_battery_low", "body_battery_current"}:
        return number if number is not None and 0 <= number <= 100 else None
    if key in {"body_battery_charged", "body_battery_drained", "recovery_time", "steps"}:
        return _integer_like_nonnegative(value)
    if key == "intensity_minutes":
        return number if number is not None and number >= 0 else None
    return None


def _deduplicated_values(
    observations: Iterable[NumericObservation], key: str, start_day: date, end_day: date,
) -> dict[date, float]:
    """Keep one valid value per day; duplicate source days fail closed."""
    candidates: dict[date, list[float]] = {}
    for observation in observations:
        if not isinstance(observation.day, date) or not start_day <= observation.day <= end_day:
            continue
        value = _valid_value(key, observation.value)
        if value is not None:
            candidates.setdefault(observation.day, []).append(value)
    return {day: values[0] for day, values in candidates.items() if len(values) == 1}


def window_stats(
    observations: Iterable[NumericObservation], key: str, start_day: date, end_day: date,
) -> TrendWindowStats:
    values_by_day = _deduplicated_values(observations, key, start_day, end_day)
    values = sorted(values_by_day.values())
    return TrendWindowStats(
        start_day=start_day,
        end_day=end_day,
        valid_days=len(values),
        calendar_days=(end_day - start_day).days + 1,
        median=float(median(values)) if values else None,
        minimum=float(values[0]) if values else None,
        maximum=float(values[-1]) if values else None,
    )


def coverage_for(recent: TrendWindowStats, baseline: TrendWindowStats) -> TrendCoverage:
    valid_days = recent.valid_days + baseline.valid_days
    comparison_ready = recent.valid_days >= 4 and baseline.valid_days >= 10
    if valid_days >= 20 and comparison_ready:
        return TrendCoverage.SUFFICIENT
    if valid_days >= 14:
        return TrendCoverage.PARTIAL
    if valid_days:
        return TrendCoverage.SPARSE
    return TrendCoverage.NONE


def _fixed(value: float) -> Callable[[float | None], float]:
    return lambda _: value


def _percent_or_minimum(minimum: float) -> Callable[[float | None], float]:
    return lambda baseline: max(minimum, 0.05 * baseline) if baseline and baseline > 0 else minimum


_THRESHOLDS: dict[str, Callable[[float | None], float]] = {
    "sleep_duration": _fixed(0.25),
    "sleep_score": _fixed(3),
    "hrv_overnight": _percent_or_minimum(2),
    "resting_hr": _fixed(2),
    "stress_avg": _fixed(3),
    "body_battery_high": _fixed(5),
    "body_battery_low": _fixed(5),
    "body_battery_charged": _fixed(5),
    "body_battery_drained": _fixed(5),
    "recovery_time": _fixed(60),
    "steps": _percent_or_minimum(500),
}

_METRICS: tuple[tuple[str, str, str], ...] = (
    ("sleep_duration", "Sleep duration", "hours"),
    ("sleep_score", "Sleep Score", "points"),
    ("hrv_overnight", "Overnight HRV", "ms"),
    ("resting_hr", "Resting HR", "bpm"),
    ("stress_avg", "Stress", "points"),
    ("body_battery_high", "Body Battery high", "points"),
    ("body_battery_low", "Body Battery low", "points"),
    ("body_battery_charged", "Body Battery charged", "points"),
    ("body_battery_drained", "Body Battery drained", "points"),
    ("recovery_time", "Recovery Time", "minutes"),
    ("steps", "Steps", "steps"),
)


def build_trend(
    key: str,
    label: str,
    unit: str,
    observations: Iterable[NumericObservation],
    *,
    end_day: date,
    source_status: str | None = None,
    source_day: date | None = None,
    source_baseline_low: float | None = None,
    source_baseline_high: float | None = None,
) -> RecoveryHealthTrend:
    """Pure 7-day vs preceding-21-day median comparison."""
    recent_start = end_day - timedelta(days=6)
    baseline_start = end_day - timedelta(days=27)
    baseline_end = end_day - timedelta(days=7)
    observation_list = tuple(observations)
    recent = window_stats(observation_list, key, recent_start, end_day)
    baseline = window_stats(observation_list, key, baseline_start, baseline_end)
    coverage = coverage_for(recent, baseline)
    threshold = _THRESHOLDS[key](baseline.median)
    delta = None
    delta_percent = None
    direction = TrendDirection.INSUFFICIENT_DATA
    if recent.valid_days >= 4 and baseline.valid_days >= 10:
        # Both medians are present when their valid-day requirement is met.
        delta = float(recent.median - baseline.median)  # type: ignore[operator]
        if baseline.median and baseline.median > 0:
            delta_percent = (delta / baseline.median) * 100
        if abs(delta) < threshold:
            direction = TrendDirection.STABLE
        else:
            direction = TrendDirection.HIGHER if delta > 0 else TrendDirection.LOWER
    valid_values = _deduplicated_values(observation_list, key, baseline_start, end_day)
    latest_day = max(valid_values) if valid_values else None
    return RecoveryHealthTrend(
        key=key, label=label, unit=unit, recent=recent, baseline=baseline,
        direction=direction, delta=delta, delta_percent=delta_percent,
        meaningful_threshold=threshold, coverage=coverage,
        latest_value=valid_values.get(latest_day) if latest_day else None,
        latest_day=latest_day, source_status=source_status,
        source_day=source_day, source_baseline_low=source_baseline_low,
        source_baseline_high=source_baseline_high,
        informational_note=INFORMATIONAL_NOTE,
    )


def _time_observations(rows: Iterable[Sleep], start_day: date, end_day: date) -> list[tuple[date, datetime, datetime, float]]:
    """Return validated timing observations; duplicate days fail closed."""
    grouped: dict[date, list[tuple[datetime, datetime, float]]] = {}
    for row in rows:
        start, finish = row.sleep_start_time, row.sleep_end_time
        if not start_day <= row.day <= end_day or not isinstance(start, datetime) or not isinstance(finish, datetime):
            continue
        if finish <= start:
            continue
        midnight = datetime.combine(row.day, time.min)
        midpoint = start + (finish - start) / 2
        offset = (midpoint - midnight).total_seconds() / 60
        grouped.setdefault(row.day, []).append((start, finish, offset))
    return [(day, *values[0]) for day, values in grouped.items() if len(values) == 1]


def _timing_stats(values: list[tuple[date, datetime, datetime, float]], start_day: date, end_day: date) -> TrendWindowStats:
    in_window = [value for value in values if start_day <= value[0] <= end_day]
    offsets = sorted(value[3] for value in in_window)
    midpoint = float(median(offsets)) if offsets else None
    mad = float(median(sorted(abs(offset - midpoint) for offset in offsets))) if midpoint is not None else None
    return TrendWindowStats(start_day, end_day, len(offsets), (end_day - start_day).days + 1, mad, min(offsets) if offsets else None, max(offsets) if offsets else None)


def build_sleep_timing_trend(rows: Iterable[Sleep], *, end_day: date) -> SleepTimingTrend | None:
    recent_start, baseline_start, baseline_end = end_day - timedelta(days=6), end_day - timedelta(days=27), end_day - timedelta(days=7)
    values = _time_observations(rows, baseline_start, end_day)
    recent = _timing_stats(values, recent_start, end_day)
    baseline = _timing_stats(values, baseline_start, baseline_end)
    coverage = coverage_for(recent, baseline)
    direction, delta = TrendDirection.INSUFFICIENT_DATA, None
    if recent.valid_days >= 4 and baseline.valid_days >= 10:
        delta = float(recent.median - baseline.median)  # type: ignore[operator]
        direction = TrendDirection.STABLE if abs(delta) < 15 else (TrendDirection.HIGHER if delta > 0 else TrendDirection.LOWER)
    recent_values = [value for value in values if recent_start <= value[0] <= end_day]
    bedtime = None
    wake_time = None
    if recent_values:
        # Median datetime is not meaningful across arbitrary dates; median local clock minute is.
        bedtime_minutes = median([v[1].hour * 60 + v[1].minute for v in recent_values])
        wake_minutes = median([v[2].hour * 60 + v[2].minute for v in recent_values])
        bedtime = (datetime.min + timedelta(minutes=bedtime_minutes)).time()
        wake_time = (datetime.min + timedelta(minutes=wake_minutes)).time()
    return SleepTimingTrend(recent, baseline, direction, delta, coverage, bedtime, wake_time, max((v[0] for v in values), default=None), INFORMATIONAL_NOTE)


def _latest_hrv_source(session: Session) -> tuple[str | None, date | None, float | None, float | None]:
    # A narrow latest-source query avoids treating an absent status as a capability claim.
    row = session.execute(
        select(DailyHealth).where(DailyHealth.hrv_status.is_not(None)).order_by(DailyHealth.day.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None or not isinstance(row.hrv_status, str) or not row.hrv_status.strip():
        return None, None, None, None
    low = _valid_value("hrv_overnight", row.hrv_baseline_low)
    high = _valid_value("hrv_overnight", row.hrv_baseline_high)
    if low is None or high is None:
        low = high = None
    return row.hrv_status, row.day, low, high


def build_recovery_health_trend_report(
    session: Session, *, as_of_day: date, overnight_today_ready: bool,
) -> RecoveryHealthTrendReport:
    """Build a tenant-session-local report with bounded, read-only SQL queries."""
    overnight_end_day = as_of_day if overnight_today_ready else as_of_day - timedelta(days=1)
    full_day_end_day = as_of_day - timedelta(days=1)
    start_day = min(overnight_end_day, full_day_end_day) - timedelta(days=27)
    end_day = max(overnight_end_day, full_day_end_day)
    sleeps = list(session.execute(select(Sleep).where(Sleep.day >= start_day, Sleep.day <= overnight_end_day).order_by(Sleep.day)).scalars())
    health = list(session.execute(select(DailyHealth).where(DailyHealth.day >= start_day, DailyHealth.day <= end_day).order_by(DailyHealth.day)).scalars())
    hrv_status, hrv_source_day, hrv_baseline_low, hrv_baseline_high = _latest_hrv_source(session)

    sleep_values = {
        "sleep_duration": [NumericObservation(row.day, (row.total_s or 0) / 3600) for row in sleeps],
        "sleep_score": [NumericObservation(row.day, row.score) for row in sleeps],
    }
    health_fields = {
        "hrv_overnight": "hrv_overnight", "resting_hr": "resting_hr", "stress_avg": "stress_avg",
        "body_battery_high": "body_battery_high", "body_battery_low": "body_battery_low",
        "body_battery_charged": "body_battery_charged", "body_battery_drained": "body_battery_drained",
        "recovery_time": "recovery_time_minutes", "steps": "steps",
    }
    health_values = {key: [NumericObservation(row.day, getattr(row, field)) for row in health] for key, field in health_fields.items()}
    trends: list[RecoveryHealthTrend] = []
    for key, label, unit in _METRICS:
        observations = sleep_values.get(key, health_values.get(key, []))
        endpoint = overnight_end_day if key in {"sleep_duration", "sleep_score", "hrv_overnight", "resting_hr", "recovery_time"} else full_day_end_day
        trends.append(build_trend(
            key, label, unit, observations, end_day=endpoint,
            source_status=hrv_status if key == "hrv_overnight" else None,
            source_day=hrv_source_day if key == "hrv_overnight" else None,
            source_baseline_low=hrv_baseline_low if key == "hrv_overnight" else None,
            source_baseline_high=hrv_baseline_high if key == "hrv_overnight" else None,
        ))
    by_key = {trend.key: trend for trend in trends}
    return RecoveryHealthTrendReport(
        as_of_day=as_of_day, overnight_end_day=overnight_end_day, full_day_end_day=full_day_end_day,
        trends=tuple(trends), sleep_timing=build_sleep_timing_trend(sleeps, end_day=overnight_end_day),
        body_battery=BodyBatteryTrend(by_key["body_battery_high"], by_key["body_battery_low"], by_key["body_battery_charged"], by_key["body_battery_drained"]),
    )
