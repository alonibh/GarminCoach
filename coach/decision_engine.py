"""Deterministic coaching decisions; no language model or mutation lives here."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
from uuid import uuid4

from sqlalchemy.orm import Session

from coach.evidence import GARMIN_READINESS_CATEGORY_V1
from coach.onboarding import active_program
from coach.program_state import program_state_facts
from db import (
    DailyHealth,
    DecisionRecord,
    ObservationFreshness,
    PlannedSession,
    ProgramCursor,
    ProgramSession,
    Sleep,
)
from metrics.freshness import (
    ERROR, EXPECTED_PENDING, FRESH, HRV, HRV_STATUS, RECOVERY_TIME, RESTING_HR,
    SLEEP, SLEEP_SCORE, STALE, STRESS, TRAINING_READINESS, capability_state,
)
from time_utils import get_local_date, get_local_now


DECISION_TYPES = {
    "NO_SELECTED_WORKOUT",
    "WORKOUT_SELECTION_REQUIRED",
    "PROGRAM_REST_RECOMMENDED",
    "KEEP_SELECTED_WORKOUT",
    "KEEP_SELECTED_WORKOUT_WITH_WARNING",
    "REST_RECOMMENDED",
    "NO_BIOMETRIC_AUTHORITY",
}


@dataclass(frozen=True)
class Observation:
    signal: str
    value: object
    source: str
    observed_for: str
    fetched_at: str | None
    freshness: str


@dataclass
class DecisionResult:
    decision_id: str
    evaluated_at: str
    decision_type: str
    workout_outcome: str
    active_program_id: int | None
    active_program_name: str | None
    program_policy_version: str | None
    planned_session_id: int | None
    planned_session_name: str | None
    planned_start_time: str | None
    next_program_session_id: int | None
    next_program_session_name: str | None
    earliest_eligible_date: str | None
    readiness_score: int | None
    readiness_category: str | None
    observations: list[dict] = field(default_factory=list)
    missing_observations: list[dict] = field(default_factory=list)
    applied_rules: list[dict] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    permitted_actions: list[dict] = field(default_factory=list)
    optional_recovery_activity: dict | None = None
    calendar_conflict: dict | None = None
    decision_date: str | None = None
    best_effort: bool = False
    idempotency_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def training_readiness_category(score: int | None) -> str | None:
    if type(score) is not int or score < 1 or score > 100:
        return None
    if score <= 24:
        return "Poor"
    if score <= 49:
        return "Low"
    if score <= 74:
        return "Moderate"
    if score <= 94:
        return "High"
    return "Prime"


def sleep_score_category(score: float | None) -> str | None:
    if score is None or score < 0 or score > 100:
        return None
    if score < 60:
        return "Poor"
    if score <= 79:
        return "Fair"
    if score <= 89:
        return "Good"
    return "Excellent"


def selected_workouts_for_date(session: Session, target: date | None = None) -> list[PlannedSession]:
    """Return only normal, current local planned workouts for one decision date."""
    target = target or get_local_date()
    rows = session.query(PlannedSession).filter(
        PlannedSession.target_date == target,
        PlannedSession.status.notin_(("completed", "cancelled")),
    ).order_by(PlannedSession.suggested_time, PlannedSession.id).all()
    eligible = []
    for row in rows:
        linked = session.get(ProgramSession, row.program_session_id) if row.program_session_id else None
        if (row.activity_type or "").lower() == "rest" or (row.intensity or "").lower() == "recovery":
            continue
        if linked and linked.session_role == "optional_recovery":
            continue
        eligible.append(row)
    return eligible


def _freshness_row(session: Session, signal: str, target: date) -> ObservationFreshness | None:
    return session.get(ObservationFreshness, (signal, target))


def _observation(signal: str, value, row: ObservationFreshness | None, target: date, source: str) -> dict:
    return Observation(
        signal=signal,
        value=value,
        source=source,
        observed_for=target.isoformat(),
        fetched_at=row.fetched_at.isoformat() if row else None,
        freshness=row.state if row else "unknown",
    ).__dict__


def _canonical_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _legacy_evaluate_morning_decision(
    session: Session,
    *,
    allow_incomplete: bool = False,
    target: date | None = None,
    evaluated_at: datetime | None = None,
) -> DecisionResult:
    target = target or get_local_date()
    now = evaluated_at or get_local_now()
    program = active_program(session)
    state = program_state_facts(session, program, on_date=target) if program else None
    planned = _planned_today(session, target, program.id if program else None)

    if planned:
        outcome = "KEEP_PLANNED_SESSION"
    elif state and state["is_program_rest_day"]:
        outcome = "PROGRAM_REST_DAY"
    elif state:
        outcome = "PROPOSE_NEXT_SESSION"
    else:
        outcome = "NO_ACTION"

    observations: list[dict] = []
    missing: list[dict] = []
    rules: list[dict] = []
    reasons: list[str] = []
    actions: list[dict] = []
    calendar_conflict = None
    if planned and planned.suggested_time:
        from coach.calendar import find_calendar_conflict, get_upcoming_schedule_result
        calendar = get_upcoming_schedule_result(days=2)
        observations.append({
            "signal": "calendar",
            "value": calendar["state"],
            "source": "ICS calendar",
            "observed_for": target.isoformat(),
            "fetched_at": now.isoformat(),
            "freshness": calendar["state"],
        })
        if calendar["state"] == "error":
            missing.append({
                "signal": "calendar",
                "critical": False,
                "freshness": "error",
                "error_code": calendar["error"],
            })
            reasons.append("CALENDAR_ACCESS_ERROR")
        else:
            calendar_conflict = find_calendar_conflict(
                calendar["events"], target, planned.suggested_time, planned.duration_min
            )
    facts = morning_freshness(session, target)
    for signal in facts["missing_critical"]:
        row = _freshness_row(session, signal, target)
        missing.append({
            "signal": signal,
            "critical": True,
            "freshness": row.state if row else "unknown",
            "error_code": row.error_code if row else None,
        })
    for signal in facts["missing_noncritical"]:
        row = _freshness_row(session, signal, target)
        missing.append({
            "signal": signal,
            "critical": False,
            "freshness": row.state if row else "unknown",
            "error_code": row.error_code if row else None,
        })

    sleep = session.get(Sleep, target)
    sleep_row = _freshness_row(session, "sleep", target)
    score_row = _freshness_row(session, "sleep_score", target)
    if sleep and sleep.total_s:
        observations.append(_observation(
            "sleep_duration_hours", round(sleep.total_s / 3600.0, 1), sleep_row, target, "Garmin Sleep",
        ))
    if sleep and sleep.score is not None:
        observations.append(_observation(
            "sleep_score",
            {"score": int(round(sleep.score)), "category": sleep_score_category(sleep.score)},
            score_row, target, "Garmin Sleep Score",
        ))

    # Before the deadline the deterministic result waits. At/after 11:30 it
    # requests sync unless the athlete explicitly selected Answer anyway.
    if facts["missing_critical"] and not allow_incomplete:
        deadline_reached = (now.hour, now.minute) >= (11, 30)
        decision_type = "SYNC_REQUIRED" if deadline_reached else "WAITING_FOR_DATA"
        reasons.append("CRITICAL_OVERNIGHT_DATA_MISSING")
        actions = [{"type": "retry_priority_sync"}, {"type": "answer_anyway"}]
        best_effort = False
    else:
        decision_type = outcome
        best_effort = bool(facts["missing_critical"] and allow_incomplete)
        if best_effort:
            decision_type = "BEST_EFFORT"
            reasons.append("ATHLETE_REQUESTED_ANSWER_WITH_MISSING_DATA")

        readiness_score = None
        readiness_category = None
        readiness_row = _freshness_row(session, TRAINING_READINESS, target)
        health = session.get(DailyHealth, target)
        if (
            facts["capability"] == "supported"
            and readiness_row
            and readiness_row.state == FRESH
            and health
            and health.training_readiness is not None
        ):
            readiness_score = int(health.training_readiness)
            readiness_category = training_readiness_category(readiness_score)
            observations.append(_observation(
                TRAINING_READINESS,
                {"score": readiness_score, "category": readiness_category},
                readiness_row,
                target,
                "Garmin Training Readiness",
            ))
            rules.append(GARMIN_READINESS_CATEGORY_V1.to_dict())
            if outcome in {"KEEP_PLANNED_SESSION", "PROPOSE_NEXT_SESSION"}:
                if readiness_category == "Poor":
                    decision_type = "ADVISE_SKIP_SESSION"
                    reasons.append("GARMIN_READINESS_POOR")
                    actions = (
                        [
                            {"type": "keep_planned_session", "planned_session_id": planned.id},
                            {"type": "cancel_planned_session", "planned_session_id": planned.id},
                            {"type": "request_reschedule", "planned_session_id": planned.id},
                        ]
                        if planned else []
                    )
                elif readiness_category == "Low":
                    decision_type = "WARN_ORIGINAL_SESSION"
                    reasons.append("GARMIN_READINESS_LOW")
        else:
            readiness_score = None
            readiness_category = None
            if facts["capability"] in {"supported", "unknown"}:
                reasons.append("GARMIN_READINESS_UNAVAILABLE_NO_SUBSTITUTE")

        if outcome == "PROPOSE_NEXT_SESSION" and decision_type != "ADVISE_SKIP_SESSION":
            actions = [{
                "type": "schedule_original_session",
                "program_session_id": state["next_session_id"],
                "target_date": target.isoformat(),
            }]
        if outcome == "PROGRAM_REST_DAY":
            reasons.append("PROGRAM_SPACING_REQUIRES_REST")
            actions = []
        elif outcome == "KEEP_PLANNED_SESSION":
            reasons.append("PLANNED_SESSION_ALREADY_EXISTS")
        elif outcome == "PROPOSE_NEXT_SESSION":
            reasons.append("NEXT_PROGRAM_SESSION_ELIGIBLE")
        if calendar_conflict and decision_type != "ADVISE_SKIP_SESSION":
            reasons.append("CALENDAR_CONFLICT")
            actions = [
                {"type": "keep_calendar_time", "planned_session_id": planned.id, "conflict": calendar_conflict},
                {"type": "request_reschedule", "planned_session_id": planned.id, "conflict": calendar_conflict},
                {"type": "cancel_planned_session", "planned_session_id": planned.id, "conflict": calendar_conflict},
            ]
        elif calendar_conflict:
            reasons.append("CALENDAR_CONFLICT")

    # Values are assigned in the waiting branch too for a stable result shape.
    if "readiness_score" not in locals():
        readiness_score = None
        readiness_category = None

    identity_payload = {
        "day": target.isoformat(),
        "decision_type": decision_type,
        "outcome": outcome,
        "program_id": program.id if program else None,
        "planned_id": planned.id if planned else None,
        "state": state,
        "observations": [
            {"signal": item["signal"], "value": item["value"], "freshness": item["freshness"]}
            for item in observations
        ],
        "missing": [
            {"signal": item["signal"], "critical": item["critical"], "freshness": item["freshness"]}
            for item in missing
        ],
        "rules": [item["rule_id"] + ":" + item["version"] for item in rules],
        "actions": actions,
    }
    idempotency_key = f"morning:{target.isoformat()}:{_canonical_hash(identity_payload)}"
    existing = session.query(DecisionRecord).filter_by(idempotency_key=idempotency_key).first()
    if existing:
        return DecisionResult(**json.loads(existing.result_json))

    decision_id = str(uuid4())
    result = DecisionResult(
        decision_id=decision_id,
        evaluated_at=now.isoformat(),
        decision_type=decision_type,
        workout_outcome=outcome,
        active_program_id=program.id if program else None,
        active_program_name=program.name if program else None,
        program_policy_version=state["policy_version"] if state else None,
        planned_session_id=planned.id if planned else None,
        planned_session_name=planned.title if planned else None,
        planned_start_time=planned.suggested_time if planned else None,
        next_program_session_id=state["next_session_id"] if state else None,
        next_program_session_name=state["next_session_name"] if state else None,
        earliest_eligible_date=state["earliest_recommended_date"] if state else None,
        readiness_score=readiness_score,
        readiness_category=readiness_category,
        observations=observations,
        missing_observations=missing,
        applied_rules=rules,
        reason_codes=reasons,
        permitted_actions=actions,
        optional_recovery_activity=state["optional_recovery_activity"] if state else None,
        calendar_conflict=calendar_conflict,
        decision_date=target.isoformat(),
        best_effort=best_effort,
        idempotency_key=idempotency_key,
    )
    session.add(DecisionRecord(
        decision_id=decision_id,
        evaluated_at=now.replace(tzinfo=None),
        decision_type=decision_type,
        active_program_id=result.active_program_id,
        program_policy_version=result.program_policy_version,
        planned_session_id=result.planned_session_id,
        next_program_session_id=result.next_program_session_id,
        earliest_eligible_date=(
            date.fromisoformat(result.earliest_eligible_date) if result.earliest_eligible_date else None
        ),
        observations_json=json.dumps(observations, sort_keys=True),
        missing_json=json.dumps(missing, sort_keys=True),
        rule_ids_json=json.dumps([item["rule_id"] for item in rules]),
        reason_codes_json=json.dumps(reasons),
        permitted_actions_json=json.dumps(actions, sort_keys=True),
        result_json=json.dumps(result.to_dict(), sort_keys=True),
        idempotency_key=idempotency_key,
    ))
    session.flush()
    return result


def _current_fact(session: Session, signal: str, target: date, value, label: str) -> dict | None:
    row = session.get(ObservationFreshness, (signal, target))
    if not row or row.state != FRESH or value is None:
        return None
    return {
        "signal": signal, "label": label, "value": value, "source": "stored Garmin data",
        "observed_for": target.isoformat(), "fetched_at": row.fetched_at.isoformat(), "freshness": FRESH,
    }


def _informational_recovery_facts(session: Session, target: date) -> list[dict]:
    """Fresh stored facts only. These never influence the outcome."""
    sleep, health = session.get(Sleep, target), session.get(DailyHealth, target)
    facts: list[dict | None] = []
    if sleep:
        facts += [
            _current_fact(session, SLEEP, target, round(sleep.total_s / 3600, 1) if sleep.total_s else None, "Sleep duration (hours)"),
            _current_fact(session, SLEEP_SCORE, target, int(round(sleep.score)) if sleep.score is not None else None, "Garmin Sleep Score"),
        ]
    if health:
        facts += [
            _current_fact(session, HRV_STATUS, target, health.hrv_status, "Garmin HRV Status"),
            _current_fact(session, HRV, target, health.hrv_overnight, "Overnight HRV"),
            _current_fact(session, RECOVERY_TIME, target, health.recovery_time_minutes, "Recovery Time (minutes)"),
            _current_fact(session, RESTING_HR, target, health.resting_hr, "Resting HR"),
            _current_fact(session, STRESS, target, health.stress_avg, "Stress"),
        ]
    return [fact for fact in facts if fact]


def _recovery_record(session: Session, result: DecisionResult, identity: dict) -> DecisionResult:
    result.idempotency_key = f"selected-recovery:{result.decision_date}:{_canonical_hash(identity)}"
    existing = session.query(DecisionRecord).filter_by(idempotency_key=result.idempotency_key).first()
    if existing:
        return DecisionResult(**json.loads(existing.result_json))
    session.add(DecisionRecord(
        decision_id=result.decision_id, evaluated_at=datetime.fromisoformat(result.evaluated_at).replace(tzinfo=None),
        decision_type=result.decision_type, active_program_id=result.active_program_id,
        program_policy_version=result.program_policy_version, planned_session_id=result.planned_session_id,
        next_program_session_id=None, earliest_eligible_date=None,
        observations_json=json.dumps(result.observations, sort_keys=True),
        missing_json=json.dumps(result.missing_observations, sort_keys=True),
        rule_ids_json=json.dumps([item["rule_id"] for item in result.applied_rules]),
        reason_codes_json=json.dumps(result.reason_codes), permitted_actions_json="[]",
        result_json=json.dumps(result.to_dict(), sort_keys=True), idempotency_key=result.idempotency_key,
    ))
    session.flush()
    return result


def evaluate_selected_workout_recovery(
    session: Session, *, planned_session_id: int | None = None, target: date | None = None,
    evaluated_at: datetime | None = None,
) -> DecisionResult:
    """Make an advisory decision for one local selected workout, without any external calls or mutation."""
    target, now = target or get_local_date(), evaluated_at or get_local_now()
    program = active_program(session)
    eligible = selected_workouts_for_date(session, target)
    selected = next((row for row in eligible if row.id == planned_session_id), None) if planned_session_id is not None else (eligible[0] if len(eligible) == 1 else None)
    observations: list[dict] = []
    missing: list[dict] = []
    rules: list[dict] = []
    reasons: list[str] = []
    readiness_score = readiness_category = None
    policy_version = None

    if planned_session_id is not None and selected is None:
        outcome, reasons = "NO_SELECTED_WORKOUT", ["PLANNED_SESSION_NOT_ELIGIBLE_FOR_DECISION_DATE"]
    elif not eligible:
        outcome, reasons = "NO_SELECTED_WORKOUT", ["NO_ELIGIBLE_SELECTED_WORKOUT"]
    elif selected is None:
        outcome, reasons = "WORKOUT_SELECTION_REQUIRED", ["MULTIPLE_ELIGIBLE_SELECTED_WORKOUTS"]
        observations.append({
            "signal": "selected_workout_candidates", "freshness": "current", "source": "local planned sessions",
            "observed_for": target.isoformat(), "value": [
                {"planned_session_id": row.id, "name": row.title, "scheduled_time": row.suggested_time} for row in eligible
            ],
        })
    else:
        # Avoid creating a cursor during advisory recovery evaluation. Existing program policy remains authoritative.
        state = program_state_facts(session, program, on_date=target) if program and session.get(ProgramCursor, program.id) else None
        if state and state["is_program_rest_day"]:
            outcome, reasons = "PROGRAM_REST_RECOMMENDED", ["PROGRAM_SPACING_REQUIRES_REST"]
            policy_version = state["policy_version"]
            rules = [{"rule_id": "program_rest_policy", "version": policy_version,
                      "conclusion": "Program-required rest precedes biometric advice."}]
        else:
            capability = capability_state(session, TRAINING_READINESS)
            freshness_row = session.get(ObservationFreshness, (TRAINING_READINESS, target))
            health = session.get(DailyHealth, target)
            raw_score = health.training_readiness if health else None
            category = training_readiness_category(raw_score)
            if capability == "supported" and freshness_row and freshness_row.state == FRESH and category:
                readiness_score, readiness_category = raw_score, category
                observations.append(_current_fact(session, TRAINING_READINESS, target,
                    {"score": raw_score, "category": category}, "Garmin Training Readiness"))
                rules = [GARMIN_READINESS_CATEGORY_V1.to_dict()]
                if category == "Poor":
                    outcome, reasons = "REST_RECOMMENDED", ["GARMIN_READINESS_POOR"]
                elif category == "Low":
                    outcome, reasons = "KEEP_SELECTED_WORKOUT_WITH_WARNING", ["GARMIN_READINESS_LOW"]
                else:
                    outcome, reasons = "KEEP_SELECTED_WORKOUT", ["GARMIN_READINESS_KEEP"]
            else:
                outcome = "NO_BIOMETRIC_AUTHORITY"
                state_name = freshness_row.state if freshness_row else "missing"
                missing = [{"signal": TRAINING_READINESS, "freshness": state_name, "critical": False,
                            "error_code": freshness_row.error_code if freshness_row else None}]
                if capability == "unsupported":
                    reasons = ["TRAINING_READINESS_UNSUPPORTED_NO_SUBSTITUTE"]
                elif capability == "unknown":
                    reasons = ["TRAINING_READINESS_SUPPORT_UNVERIFIED"]
                else:
                    reasons = [{EXPECTED_PENDING: "TRAINING_READINESS_EXPECTED_PENDING", STALE: "TRAINING_READINESS_STALE",
                                ERROR: "TRAINING_READINESS_ERROR", "missing": "TRAINING_READINESS_MISSING"}.get(state_name, "TRAINING_READINESS_INVALID")]

    observations.extend(_informational_recovery_facts(session, target))
    result = DecisionResult(
        decision_id=str(uuid4()), evaluated_at=now.isoformat(), decision_type=outcome, workout_outcome=outcome,
        active_program_id=program.id if program else None, active_program_name=program.name if program else None,
        program_policy_version=policy_version, planned_session_id=selected.id if selected else None,
        planned_session_name=selected.title if selected else None, planned_start_time=selected.suggested_time if selected else None,
        next_program_session_id=None, next_program_session_name=None, earliest_eligible_date=None,
        readiness_score=readiness_score, readiness_category=readiness_category, observations=observations,
        missing_observations=missing, applied_rules=rules, reason_codes=reasons, permitted_actions=[],
        decision_date=target.isoformat(),
    )
    health = session.get(DailyHealth, target)
    readiness_row = session.get(ObservationFreshness, (TRAINING_READINESS, target))
    return _recovery_record(session, result, {
        "selected": result.planned_session_id, "selected_status": selected.status if selected else None,
        "target": target.isoformat(), "candidates": [row.id for row in eligible], "outcome": outcome,
        "capability": capability_state(session, TRAINING_READINESS),
        "freshness": readiness_row.state if readiness_row else "missing",
        "score": health.training_readiness if health else None,
        "policy": policy_version, "rules": [(rule["rule_id"], rule["version"]) for rule in rules],
    })


def evaluate_morning_decision(
    session: Session, *, allow_incomplete: bool = False, target: date | None = None,
    evaluated_at: datetime | None = None,
) -> DecisionResult:
    """Compatibility entry point; no recovery decision may propose a next program session."""
    return evaluate_selected_workout_recovery(session, target=target, evaluated_at=evaluated_at)
