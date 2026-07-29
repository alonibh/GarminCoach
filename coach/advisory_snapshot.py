"""Read-only, privacy-bounded athlete facts for Ask Coach."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

import config
from coach.onboarding import active_program
from db import (
    Activity,
    ActivityProgramMatch,
    AthleteProfile,
    DailyHealth,
    DailyMetrics,
    DecisionRecord,
    ObservationFreshness,
    PlannedSession,
    ProgramCursor,
    ProgramSession,
    SessionExercise,
    Sleep,
    SyncState,
)
from tenant_context import current_tenant

SNAPSHOT_VERSION = "ask-coach-v2"
RECOVERY_METRICS = (
    "sleep_duration_hours",
    "sleep_score",
    "hrv",
    "hrv_baseline_low",
    "hrv_baseline_high",
    "hrv_weekly_avg",
    "garmin_hrv_status",
    "hrv_7d_coverage_days",
    "resting_heart_rate",
    "body_battery",
    "body_battery_charged",
    "body_battery_drained",
    "training_readiness",
    "recovery_time_minutes",
    "recovery_time_change_phrase",
    "stress",
    "acute_load",
    "chronic_load",
    "acwr",
)


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _state(session: Session, key: str) -> str | None:
    row = session.get(SyncState, key)
    return row.value if row and row.value else None


def _json(value: str | None, fallback):
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _metric(value, observed_at: datetime | None, generated_at: datetime) -> dict:
    if value is None:
        return {"value": None, "observed_at": None, "status": "missing"}
    status = "available"
    if observed_at is None:
        status = "incomplete"
    else:
        normalized = (
            observed_at.replace(tzinfo=timezone.utc)
            if observed_at.tzinfo is None
            else observed_at.astimezone(timezone.utc)
        )
        if generated_at - normalized > timedelta(days=2):
            status = "stale"
    return {
        "value": value,
        "observed_at": _utc_iso(observed_at),
        "status": status,
    }


def _day_observed(day: date | None) -> datetime | None:
    if day is None:
        return None
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _bounded(items: list[dict], maximum: int) -> dict:
    maximum = max(0, maximum)
    kept = items[:maximum]
    omitted = max(0, len(items) - len(kept))
    return {"items": kept, "truncated": omitted > 0, "omitted_count": omitted}


def _official_recommendation(
    session: Session,
) -> tuple[dict, str | None]:
    record = (
        session.query(DecisionRecord)
        .order_by(DecisionRecord.evaluated_at.desc())
        .first()
    )
    if record is None:
        return {
            "status": "unavailable",
            "source": "garmincoach_official",
            "verdict": None,
            "score": None,
            "recommended_session": None,
            "evaluated_at": None,
            "reasons": [],
            "missing_inputs": ["No official recommendation has been evaluated"],
        }, None
    result = _json(record.result_json, {})
    session_name = (
        result.get("planned_session_name")
        or result.get("next_program_session_name")
    )
    duration = None
    session_type = None
    if session_name:
        program_session = (
            session.query(ProgramSession)
            .filter(ProgramSession.name == session_name)
            .order_by(ProgramSession.id.desc())
            .first()
        )
        if program_session:
            duration = program_session.duration_min
            session_type = program_session.sport_type or None
    decision_type = result.get("decision_type") or record.decision_type
    readiness = result.get("readiness_score")
    category = result.get("readiness_category")
    verdict = decision_type.replace("_", " ").title()
    if category:
        verdict = f"{verdict} — {str(category).lower()} readiness"
    missing = result.get("missing_observations") or _json(record.missing_json, [])
    missing_inputs = [
        item.get("signal", "Unknown input") if isinstance(item, dict) else str(item)
        for item in missing
    ]
    reasons = [
        str(item).replace("_", " ").title()
        for item in (result.get("reason_codes") or _json(record.reason_codes_json, []))
    ]
    return {
        "status": "available",
        "source": "garmincoach_official",
        "verdict": verdict,
        "score": readiness,
        "recommended_session": (
            {
                "name": session_name,
                "duration_min": duration,
                "session_type": session_type,
            }
            if session_name
            else None
        ),
        "evaluated_at": _utc_iso(record.evaluated_at),
        "reasons": reasons,
        "missing_inputs": missing_inputs,
    }, _utc_iso(record.evaluated_at)


def _freshness(session: Session, recommendation_at: str | None) -> dict:
    latest_observation = (
        session.query(ObservationFreshness)
        .order_by(ObservationFreshness.fetched_at.desc())
        .first()
    )
    metrics = (
        session.query(DailyMetrics).order_by(DailyMetrics.day.desc()).first()
    )
    sleep = session.query(Sleep).order_by(Sleep.day.desc()).first()
    health = session.query(DailyHealth).order_by(DailyHealth.day.desc()).first()
    return {
        "last_sync_at": _utc_iso(_parse_datetime(_state(session, "last_sync_at"))),
        "device_last_upload_at": _utc_iso(
            _parse_datetime(_state(session, "device_last_upload"))
        ),
        "health_last_updated": _utc_iso(
            latest_observation.fetched_at
            if latest_observation
            else _day_observed(health.day if health else None)
        ),
        "metrics_last_updated": _utc_iso(
            _day_observed(metrics.day if metrics else None)
        ),
        "sleep_last_updated": _utc_iso(
            sleep.sleep_end_time
            if sleep and sleep.sleep_end_time
            else _day_observed(sleep.day if sleep else None)
        ),
        "recommendation_evaluated_at": recommendation_at,
    }


def _profile(session: Session, generated_at: datetime) -> dict:
    profile = session.get(AthleteProfile, 1)
    birth = _state(session, "user_birth_date")
    birth_date = None
    try:
        birth_date = date.fromisoformat(birth) if birth else None
    except ValueError:
        pass
    age = None
    if birth_date:
        today = generated_at.date()
        age = today.year - birth_date.year - (
            (today.month, today.day) < (birth_date.month, birth_date.day)
        )
    weight = None
    try:
        raw_weight = _state(session, "user_weight")
        weight = float(raw_weight) if raw_weight is not None else None
    except ValueError:
        pass
    observed = _parse_datetime(_state(session, "last_sync_at"))
    goals = []
    equipment = []
    preferences = {}
    if profile:
        goals = [
            value
            for value in (profile.primary_goal, profile.goal_detail)
            if value and value.strip()
        ]
        equipment = [
            str(item) for item in _json(profile.equipment_access, []) if str(item)
        ]
        preferences = {
            "training_type": profile.training_type or None,
            "experience_level": profile.experience_level or None,
            "preferred_activities": _json(profile.preferred_activities, []),
            "availability": profile.availability or None,
            "timing": _json(profile.timing_preferences, {}),
            "scheduling": profile.scheduling_preferences or None,
        }
    return {
        "age": _metric(age, _day_observed(generated_at.date()), generated_at),
        "weight_kg": _metric(weight, observed, generated_at),
        "height_cm": _metric(None, None, generated_at),
        "goals": goals,
        "equipment": equipment,
        "preferences": preferences,
    }


def _recovery(session: Session, generated_at: datetime) -> dict:
    sleep = session.query(Sleep).order_by(Sleep.day.desc()).first()
    health = session.query(DailyHealth).order_by(DailyHealth.day.desc()).first()
    metrics = (
        session.query(DailyMetrics).order_by(DailyMetrics.day.desc()).first()
    )
    sleep_at = (
        sleep.sleep_end_time
        if sleep and sleep.sleep_end_time
        else _day_observed(sleep.day if sleep else None)
    )
    health_at = _day_observed(health.day if health else None)
    metrics_at = _day_observed(metrics.day if metrics else None)
    values = {
        "sleep_duration_hours": (
            round(sleep.total_s / 3600, 2)
            if sleep and sleep.total_s is not None
            else None,
            sleep_at,
        ),
        "sleep_score": (sleep.score if sleep else None, sleep_at),
        "hrv": (health.hrv_overnight if health else None, health_at),
        "hrv_baseline_low": (
            health.hrv_baseline_low if health else None,
            health_at,
        ),
        "hrv_baseline_high": (
            health.hrv_baseline_high if health else None,
            health_at,
        ),
        "hrv_weekly_avg": (health.hrv_weekly_avg if health else None, health_at),
        "garmin_hrv_status": (health.hrv_status if health else None, health_at),
        "hrv_7d_coverage_days": (health.hrv_7d_coverage_days if health else None, health_at),
        "resting_heart_rate": (health.resting_hr if health else None, health_at),
        "body_battery": (
            (
                health.body_battery_current
                if health and health.body_battery_current is not None
                else health.body_battery_high if health else None
            ),
            health_at,
        ),
        "body_battery_charged": (health.body_battery_charged if health else None, health_at),
        "body_battery_drained": (health.body_battery_drained if health else None, health_at),
        "training_readiness": (
            health.training_readiness if health else None,
            health_at if health and health.training_readiness is not None else None,
        ),
        "recovery_time_minutes": (health.recovery_time_minutes if health else None, health.recovery_time_observed_at if health else None),
        "recovery_time_change_phrase": (health.recovery_time_change_phrase if health else None, health.recovery_time_observed_at if health else None),
        "stress": (health.stress_avg if health else None, health_at),
        "acute_load": (metrics.acute_load if metrics else None, metrics_at),
        "chronic_load": (metrics.chronic_load if metrics else None, metrics_at),
        "acwr": (metrics.acwr if metrics else None, metrics_at),
    }
    return {
        name: _metric(values[name][0], values[name][1], generated_at)
        for name in RECOVERY_METRICS
    }


def _planned_sessions(session: Session, today: date) -> dict:
    rows = (
        session.query(PlannedSession)
        .filter(
            PlannedSession.target_date >= today,
            PlannedSession.status.notin_(("cancelled", "completed")),
        )
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time)
        .all()
    )
    items = [
        {
            "title": row.title,
            "target_date": row.target_date.isoformat(),
            "suggested_time": row.suggested_time or None,
            "duration_min": row.duration_min,
            "session_type": row.activity_type or None,
            "status": row.status,
            "official_program_session": row.program_session_id is not None,
        }
        for row in rows
    ]
    return _bounded(items, config.ASK_COACH_MAX_CALENDAR_EVENTS)


def _calendar(session: Session, today: date, tz: ZoneInfo) -> dict:
    items: list[dict] = []
    end_day = today + timedelta(days=7)
    rows = (
        session.query(PlannedSession)
        .filter(
            PlannedSession.target_date >= today,
            PlannedSession.target_date < end_day,
            PlannedSession.status.in_(("approved", "scheduled", "planned")),
        )
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time, PlannedSession.id)
        .all()
    )
    for row in rows:
        start_text = str(row.suggested_time or "00:00")[:5]
        try:
            start_clock = time.fromisoformat(start_text)
        except ValueError:
            start_clock = time.min
        start = datetime.combine(row.target_date, start_clock, tzinfo=tz)
        duration = int(row.duration_min or 0)
        items.append(
            {
                "title": str(row.title or "Workout")[:255],
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=duration)).isoformat(),
                "source": "garmincoach_workout",
            }
        )

    # This section intentionally contains GarminCoach workouts only. Private
    # calendar events are never part of the Ask Coach context.
    items.sort(key=lambda item: item["start_time"])
    return _bounded(items, config.ASK_COACH_MAX_CALENDAR_EVENTS)


def _recent_activities(
    session: Session, generated_at: datetime, tz: ZoneInfo
) -> tuple[dict, list[Activity]]:
    cutoff = generated_at.replace(tzinfo=None) - timedelta(days=14)
    rows = (
        session.query(Activity)
        .filter(Activity.start_time >= cutoff)
        .order_by(Activity.start_time.desc())
        .all()
    )
    matched_activity_ids = {
        row.completed_activity_id
        for row in session.query(PlannedSession).filter(
            PlannedSession.completed_activity_id.is_not(None)
        )
    }
    matched_activity_ids.update(
        row[0]
        for row in session.query(ActivityProgramMatch.activity_id).all()
    )
    items = []
    for row in rows:
        started = row.start_time
        if started and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        items.append(
            {
                "title": row.name or row.activity_type or "Activity",
                "activity_type": row.activity_type or None,
                "started_at": started.astimezone(tz).isoformat() if started else None,
                "duration_min": (
                    round(row.duration_s / 60, 1)
                    if row.duration_s is not None
                    else None
                ),
                "distance_km": (
                    round(row.distance_m / 1000, 2)
                    if row.distance_m is not None
                    else None
                ),
                "training_load": row.training_load,
                "average_heart_rate": row.avg_hr,
                "calories": row.calories,
                "completed_program_session": row.id in matched_activity_ids,
            }
        )
    return _bounded(items, config.ASK_COACH_MAX_RECENT_ACTIVITIES), rows


def _trends(rows: list[Activity], generated_at: datetime) -> dict:
    week_starts = [
        (generated_at.date() - timedelta(days=generated_at.weekday()))
        - timedelta(weeks=offset)
        for offset in reversed(range(6))
    ]
    duration_by_week: defaultdict[date, float] = defaultdict(float)
    activity_mix: Counter[str] = Counter()
    active_days: Counter[int] = Counter()
    for row in rows:
        if not row.start_time:
            continue
        activity_day = row.start_time.date()
        week_start = activity_day - timedelta(days=activity_day.weekday())
        duration_by_week[week_start] += float(row.duration_s or 0) / 3600
        activity_mix[row.activity_type or "other"] += 1
        active_days[activity_day.weekday()] += 1
    weekday_names = (
        "Mondays",
        "Tuesdays",
        "Wednesdays",
        "Thursdays",
        "Fridays",
        "Saturdays",
        "Sundays",
    )
    rest = [weekday_names[index] for index in range(7) if active_days[index] == 0]
    return {
        "weekly_duration_hours": [
            round(duration_by_week[week], 2) for week in week_starts
        ],
        "activity_mix": dict(activity_mix),
        "rest_days_pattern": ", ".join(rest) if rest else "No consistent rest day",
    }


def _trend_activities(
    session: Session, generated_at: datetime
) -> list[Activity]:
    cutoff = generated_at.replace(tzinfo=None) - timedelta(weeks=6)
    return (
        session.query(Activity)
        .filter(Activity.start_time >= cutoff)
        .order_by(Activity.start_time)
        .all()
    )


def _active_program(session: Session) -> dict | None:
    program = active_program(session)
    if program is None:
        return None
    cursor = session.get(ProgramCursor, program.id)
    next_session = (
        session.get(ProgramSession, cursor.next_program_session_id)
        if cursor and cursor.next_program_session_id
        else None
    )
    sessions = (
        session.query(ProgramSession)
        .filter(ProgramSession.program_id == program.id)
        .order_by(ProgramSession.sequence_order, ProgramSession.id)
        .all()
    )
    exercise_budget = max(0, config.ASK_COACH_MAX_PROGRAM_EXERCISES)
    omitted_exercises = 0
    session_items = []
    for program_session in sessions:
        exercises = (
            session.query(SessionExercise)
            .filter(SessionExercise.program_session_id == program_session.id)
            .order_by(SessionExercise.order_index, SessionExercise.id)
            .all()
        )
        allowed = max(0, exercise_budget)
        kept_exercises = exercises[:allowed]
        exercise_budget -= len(kept_exercises)
        omitted_exercises += len(exercises) - len(kept_exercises)
        session_items.append(
            {
                "sequence_order": program_session.sequence_order,
                "name": program_session.name,
                "duration_min": program_session.duration_min,
                "exercises": [
                    {
                        "order": exercise.order_index + 1,
                        "name": exercise.exercise_name,
                        "sets": exercise.sets,
                        "reps": exercise.reps,
                        "target_weight_kg": exercise.weight_kg,
                        "duration_seconds": exercise.duration_seconds,
                        "rest_seconds": exercise.rest_seconds,
                        "warmup": exercise.warmup_enabled,
                        "notes": (exercise.notes or "")[:500] or None,
                    }
                    for exercise in kept_exercises
                ],
            }
        )
    earliest = (
        session.query(PlannedSession.target_date)
        .filter(
            PlannedSession.program_session_id
            == (next_session.id if next_session else -1),
            PlannedSession.status.notin_(("cancelled", "completed")),
        )
        .order_by(PlannedSession.target_date)
        .scalar()
    )
    return {
        "name": program.name,
        "next_session": next_session.name if next_session else None,
        "earliest_eligible_date": earliest.isoformat() if earliest else None,
        "sessions": {
            "items": session_items,
            "truncated": omitted_exercises > 0,
            "omitted_count": omitted_exercises,
        },
    }


def build_advisory_snapshot(session: Session) -> dict:
    """Build the complete snapshot using read-only ORM queries only."""
    generated_at = datetime.now(timezone.utc)
    tenant = current_tenant()
    timezone_name = tenant.timezone if tenant and tenant.timezone else "UTC"
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        timezone_name = "UTC"
        local_timezone = ZoneInfo("UTC")
    today = generated_at.astimezone(local_timezone).date()
    with session.no_autoflush:
        recommendation, recommendation_at = _official_recommendation(session)
        recent, _ = _recent_activities(
            session, generated_at, local_timezone
        )
        trend_rows = _trend_activities(session, generated_at)
        snapshot = {
            "snapshot_version": SNAPSHOT_VERSION,
            "generated_at": _utc_iso(generated_at),
            "timezone": timezone_name,
            "official_recommendation": recommendation,
            "data_freshness": _freshness(session, recommendation_at),
            "profile": _profile(session, generated_at),
            "recovery": _recovery(session, generated_at),
            "planned_sessions": _planned_sessions(session, today),
            "calendar_next_7_days": _calendar(session, today, local_timezone),
            "recent_activities_14_days": recent,
            "training_trends_6_weeks": _trends(trend_rows, generated_at),
            "active_program": _active_program(session),
        }
    return snapshot


def serialize_advisory_snapshot(snapshot: dict) -> str:
    """Serialize within the configured prompt-size ceiling without losing schema."""
    compact = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)
    if len(compact) <= config.ASK_COACH_SNAPSHOT_MAX_CHARS:
        return compact
    trimmed = json.loads(json.dumps(snapshot))
    sections = (
        "recent_activities_14_days",
        "calendar_next_7_days",
        "planned_sessions",
    )
    for section in sections:
        wrapper = trimmed[section]
        while wrapper["items"] and len(
            json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
        ) > config.ASK_COACH_SNAPSHOT_MAX_CHARS:
            wrapper["items"].pop()
            wrapper["truncated"] = True
            wrapper["omitted_count"] += 1
    active = trimmed.get("active_program")
    if active:
        for item in reversed(active["sessions"]["items"]):
            while item["exercises"] and len(
                json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
            ) > config.ASK_COACH_SNAPSHOT_MAX_CHARS:
                item["exercises"].pop()
                active["sessions"]["truncated"] = True
                active["sessions"]["omitted_count"] += 1
    compact = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
    if len(compact) > config.ASK_COACH_SNAPSHOT_MAX_CHARS:
        raise ValueError("Advisory snapshot exceeds configured safe size")
    return compact
