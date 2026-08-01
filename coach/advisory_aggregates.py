"""Bounded, read-only aggregate facts for the Ask Coach v3 context.

This module is deliberately a local read model.  It has no provider, Telegram,
calendar, scheduling, notification, or decision-engine dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from coach.exercises import GARMIN_EXERCISES
from db import Activity, ActivityProgramMatch, DailyHealth, ExerciseSet, PlannedSession, ProgramSession, TrainingProgram
from metrics.recovery_trends import build_recovery_health_trend_report
from metrics.slow_metric_history import build_slow_metric_history_report
from metrics import training_aggregates as shared

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_INACTIVE = ("cancelled", "completed", "replaced_by_active_recovery", "rest_selected")


@dataclass(frozen=True)
class DomainCount:
    key: str
    count: int


@dataclass(frozen=True)
class AggregateWindow:
    start_day: date
    end_day: date
    activity_count: int
    active_days: int
    duration_minutes: int | None
    duration_valid_activities: int
    activity_domains: tuple[DomainCount, ...]
    program_sessions_completed: int | None
    unmatched_strength_activities: int
    steps_total: int | None
    steps_valid_days: int
    moderate_minutes: int | None
    vigorous_minutes: int | None
    intensity_valid_days: int


@dataclass(frozen=True)
class StrengthHighlight:
    label: str
    reps: int
    current_weight_kg: float
    prior_weight_kg: float
    delta_kg: float


@dataclass(frozen=True)
class RecoveryAggregateFact:
    key: str
    unit: str
    recent_median: float | None
    baseline_median: float | None
    delta: float | None
    direction: str
    recent_valid_days: int
    baseline_valid_days: int
    coverage: str
    latest_value: float | None
    latest_day: date | None


@dataclass(frozen=True)
class SleepTimingAggregate:
    key: str
    unit: str
    recent_variability_median: float | None
    baseline_variability_median: float | None
    delta: float | None
    direction: str
    recent_valid_days: int
    baseline_valid_days: int
    coverage: str
    latest_day: date | None
    recent_median_bedtime: str | None
    recent_median_wake_time: str | None


@dataclass(frozen=True)
class SlowFitnessAggregate:
    key: str
    capability_state: str
    current_value: float | str | None
    current_observed_on: date | None
    previous_value: float | str | None
    previous_observed_on: date | None


@dataclass(frozen=True)
class AskCoachAggregateContext:
    recent_7_days: AggregateWindow
    prior_7_days: AggregateWindow
    recent_28_days: AggregateWindow
    strength_highlights: tuple[StrengthHighlight, ...]
    recovery_trends: tuple[RecoveryAggregateFact, ...]
    slow_fitness: tuple[SlowFitnessAggregate, ...]


def _clean(value: object, maximum: int = 48) -> str | None:
    if not isinstance(value, str):
        return None
    value = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    if not value:
        return None
    return value[:maximum].rstrip()


def _finite(value: object) -> float | None:
    return shared.finite(value)


def _nonnegative_integer(value: object) -> int | None:
    return shared.nonnegative_integer(value)


def _positive_integer(value: object) -> int | None:
    return shared.positive_integer(value)


def normalize_activity_domain(value: object) -> str:
    return shared.normalize_activity_domain(value)


def _display_weight(value: object) -> float | None:
    return shared.display_weight_kg(value)


_GENERIC = frozenset({"exercise", "unknown", "other", "generic", "strength", "unknown_exercise", "other_exercise", "generic_exercise", "strength_exercise", "unnamed_exercise"})
_CATEGORIES = frozenset(item["category"] for item in GARMIN_EXERCISES.values())


def _source_identity(value: object) -> tuple[str, str] | None:
    cleaned = _clean(value, 96)
    if not cleaned:
        return None
    token = re.sub(r"[^a-z0-9]+", "_", cleaned.casefold()).strip("_")
    return None if not token or token in _GENERIC else (token, cleaned)


def exact_strength_identity(row: ExerciseSet) -> tuple[str, str] | None:
    """Phase 4G exact catalog/custom identity, never a broad category guess."""
    return shared.exact_strength_identity(row)


def _activity_day(row: Activity) -> date | None:
    return row.start_time.date() if isinstance(row.start_time, datetime) else None


def _aggregate_window(session: Session, start: date, end: date, program: TrainingProgram | None) -> AggregateWindow:
    rows = list(session.query(Activity).filter(Activity.start_time >= datetime.combine(start, time.min), Activity.start_time <= datetime.combine(end, time.max)).order_by(Activity.start_time, Activity.id))
    ids = [row.id for row in rows]
    matches = list(session.query(ActivityProgramMatch.activity_id, ActivityProgramMatch.program_id).filter(ActivityProgramMatch.activity_id.in_(ids)).all()) if ids else []
    any_matched = {activity_id for activity_id, _ in matches}
    program_matched = {activity_id for activity_id, program_id in matches if program and program_id == program.id}
    durations = [_finite(row.duration_s) for row in rows]
    valid = [item for item in durations if item is not None and item >= 0]
    domains: dict[str, int] = {}
    for row in rows:
        domain = normalize_activity_domain(row.activity_type)
        domains[domain] = domains.get(domain, 0) + 1
    health = list(session.query(DailyHealth).filter(DailyHealth.day >= start, DailyHealth.day <= end).order_by(DailyHealth.day))
    steps = [_nonnegative_integer(row.steps) for row in health]
    moderate = [_nonnegative_integer(row.daily_moderate_intensity_minutes) for row in health]
    vigorous = [_nonnegative_integer(row.daily_vigorous_intensity_minutes) for row in health]
    return AggregateWindow(
        start, end, len(rows), len({day for row in rows if (day := _activity_day(row))}),
        int(round(sum(valid) / 60)) if valid else None, len(valid),
        tuple(DomainCount(key, count) for key, count in sorted(domains.items(), key=lambda item: (-item[1], item[0]))[:4]),
        len(program_matched) if program else None,
        sum(1 for row in rows if normalize_activity_domain(row.activity_type) == "strength" and row.id not in any_matched),
        sum(item for item in steps if item is not None) if any(item is not None for item in steps) else None,
        sum(item is not None for item in steps),
        sum(item for item in moderate if item is not None) if any(item is not None for item in moderate) else None,
        sum(item for item in vigorous if item is not None) if any(item is not None for item in vigorous) else None,
        sum(left is not None or right is not None for left, right in zip(moderate, vigorous)),
    )


def _strength_highlights(session: Session, start: date, end: date) -> tuple[StrengthHighlight, ...]:
    prior_start = start - timedelta(days=7)
    rows = session.query(ExerciseSet, Activity.start_time).join(Activity, ExerciseSet.activity_id == Activity.id).filter(Activity.start_time >= datetime.combine(prior_start, time.min), Activity.start_time <= datetime.combine(end, time.max)).all()
    result = shared.build_strength_comparisons(rows, current_start=start, current_end=end)
    return tuple(StrengthHighlight(item.label, item.reps, item.current_weight_kg, item.prior_weight_kg, item.delta_kg) for item in result.candidates[:3])


def _recovery(session: Session, as_of_day: date, overnight_today_ready: bool) -> tuple[RecoveryAggregateFact | SleepTimingAggregate, ...]:
    report = build_recovery_health_trend_report(session, as_of_day=as_of_day, overnight_today_ready=overnight_today_ready)
    allowed = {"sleep_duration", "sleep_score", "hrv_overnight", "resting_hr", "stress_avg", "body_battery_high"}
    result: list[RecoveryAggregateFact | SleepTimingAggregate] = [RecoveryAggregateFact(item.key, item.unit, item.recent.median, item.baseline.median, item.delta, item.direction.value, item.recent.valid_days, item.baseline.valid_days, item.coverage.value, item.latest_value, item.latest_day) for item in report.trends if item.key in allowed]
    timing = report.sleep_timing
    if timing:
        result.append(SleepTimingAggregate("sleep_timing_variability", "minutes", timing.recent.median, timing.baseline.median, timing.delta, timing.direction.value, timing.recent.valid_days, timing.baseline.valid_days, timing.coverage.value, timing.latest_day, timing.recent_bedtime.isoformat() if timing.recent_bedtime else None, timing.recent_wake_time.isoformat() if timing.recent_wake_time else None))
    return tuple(result)


def _slow_fitness(session: Session, as_of_day: date) -> tuple[SlowFitnessAggregate, ...]:
    report = build_slow_metric_history_report(session, as_of_day=as_of_day)
    numeric = (("fitness_age", report.fitness_age), ("target_fitness_age", report.target_fitness_age), ("vo2_running", report.vo2_running), ("vo2_cycling", report.vo2_cycling))
    result = []
    for key, history in numeric:
        current_day = next((point.observed_on for point in reversed(history.points) if point.value == history.current_value), None)
        previous_day = next((point.observed_on for point in reversed(history.points) if point.value == history.previous_value and point.observed_on != current_day), None)
        result.append(SlowFitnessAggregate(key, history.capability_state.lower(), history.current_value, current_day, history.previous_value, previous_day))
    status = report.training_status
    result.append(SlowFitnessAggregate("training_status", status.capability_state.lower(), status.current_status if status.state == "SUPPORTED_WITH_DATA" else None, status.current_day, None, None))
    return tuple(result)


def build_ask_coach_aggregate_context(session: Session, *, as_of_day: date, overnight_today_ready: bool) -> AskCoachAggregateContext:
    """Return fixed-window tenant-local aggregates without changing ORM state."""
    program = session.query(TrainingProgram).filter(TrainingProgram.active.is_(True)).order_by(TrainingProgram.id.desc()).first()
    recent_start, prior_start, month_start = as_of_day - timedelta(days=6), as_of_day - timedelta(days=13), as_of_day - timedelta(days=27)
    return AskCoachAggregateContext(
        _aggregate_window(session, recent_start, as_of_day, program),
        _aggregate_window(session, prior_start, recent_start - timedelta(days=1), program),
        _aggregate_window(session, month_start, as_of_day, program),
        _strength_highlights(session, recent_start, as_of_day),
        _recovery(session, as_of_day, overnight_today_ready),
        _slow_fitness(session, as_of_day),
    )


def aggregate_dict(value: object) -> dict:
    """JSON-ready nested dataclass representation with ISO local dates."""
    def convert(item):
        if isinstance(item, date): return item.isoformat()
        if isinstance(item, dict): return {key: convert(value) for key, value in item.items()}
        if isinstance(item, tuple): return [convert(entry) for entry in item]
        if hasattr(item, "__dataclass_fields__"): return {key: convert(value) for key, value in asdict(item).items()}
        return item
    return convert(value)
