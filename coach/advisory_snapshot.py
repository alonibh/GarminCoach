"""The compact, consent-versioned Ask Coach v3 local snapshot."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
from math import isfinite
import re
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

import config
from coach.advisory_aggregates import aggregate_dict, build_ask_coach_aggregate_context, normalize_activity_domain
from db import Activity, AthleteProfile, DailyHealth, DecisionRecord, ObservationFreshness, PlannedSession, ProgramCursor, ProgramSession, SessionExercise, Sleep, SyncState, TrainingProgram
from metrics.freshness import proactive_metrics_ready
from tenant_context import current_tenant

SNAPSHOT_VERSION = "ask-coach-v3"
PRIVACY_CONTRACT_VERSION = "ask-coach-aggregate-context-v1"
RECOVERY_METRICS = (
    "sleep_duration_hours", "sleep_score", "overnight_hrv", "garmin_hrv_status",
    "garmin_hrv_weekly_average", "local_hrv_7_night_coverage", "resting_heart_rate",
    "body_battery_current", "body_battery_high", "body_battery_charged",
    "body_battery_drained", "stress", "garmin_training_readiness", "recovery_time_minutes",
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_INACTIVE = ("cancelled", "completed", "replaced_by_active_recovery", "rest_selected")
_HARD_MAX_CHARS = 16_000


def _utc_iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(value: object, maximum: int = 96) -> str | None:
    if not isinstance(value, str):
        return None
    value = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    return value[:maximum].rstrip() or None


def _number(value: object, *, low: float | None = None, high: float | None = None, integer: bool = False) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        return None
    if integer:
        return int(value) if value.is_integer() else None
    return value


def _state_dt(session: Session, key: str) -> datetime | None:
    item = session.get(SyncState, key)
    if not item or not item.value:
        return None
    try:
        result = datetime.fromisoformat(item.value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _timezone() -> tuple[str, ZoneInfo]:
    tenant = current_tenant()
    name = tenant.timezone if tenant and tenant.timezone else "UTC"
    try:
        return name, ZoneInfo(name)
    except (KeyError, ValueError):
        return "UTC", ZoneInfo("UTC")


def _freshness_row(session: Session, signal: str, day: date) -> str | None:
    row = session.get(ObservationFreshness, (signal, day))
    state = row.state if row else None
    return state if state in {"fresh", "stale", "missing", "expected_pending", "error"} else None


def _fact(value: object, observed_at: datetime | None, *, capability: str = "not_applicable", freshness: str = "missing") -> dict:
    allowed_capability = {"supported", "unsupported", "unknown", "not_applicable"}
    allowed_freshness = {"fresh", "stale", "missing", "expected_pending", "error", "unknown"}
    capability = capability if capability in allowed_capability else "unknown"
    freshness = freshness if freshness in allowed_freshness else "unknown"
    if freshness not in {"fresh", "stale"}:
        value, observed_at = None, None
    return {"value": value, "observed_at": _utc_iso(observed_at), "capability": capability, "freshness": freshness}


def _official_recommendation(session: Session, local_day: date) -> tuple[dict, datetime | None]:
    record = session.query(DecisionRecord).order_by(DecisionRecord.evaluated_at.desc()).first()
    if record is None:
        return {"status": "unavailable", "decision_type": None, "selected_session_name": None, "training_readiness": None, "evaluated_at": None, "decision_day": None, "reason_labels": [], "missing_input_labels": ["official recommendation unavailable"]}, None
    try:
        result = json.loads(record.result_json or "{}")
    except (TypeError, ValueError):
        result = {}
    evaluated = record.evaluated_at
    decision_day = evaluated.date() if isinstance(evaluated, datetime) else None
    session_name = _clean(result.get("planned_session_name") or result.get("next_program_session_name"), 96)
    readiness = _number(result.get("readiness_score"), low=0, high=100)
    category = _clean(result.get("readiness_category"), 32)
    reasons = result.get("reason_codes") or []
    missing = result.get("missing_observations") or []
    def labels(values):
        return [_clean(item.get("signal") if isinstance(item, dict) else str(item), 64) for item in values][:8]
    return {
        "status": "current" if decision_day == local_day else "historical",
        "decision_type": _clean(result.get("decision_type") or record.decision_type, 48),
        "selected_session_name": session_name,
        "training_readiness": {"score": readiness, "category": category} if readiness is not None or category else None,
        "evaluated_at": _utc_iso(evaluated), "decision_day": decision_day.isoformat() if decision_day else None,
        "reason_labels": [item for item in labels(reasons) if item],
        "missing_input_labels": [item for item in labels(missing) if item],
    }, evaluated


def _data_freshness(session: Session, recommendation_at: datetime | None) -> dict:
    health = session.query(DailyHealth).order_by(DailyHealth.day.desc()).first()
    sleep = session.query(Sleep).order_by(Sleep.day.desc()).first()
    return {
        "last_sync_at": _utc_iso(_state_dt(session, "last_sync_at")),
        "device_last_upload_at": _utc_iso(_state_dt(session, "device_last_upload")),
        "latest_health_update": health.day.isoformat() if health else None,
        "latest_sleep_update": _utc_iso(sleep.sleep_end_time) if sleep and sleep.sleep_end_time else (sleep.day.isoformat() if sleep else None),
        "official_recommendation_evaluated_at": _utc_iso(recommendation_at),
    }


def _profile(session: Session, local_day: date) -> dict:
    profile = session.get(AthleteProfile, 1)
    birth = session.get(SyncState, "user_birth_date")
    age = None
    if birth and birth.value:
        try:
            born = date.fromisoformat(birth.value)
            age = local_day.year - born.year - ((local_day.month, local_day.day) < (born.month, born.day))
        except ValueError:
            pass
    weight = None
    raw_weight = session.get(SyncState, "user_weight")
    try:
        weight = _number(float(raw_weight.value), low=0) if raw_weight and raw_weight.value else None
    except (TypeError, ValueError):
        pass
    def local_json(value):
        try: data = json.loads(value or "[]")
        except (TypeError, ValueError): data = []
        return [_clean(item, 64) for item in data if _clean(item, 64)][:8] if isinstance(data, list) else None
    return {
        "age": age, "weight_kg": weight,
        "goals": [_clean(item, 128) for item in ((profile.primary_goal, profile.goal_detail) if profile else ()) if _clean(item, 128)],
        "experience": _clean(profile.experience_level, 48) if profile else None,
        "preferred_activities": local_json(profile.preferred_activities) if profile else [],
        "equipment": local_json(profile.equipment_access) if profile else [],
        "training_preferences": {
            "training_type": _clean(profile.training_type, 48) if profile else None,
            "availability": _clean(profile.availability, 128) if profile else None,
            "scheduling": _clean(profile.scheduling_preferences, 128) if profile else None,
        },
    }


def _current_recovery(session: Session, local_day: date, overnight_ready: bool) -> dict:
    sleep, health = session.get(Sleep, local_day), session.get(DailyHealth, local_day)
    sleep_at = sleep.sleep_end_time if sleep and sleep.sleep_end_time else (datetime.combine(local_day, time.min, tzinfo=timezone.utc) if sleep else None)
    health_at = datetime.combine(local_day, time.min, tzinfo=timezone.utc) if health else None
    def overnight(value, *, low=None, high=None, signal=None, capability="not_applicable", integer=False):
        state = _freshness_row(session, signal, local_day) if signal else None
        state = state or ("fresh" if overnight_ready and value is not None else ("expected_pending" if not overnight_ready else "missing"))
        return _fact(_number(value, low=low, high=high, integer=integer), health_at if health else sleep_at, capability=capability, freshness=state)
    def full_day(value, *, low=None, high=None, integer=False):
        state = "fresh" if value is not None else "missing"
        return _fact(_number(value, low=low, high=high, integer=integer), health_at, freshness=state)
    return {
        "sleep_duration_hours": overnight((sleep.total_s / 3600) if sleep and sleep.total_s is not None else None, low=0.01, high=24),
        "sleep_score": overnight(sleep.score if sleep else None, low=0, high=100),
        "overnight_hrv": overnight(health.hrv_overnight if health else None, low=0.01),
        "garmin_hrv_status": overnight(_clean(health.hrv_status, 64) if health else None, signal="hrv_status", capability="unknown"),
        "garmin_hrv_weekly_average": overnight(health.hrv_weekly_avg if health else None, low=0.01, signal="hrv_status", capability="unknown"),
        "local_hrv_7_night_coverage": overnight(health.hrv_7d_coverage_days if health else None, low=0, high=7, integer=True),
        "resting_heart_rate": overnight(health.resting_hr if health else None, low=0.01),
        "body_battery_current": full_day(health.body_battery_current if health else None, low=0, high=100),
        "body_battery_high": full_day(health.body_battery_high if health else None, low=0, high=100),
        "body_battery_charged": full_day(health.body_battery_charged if health else None, low=0, integer=True),
        "body_battery_drained": full_day(health.body_battery_drained if health else None, low=0, integer=True),
        "stress": full_day(health.stress_avg if health else None, low=0, high=100),
        "garmin_training_readiness": overnight(health.training_readiness if health else None, low=0, high=100, signal="training_readiness", capability="unknown", integer=True),
        "recovery_time_minutes": overnight(health.recovery_time_minutes if health else None, low=0, signal="recovery_time", capability="unknown", integer=True),
    }


def _recent_activity_facts(session: Session, local_day: date) -> dict:
    start = local_day - timedelta(days=6)
    rows = session.query(Activity).filter(Activity.start_time >= datetime.combine(start, time.min), Activity.start_time <= datetime.combine(local_day, time.max)).order_by(Activity.start_time.desc(), Activity.id.desc()).limit(6).all()
    items = []
    for row in rows[:5]:
        duration = _number(row.duration_s, low=0)
        distance = _number(row.distance_m, low=0)
        items.append({"local_day": row.start_time.date().isoformat(), "domain": normalize_activity_domain(row.activity_type), "duration_minutes": int(round(duration / 60)) if duration is not None else None, "distance_km": round(distance / 1000, 2) if distance is not None else None})
    return {"items": items, "truncated": len(rows) > 5, "omitted_count": max(0, len(rows) - 5)}


def _active_program(session: Session) -> dict | None:
    program = session.query(TrainingProgram).filter(TrainingProgram.active.is_(True)).order_by(TrainingProgram.id.desc()).first()
    if not program: return None
    cursor = session.get(ProgramCursor, program.id)
    next_session = session.get(ProgramSession, cursor.next_program_session_id) if cursor and cursor.next_program_session_id else None
    if next_session and next_session.program_id != program.id: next_session = None
    exercises = []
    if next_session:
        rows = session.query(SessionExercise).filter(SessionExercise.program_session_id == next_session.id).order_by(SessionExercise.order_index, SessionExercise.id).limit(21).all()
        exercises = [{"name": _clean(row.exercise_name, 64), "sets": _number(row.sets, low=0, integer=True), "reps": _number(row.reps, low=0, integer=True), "target_weight_kg": _number(row.weight_kg, low=0)} for row in rows[:20] if _clean(row.exercise_name, 64)]
        exercise_meta = {"items": exercises, "truncated": len(rows) > 20, "omitted_count": max(0, len(rows) - 20)}
    else:
        exercise_meta = {"items": [], "truncated": False, "omitted_count": 0}
    return {"name": _clean(program.name, 96), "mode": _clean(program.mode, 48), "weekly_sessions": _number(program.days_per_week, low=1, integer=True), "next_session": {"name": _clean(next_session.name, 96), "domain": normalize_activity_domain(next_session.sport_type), "duration_minutes": _number(next_session.duration_min, low=0, integer=True), "exercises": exercise_meta} if next_session else None}


def _planned_sessions(session: Session, local_day: date) -> dict:
    end = local_day + timedelta(days=6)
    rows = session.query(PlannedSession, ProgramSession).outerjoin(ProgramSession, PlannedSession.program_session_id == ProgramSession.id).filter(PlannedSession.target_date >= local_day, PlannedSession.target_date <= end, PlannedSession.status.notin_(_INACTIVE)).order_by(PlannedSession.target_date, PlannedSession.suggested_time, PlannedSession.id).limit(11).all()
    items = [{"local_day": planned.target_date.isoformat(), "session_name": _clean(program.name, 96) if program else None, "domain": normalize_activity_domain(program.sport_type if program else planned.activity_type), "duration_minutes": _number(planned.duration_min, low=0, integer=True), "status": _clean(planned.status, 32)} for planned, program in rows[:10]]
    return {"items": items, "truncated": len(rows) > 10, "omitted_count": max(0, len(rows) - 10)}


def build_advisory_snapshot(session: Session, *, generated_at: datetime | None = None) -> dict:
    """Build a deterministic local-only v3 dictionary; never repairs state."""
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc)
    timezone_name, local_timezone = _timezone()
    local_day = generated_at.astimezone(local_timezone).date()
    with session.no_autoflush:
        overnight_ready = proactive_metrics_ready(session, day=local_day)
        recommendation, recommendation_at = _official_recommendation(session, local_day)
        aggregate = aggregate_dict(build_ask_coach_aggregate_context(session, as_of_day=local_day, overnight_today_ready=overnight_ready))
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "privacy_contract_version": PRIVACY_CONTRACT_VERSION,
            "generated_at": _utc_iso(generated_at), "timezone": timezone_name,
            "date_context": {"local_day": local_day.isoformat(), "overnight_today_ready": overnight_ready, "training_windows": {"recent_7_days": {"start": (local_day - timedelta(days=6)).isoformat(), "end": local_day.isoformat()}, "prior_7_days": {"start": (local_day - timedelta(days=13)).isoformat(), "end": (local_day - timedelta(days=7)).isoformat()}, "recent_28_days": {"start": (local_day - timedelta(days=27)).isoformat(), "end": local_day.isoformat()}}},
            "official_recommendation": recommendation, "data_freshness": _data_freshness(session, recommendation_at),
            "profile": _profile(session, local_day), "current_recovery": _current_recovery(session, local_day, overnight_ready),
            "training_aggregates": {"recent_7_days": aggregate["recent_7_days"], "prior_7_days": aggregate["prior_7_days"], "recent_28_days": aggregate["recent_28_days"], "strength_highlights_14_days": aggregate["strength_highlights"]},
            "recovery_trends_28_days": aggregate["recovery_trends"], "slow_fitness_summary": aggregate["slow_fitness"],
            "recent_activity_facts_7_days": _recent_activity_facts(session, local_day), "active_program": _active_program(session),
            "planned_sessions_next_7_days": _planned_sessions(session, local_day),
        }


def serialize_advisory_snapshot(snapshot: dict) -> str:
    """Serialize under the non-overridable 16k privacy ceiling.

    Optional fields are removed in this order: recent activity facts, planned
    sessions, next-session exercises, then optional strength/recovery/fitness
    aggregates. Mandatory schema and decisive facts are never removed.
    """
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
        raise ValueError("invalid Ask Coach v3 snapshot")
    trimmed = json.loads(json.dumps(snapshot, ensure_ascii=False))
    def dump(): return json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
    def pop(wrapper):
        if wrapper.get("items"):
            wrapper["items"].pop(); wrapper["truncated"] = True; wrapper["omitted_count"] = int(wrapper.get("omitted_count", 0)) + 1; return True
        return False
    while len(dump()) > _HARD_MAX_CHARS and pop(trimmed["recent_activity_facts_7_days"]): pass
    while len(dump()) > _HARD_MAX_CHARS and pop(trimmed["planned_sessions_next_7_days"]): pass
    exercises = (trimmed.get("active_program") or {}).get("next_session", {}).get("exercises") if (trimmed.get("active_program") or {}).get("next_session") else None
    while len(dump()) > _HARD_MAX_CHARS and exercises and pop(exercises): pass
    for key in ("strength_highlights_14_days",):
        while len(dump()) > _HARD_MAX_CHARS and trimmed["training_aggregates"][key]: trimmed["training_aggregates"][key].pop()
    for key in ("recovery_trends_28_days", "slow_fitness_summary"):
        while len(dump()) > _HARD_MAX_CHARS and trimmed[key]: trimmed[key].pop()
    output = dump()
    if len(output) > _HARD_MAX_CHARS:
        raise ValueError("Ask Coach v3 mandatory snapshot exceeds 16000 characters")
    return output
