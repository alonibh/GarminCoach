"""Local, typed, deterministic weekly-summary reporting.

This module deliberately contains calculation and rendering only. It converts
one tenant-local SQLAlchemy session into immutable facts and then plain text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
import re

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from coach.exercises import exercise_metadata
from coach.onboarding import active_program
from coach.planned_session_status import INACTIVE_ORIGINAL_SESSION_STATUSES
from db import (
    Activity, ActivityProgramMatch, DailyHealth, ExerciseSet, PlannedSession,
    ProgramCursor, ProgramSession,
)
from metrics.recovery_trends import TrendCoverage, TrendDirection, build_recovery_health_trend_report
from metrics.slow_metric_history import build_slow_metric_history_report


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_FOOTER = "Informational only; this summary does not change your workout."


class WeeklySummaryValidationError(ValueError):
    """A payload or local-date identity that is unsafe to summarize."""


class WeeklySummaryStaleError(WeeklySummaryValidationError):
    """A valid Saturday whose outbox delivery window has expired."""


def validate_week_end(value: object, *, local_day: date) -> date:
    """Validate one canonical, athlete-local Saturday identity without clocks."""
    if type(local_day) is not date or type(value) is not date:
        raise WeeklySummaryValidationError("invalid weekly date")
    if value.weekday() != 5 or value > local_day:
        raise WeeklySummaryValidationError("invalid weekly date")
    if local_day > value + timedelta(days=6):
        raise WeeklySummaryStaleError("expired weekly date")
    return value


def weekly_overnight_ready(session: Session, *, week_end: date, local_delivery_day: date) -> bool:
    """Use freshness only for Saturday itself; historical weeks are complete."""
    validate_week_end(week_end, local_day=local_delivery_day)
    if week_end < local_delivery_day:
        return True
    from metrics.freshness import proactive_metrics_ready
    return proactive_metrics_ready(session, day=week_end)


@dataclass(frozen=True)
class ActivityDomainCount:
    key: str
    label: str
    count: int


@dataclass(frozen=True)
class WeeklyTrainingAggregate:
    program_completed: int
    program_target: int | None
    incomplete_planned: int
    unmatched_strength: int
    total_activities: int
    total_duration_minutes: int | None
    activity_domains: tuple[ActivityDomainCount, ...]
    has_active_program: bool


@dataclass(frozen=True)
class WeeklyMovementAggregate:
    steps_total: int | None
    steps_valid_days: int
    moderate_minutes: int | None
    vigorous_minutes: int | None
    intensity_valid_days: int


@dataclass(frozen=True)
class WeeklyStrengthHighlight:
    exercise_key: str
    label: str
    reps: int
    current_weight_kg: float
    prior_weight_kg: float
    delta_kg: float


@dataclass(frozen=True)
class WeeklyRecoveryHighlight:
    key: str
    text: str
    coverage_text: str


@dataclass(frozen=True)
class WeeklyFitnessHighlight:
    key: str
    text: str


@dataclass(frozen=True)
class WeeklySummaryReport:
    week_start: date
    week_end: date
    training: WeeklyTrainingAggregate
    movement: WeeklyMovementAggregate
    strength_highlights: tuple[WeeklyStrengthHighlight, ...]
    recovery_highlights: tuple[WeeklyRecoveryHighlight, ...]
    fitness_highlights: tuple[WeeklyFitnessHighlight, ...]
    next_session_name: str | None
    source_notes: tuple[str, ...] = ()


def _clean(value: object, maximum: int = 48) -> str | None:
    if not isinstance(value, str):
        return None
    value = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    if not value:
        return None
    return value if len(value) <= maximum else value[: maximum - 1].rstrip() + "\u2026"


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if isfinite(value) else None


def _nonnegative_integer(value: object) -> int | None:
    number = _finite(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _positive_integer(value: object) -> int | None:
    number = _nonnegative_integer(value)
    return number if number and number > 0 else None


def _domain(value: object) -> tuple[str, str]:
    token = _SPACE.sub("_", str(value or "").strip().lower().replace("-", "_")).strip("_")
    if "strength" in token or "weight" in token:
        return "strength", "strength"
    if "run" in token:
        return "running", "running"
    if "cycl" in token or "bike" in token:
        return "cycling", "cycling"
    if "walk" in token or "hike" in token:
        return "walking", "walking"
    if "soccer" in token or "football" in token:
        return "soccer", "soccer"
    if "swim" in token:
        return "swimming", "swimming"
    return "other", "other"


def _duration_minutes(activities: list[Activity]) -> int | None:
    valid = [_finite(row.duration_s) for row in activities]
    seconds = sum(value for value in valid if value is not None and value >= 0)
    return int(round(seconds / 60)) if any(value is not None and value >= 0 for value in valid) else None


def _display_weight_kg(value: object) -> float | None:
    """Round a finite positive stored kilogram value to the 250 g display grid."""
    number = _finite(value)
    if number is None or number <= 0:
        return None
    try:
        units = (Decimal(str(number)) * Decimal("1000") / Decimal("250")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        )
        grams = units * Decimal("250")
    except (InvalidOperation, ValueError):
        return None
    return float(grams / Decimal("1000"))


def _next_session_name_read_only(session: Session, *, program) -> str | None:
    """Read an existing cursor target without repairing program state."""
    if program is None:
        return None
    cursor = session.get(ProgramCursor, program.id)
    if cursor is None or cursor.next_program_session_id is None:
        return None
    item = session.get(ProgramSession, cursor.next_program_session_id)
    if item is None or item.program_id != program.id:
        return None
    return _clean(item.name)


_GENERIC_EXERCISE_IDENTITIES = frozenset({"exercise", "unknown", "other", "generic", "strength"})


def _specific_exercise_source(value: object) -> tuple[str, str] | None:
    """Return a bounded source token and display label, excluding placeholders."""
    if not isinstance(value, str):
        return None
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    if not cleaned or len(cleaned) > 96:
        return None
    token = re.sub(r"[^a-z0-9]+", "_", cleaned.casefold()).strip("_")
    if not token or token in _GENERIC_EXERCISE_IDENTITIES:
        return None
    return token, _clean(cleaned, 48) or ""


def _weekly_exercise_identity(row: ExerciseSet) -> tuple[str, str] | None:
    """Use catalog identity, or an unambiguous custom source composite only."""
    name = _specific_exercise_source(row.exercise_name)
    category = _specific_exercise_source(row.exercise_category)
    for candidate in (row.exercise_name, row.exercise_category):
        if isinstance(candidate, str):
            meta = exercise_metadata(candidate)
            if meta:
                label = _clean(meta.get("label"), 48)
                key = meta.get("key")
                if isinstance(key, str) and label:
                    return key, label
    if name and category:
        return f"custom:{category[0]}:{name[0]}", name[1]
    if name:
        return f"custom:name:{name[0]}", name[1]
    if category:
        return f"custom:category:{category[0]}", category[1]
    return None


def _activity_domains(activities: list[Activity]) -> tuple[ActivityDomainCount, ...]:
    counts: dict[tuple[str, str], int] = {}
    for activity in activities:
        key, label = _domain(activity.activity_type)
        counts[(key, label)] = counts.get((key, label), 0) + 1
    return tuple(
        ActivityDomainCount(key, label, count)
        for (key, label), count in sorted(counts.items(), key=lambda item: (-item[1], item[0][1]))
    )


def _strength_highlights(session: Session, start: date, end: date) -> tuple[WeeklyStrengthHighlight, ...]:
    previous_start = start - timedelta(days=7)
    rows = session.query(ExerciseSet, Activity.start_time).join(
        Activity, ExerciseSet.activity_id == Activity.id,
    ).filter(
        Activity.start_time >= datetime.combine(previous_start, time.min),
        Activity.start_time <= datetime.combine(end, time.max),
    ).all()
    current: dict[tuple[str, int], tuple[float, str]] = {}
    prior: dict[tuple[str, int], float] = {}
    for row, started in rows:
        if not isinstance(started, datetime) or not isinstance(row.set_type, str) or row.set_type.strip().upper() not in {"ACTIVE", "WORK"}:
            continue
        weight, reps = _finite(row.weight_kg), _positive_integer(row.reps)
        if weight is None or weight <= 0 or reps is None:
            continue
        identity = _weekly_exercise_identity(row)
        if identity is None:
            continue
        canonical, label = identity
        key = (canonical, reps)
        if start <= started.date() <= end:
            if key not in current or weight > current[key][0]:
                current[key] = (weight, label)
        elif previous_start <= started.date() < start:
            prior[key] = max(prior.get(key, 0.0), weight)
    highlights = []
    for key, (weight, label) in current.items():
        prior_weight = prior.get(key)
        current_display, prior_display = _display_weight_kg(weight), _display_weight_kg(prior_weight)
        if current_display is None or prior_display is None or current_display <= prior_display:
            continue
        highlights.append(WeeklyStrengthHighlight(
            key[0], label, key[1], current_display, prior_display, current_display - prior_display,
        ))
    return tuple(sorted(highlights, key=lambda item: (-item.delta_kg, item.label.casefold(), item.reps))[:2])


def _recovery_highlights(session: Session, end: date, overnight_today_ready: bool) -> tuple[WeeklyRecoveryHighlight, ...]:
    report = build_recovery_health_trend_report(
        session, as_of_day=end, overnight_today_ready=overnight_today_ready,
    )
    eligible = {
        trend.key: trend for trend in report.trends
        if trend.key in {"sleep_duration", "sleep_score", "hrv_overnight", "resting_hr", "stress_avg", "body_battery_high"}
        and trend.coverage == TrendCoverage.SUFFICIENT
        and trend.direction != TrendDirection.INSUFFICIENT_DATA
        and trend.recent.median is not None and trend.baseline.median is not None
    }
    order = ["sleep_duration"]
    order += [key for key in ("sleep_score", "hrv_overnight", "resting_hr", "stress_avg", "body_battery_high")
              if key in eligible and eligible[key].direction != TrendDirection.STABLE]
    order += [key for key in ("sleep_duration", "hrv_overnight") if key in eligible and key not in order]
    result: list[WeeklyRecoveryHighlight] = []
    labels = {"sleep_duration": "Sleep", "sleep_score": "Sleep Score", "hrv_overnight": "HRV",
              "resting_hr": "Resting HR", "stress_avg": "Stress", "body_battery_high": "Body Battery high"}
    for key in order:
        trend = eligible.get(key)
        if trend is None:
            continue
        if key == "sleep_duration":
            median = _hours(float(trend.recent.median))
            delta = _minutes(abs(float(trend.delta or 0)))
            change = "similar" if trend.direction == TrendDirection.STABLE else f"{delta} {'higher' if (trend.delta or 0) > 0 else 'lower'}"
        else:
            median = _number(float(trend.recent.median))
            unit = " ms" if key == "hrv_overnight" else ""
            delta = _number(abs(float(trend.delta or 0)))
            change = "similar" if trend.direction == TrendDirection.STABLE else f"{delta}{unit} {'higher' if (trend.delta or 0) > 0 else 'lower'}"
            median += unit
        comparison = "to prior 21-day median" if change == "similar" else f"than prior 21-day median"
        text = f"{labels[key]}: {median} median · {change} {comparison}."
        coverage = f"{trend.recent.valid_days + trend.baseline.valid_days}/28 days"
        result.append(WeeklyRecoveryHighlight(key, text, coverage))
        if len(result) == 3:
            break
    return tuple(result)


def _changed_numeric(history, start: date, end: date) -> tuple[float, float] | None:
    points = history.points
    for index in range(len(points) - 1, 0, -1):
        point = points[index]
        if start <= point.observed_on <= end and point.value != points[index - 1].value:
            return points[index - 1].value, point.value
    return None


def _fitness_highlights(session: Session, start: date, end: date) -> tuple[WeeklyFitnessHighlight, ...]:
    report = build_slow_metric_history_report(session, as_of_day=end)
    result: list[WeeklyFitnessHighlight] = []
    for key, label, history, unit in (
        ("vo2_running", "Running VO\u2082 max", report.vo2_running, " ml/kg/min"),
        ("vo2_cycling", "Cycling VO\u2082 max", report.vo2_cycling, " ml/kg/min"),
        ("fitness_age", "Fitness Age", report.fitness_age, " years"),
    ):
        change = _changed_numeric(history, start, end)
        if change:
            result.append(WeeklyFitnessHighlight(key, f"{label}: {_number(change[0])} -> {_number(change[1])}{unit}."))
    target_change = _changed_numeric(report.target_fitness_age, start, end)
    if target_change:
        result.append(WeeklyFitnessHighlight("target_fitness_age", f"Target Fitness Age: {_number(target_change[0])} -> {_number(target_change[1])} years."))
    training = report.training_status
    if training.state == "SUPPORTED_WITH_DATA" and training.current_status:
        status = _clean(training.current_status)
        if status:
            result.append(WeeklyFitnessHighlight("training_status", f"Garmin Training Status: {status}."))
    # Training Status replaces target Fitness Age if retaining it would hide status.
    if len(result) > 3 and result[-1].key == "training_status":
        result = [item for item in result if item.key != "target_fitness_age"]
    return tuple(result[:3])


def build_weekly_summary_report(
    session: Session, *, week_end: date, generated_at: datetime, overnight_today_ready: bool,
) -> WeeklySummaryReport:
    """Build one local report for exactly seven local dates; never writes."""
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is not None:
        raise ValueError("weekly summary requires a date and naive local datetime")
    week_end = validate_week_end(week_end, local_day=generated_at.date())
    start = week_end - timedelta(days=6)
    start_dt, end_dt = datetime.combine(start, time.min), datetime.combine(week_end, time.max)
    activities = session.query(Activity).filter(Activity.start_time >= start_dt, Activity.start_time <= end_dt).all()
    activity_ids = [row.id for row in activities]
    program = active_program(session)
    active_matches = []
    all_matches = []
    if activity_ids and program:
        active_matches = session.query(ActivityProgramMatch.activity_id).filter(
            ActivityProgramMatch.activity_id.in_(activity_ids), ActivityProgramMatch.program_id == program.id,
        ).all()
    if activity_ids:
        all_matches = session.query(ActivityProgramMatch.activity_id).filter(
            ActivityProgramMatch.activity_id.in_(activity_ids),
        ).all()
    matched_ids = {row[0] for row in active_matches}
    any_matched_ids = {row[0] for row in all_matches}
    target: int | None = None
    if program:
        days = _positive_integer(program.days_per_week)
        target = days
        if target is None:
            target = session.query(ProgramSession).filter(
                ProgramSession.program_id == program.id, ProgramSession.session_role == "coach_strength",
                ProgramSession.is_addon.is_(False),
            ).count() or None
    incomplete = session.query(PlannedSession.id).outerjoin(
        ProgramSession, PlannedSession.program_session_id == ProgramSession.id,
    ).filter(
        PlannedSession.target_date >= start, PlannedSession.target_date <= week_end,
        PlannedSession.status.notin_(tuple(INACTIVE_ORIGINAL_SESSION_STATUSES)),
        func.lower(PlannedSession.activity_type) != "rest",
        func.lower(PlannedSession.intensity) != "recovery",
        or_(ProgramSession.id.is_(None), func.lower(ProgramSession.session_role) != "optional_recovery"),
    ).count()
    unmatched = sum(1 for row in activities if _domain(row.activity_type)[0] == "strength" and row.id not in any_matched_ids)
    health = session.query(DailyHealth).filter(DailyHealth.day >= start, DailyHealth.day <= week_end).all()
    steps = [_nonnegative_integer(row.steps) for row in health]
    moderate = [_nonnegative_integer(row.daily_moderate_intensity_minutes) for row in health]
    vigorous = [_nonnegative_integer(row.daily_vigorous_intensity_minutes) for row in health]
    intensity_days = sum(1 for a, b in zip(moderate, vigorous) if a is not None or b is not None)
    movement = WeeklyMovementAggregate(
        sum(value for value in steps if value is not None) if any(value is not None for value in steps) else None,
        sum(value is not None for value in steps),
        sum(value for value in moderate if value is not None) if any(value is not None for value in moderate) else None,
        sum(value for value in vigorous if value is not None) if any(value is not None for value in vigorous) else None,
        intensity_days,
    )
    next_name = _next_session_name_read_only(session, program=program)
    return WeeklySummaryReport(
        start, week_end,
        WeeklyTrainingAggregate(len(matched_ids), target, incomplete, unmatched, len(activities),
                                _duration_minutes(activities), _activity_domains(activities), bool(program)),
        movement, _strength_highlights(session, start, week_end),
        _recovery_highlights(session, week_end, overnight_today_ready),
        _fitness_highlights(session, start, week_end), next_name,
    )


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _hours(value: float) -> str:
    minutes = int(round(value * 60))
    return f"{minutes // 60}h {minutes % 60}m"


def _minutes(value: float) -> str:
    return f"{int(round(value * 60))} min"


def _duration(value: int) -> str:
    return f"{value // 60}h {value % 60:02d}m" if value >= 60 else f"{value}m"


def _plural(value: int, singular: str, plural: str | None = None) -> str:
    return singular if value == 1 else plural or singular + "s"


def render_weekly_summary(report: WeeklySummaryReport) -> str:
    """Render plain text with deterministic omission before the hard bounds."""
    header = f"Weekly summary \u00b7 {report.week_start.strftime('%b')} {report.week_start.day}\u2013{report.week_end.strftime('%b')} {report.week_end.day}, {report.week_end.year}"
    training = report.training
    if not training.has_active_program:
        program_line = "Program: no active program."
    elif training.program_target is None:
        program_line = f"Program: {training.program_completed} {_plural(training.program_completed, 'session')} completed."
    elif training.program_completed <= training.program_target:
        program_line = f"Program: {training.program_completed} of {training.program_target} sessions completed."
    else:
        program_line = f"Program: {training.program_completed} sessions completed; weekly target {training.program_target}."
    groups: list[tuple[str, list[str], bool]] = [("Training", [program_line], True)]
    training_optional: list[str] = []
    if training.incomplete_planned:
        training_optional.append(f"Schedule: {training.incomplete_planned} planned {_plural(training.incomplete_planned, 'session')} remained incomplete.")
    if training.unmatched_strength:
        training_optional.append(f"Additional strength: {training.unmatched_strength} unmatched {_plural(training.unmatched_strength, 'activity')}.")
    domains = list(training.activity_domains)
    if domains:
        shown, rest = domains[:4], domains[4:]
        domain_text = ", ".join(f"{item.count} {item.label}" for item in shown)
        if rest:
            domain_text += f", +{sum(item.count for item in rest)} other"
        duration = f" \u00b7 {_duration(training.total_duration_minutes)} recorded" if training.total_duration_minutes is not None else ""
        training_optional.append(f"Activity: {training.total_activities} {_plural(training.total_activities, 'activity')}" + duration + f" ({domain_text}).")
    else:
        training_optional.append(f"Activity: {training.total_activities} {_plural(training.total_activities, 'activity')}.")
    groups[0][1].extend(training_optional)
    movement_lines: list[str] = []
    if report.movement.steps_total is not None:
        movement_lines.append(f"Steps: {report.movement.steps_total:,} recorded \u00b7 {report.movement.steps_valid_days}/7 days.")
    if report.movement.intensity_valid_days:
        moderate, vigorous = report.movement.moderate_minutes, report.movement.vigorous_minutes
        chunks = []
        if moderate is not None:
            chunks.append(f"{moderate} moderate")
        if vigorous is not None:
            chunks.append(f"{vigorous} vigorous")
        movement_lines.append(f"Intensity: {' + '.join(chunks)} min \u00b7 {report.movement.intensity_valid_days}/7 days.")
    if movement_lines:
        groups.append(("Movement", movement_lines, False))
    if report.strength_highlights:
        groups.append(("Strength", [
            f"{item.label} {_number(item.current_weight_kg)} kg \u00d7 {item.reps}, up {_number(item.delta_kg)} kg from prior week."
            for item in report.strength_highlights
        ], False))
    if report.recovery_highlights:
        groups.append(("Recovery trends", [f"{item.text[:-1]} \u00b7 {item.coverage_text}." for item in report.recovery_highlights], False))
    if report.fitness_highlights:
        groups.append(("Fitness", [item.text for item in report.fitness_highlights], False))
    if report.next_session_name:
        groups.append(("", [f"Next: {report.next_session_name}."], False))

    def assemble() -> list[str]:
        result = [header]
        for title, lines, _mandatory in groups:
            if lines:
                if title:
                    result.append(title)
                result.extend(lines)
        result.append(_FOOTER)
        return result

    # Remove optional data from the end before ever sacrificing the contract's
    # header, training line, or footer.  Empty optional sections disappear too.
    lines = assemble()
    while len(lines) > 18 or len("\n".join(lines)) > 3500:
        removed = False
        for index in range(len(groups) - 1, -1, -1):
            title, values, mandatory = groups[index]
            if mandatory and len(values) <= 1:
                continue
            if values:
                values.pop()
                removed = True
                break
        if not removed:
            break
        lines = assemble()
    return "\n".join(_clean(line, 3500) or "" for line in lines)
