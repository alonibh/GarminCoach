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
    ProgramSession,
    Sleep,
)
from metrics.freshness import FRESH, TRAINING_READINESS, morning_freshness
from time_utils import get_local_date, get_local_now


DECISION_TYPES = {
    "WAITING_FOR_DATA",
    "SYNC_REQUIRED",
    "KEEP_PLANNED_SESSION",
    "PROPOSE_NEXT_SESSION",
    "PROGRAM_REST_DAY",
    "WARN_ORIGINAL_SESSION",
    "ADVISE_SKIP_SESSION",
    "BEST_EFFORT",
    "NO_ACTION",
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
    if score is None or score < 1 or score > 100:
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


def _planned_today(session: Session, target: date, program_id: int | None) -> PlannedSession | None:
    query = session.query(PlannedSession).filter(
        PlannedSession.target_date == target,
        PlannedSession.status.notin_(("completed", "cancelled")),
    )
    if program_id is not None:
        query = query.outerjoin(ProgramSession, PlannedSession.program_session_id == ProgramSession.id).filter(
            (ProgramSession.program_id == program_id) | (PlannedSession.program_session_id.is_(None))
        )
    return query.order_by(PlannedSession.suggested_time, PlannedSession.id).first()


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


def evaluate_morning_decision(
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
                    actions = [{"type": "skip_today"}, {"type": "do_original_workout"}]
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
            ]
        elif calendar_conflict:
            reasons.append("CALENDAR_CONFLICT")
            actions.append({
                "type": "request_reschedule",
                "planned_session_id": planned.id,
                "conflict": calendar_conflict,
            })

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
