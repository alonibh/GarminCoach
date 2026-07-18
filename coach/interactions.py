"""Stage, revalidate, and atomically apply deterministic Telegram actions."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy.orm import Session

from coach.decision_engine import DecisionResult, evaluate_morning_decision
from coach.onboarding import active_program
from db import (
    AthleteSafetyReport,
    DecisionRecord,
    MorningBriefState,
    PendingInteraction,
    ProgramCursor,
    ProgramSession,
    SyncState,
)
from time_utils import get_local_now


def _hash(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def program_version(session: Session) -> str:
    program = active_program(session)
    if not program:
        return "none"
    cursor = session.get(ProgramCursor, program.id)
    sessions = (
        session.query(ProgramSession)
        .filter_by(program_id=program.id)
        .order_by(ProgramSession.sequence_order, ProgramSession.id)
        .all()
    )
    return _hash({
        "program": program.id,
        "updated_at": program.updated_at,
        "cursor": {
            "next": cursor.next_program_session_id if cursor else None,
            "last_activity": cursor.last_completed_activity_id if cursor else None,
            "policy": cursor.policy_version if cursor else None,
        },
        "sessions": [(item.id, item.sequence_order, item.name) for item in sessions],
    })


def _state_value(session: Session, key: str) -> str:
    row = session.get(SyncState, key)
    return row.value if row and row.value else ""


def sync_version(session: Session) -> str:
    return _state_value(session, "overnight_facts_updated_at") or _state_value(session, "last_sync_at")


def calendar_version(session: Session) -> str:
    return _hash(_state_value(session, "coach_calendar_events"))


def _schedule_payload(session: Session, result: DecisionResult, action: dict) -> dict:
    session_id = int(action["program_session_id"])
    program_session = session.get(ProgramSession, session_id)
    if not program_session:
        raise ValueError("Program session no longer exists")
    return {
        "action": "schedule_session",
        "program_session_id": session_id,
        "activity_type": program_session.sport_type or "strength_training",
        "title": program_session.name,
        "target_date": action["target_date"],
        "suggested_time": None,
        "duration_min": program_session.duration_min or 60,
        "intensity": "normal",
        "modifications": [],
    }


def stage_decision_actions(
    session: Session, result: DecisionResult, *, action_types: set[str] | None = None
) -> list[PendingInteraction]:
    now = get_local_now().replace(tzinfo=None)
    expiry = min(now + timedelta(hours=6), datetime.combine(now.date(), datetime.max.time()))
    staged: list[PendingInteraction] = []
    for action in result.permitted_actions:
        action_type = action["type"]
        if action_types is not None and action_type not in action_types:
            continue
        if action_type == "schedule_original_session":
            payload = _schedule_payload(session, result, action)
            target_type = "program_session"
            target_id = int(action["program_session_id"])
        elif action_type == "do_original_workout":
            if result.workout_outcome == "PROPOSE_NEXT_SESSION" and result.next_program_session_id:
                payload = _schedule_payload(session, result, {
                    "program_session_id": result.next_program_session_id,
                    "target_date": result.evaluated_at[:10],
                })
                target_type = "program_session"
                target_id = result.next_program_session_id
            else:
                payload = {"acknowledge": "do_original_workout"}
                target_type = "planned_session"
                target_id = result.planned_session_id
        elif action_type == "skip_today":
            payload = {"acknowledge": "skip_today"}
            target_type = "planned_session" if result.planned_session_id else "decision"
            target_id = result.planned_session_id
        else:
            continue
        row = PendingInteraction(
            interaction_id=str(uuid4()),
            decision_id=result.decision_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            payload_json=json.dumps(payload, sort_keys=True),
            program_version=program_version(session),
            sync_version=sync_version(session),
            calendar_version=calendar_version(session),
            created_at=now,
            expires_at=expiry,
            status="pending",
        )
        session.add(row)
        staged.append(row)
    session.flush()
    return staged


def button_label(action_type: str) -> str:
    return {
        "schedule_original_session": "Schedule session",
        "skip_today": "Skip today",
        "do_original_workout": "Do original workout",
        "confirm_safety_report": "Confirm report",
    }[action_type]


def reply_markup(interactions: list[PendingInteraction]) -> dict | None:
    if not interactions:
        return None
    return {
        "inline_keyboard": [[
            {"text": button_label(item.action_type), "callback_data": f"decision_action_{item.interaction_id}"}
            for item in interactions
        ], [{"text": "Cancel", "callback_data": f"decision_cancel_{interactions[0].interaction_id}"}]]
    }


def reply_markup_for_ids(session: Session, interaction_ids: list[str]) -> dict | None:
    rows = [session.get(PendingInteraction, item) for item in interaction_ids]
    return reply_markup([row for row in rows if row and row.status == "pending"])


def stage_free_text_change(session: Session, user_text: str) -> tuple[str, list[PendingInteraction]] | None:
    """Recognize a small, explicit change vocabulary; everything else stays informational."""
    lowered = " ".join(user_text.lower().split())
    now = get_local_now().replace(tzinfo=None)
    safety_terms = ("pain", "dizzy", "dizziness", "faint", "chest pain", "unusual difficulty")
    if any(term in lowered for term in safety_terms):
        report_type = "pain" if "pain" in lowered else "dizziness" if "dizz" in lowered or "faint" in lowered else "difficulty"
        row = PendingInteraction(
            interaction_id=str(uuid4()),
            decision_id=None,
            action_type="confirm_safety_report",
            target_type="safety_report",
            target_id=None,
            payload_json=json.dumps({"report_type": report_type, "report_text": user_text}, sort_keys=True),
            program_version=program_version(session),
            sync_version=sync_version(session),
            calendar_version=calendar_version(session),
            created_at=now,
            expires_at=now + timedelta(hours=1),
            status="pending",
        )
        session.add(row)
        session.flush()
        return f"Confirm this report: {user_text}", [row]

    requested = None
    if any(phrase in lowered for phrase in ("schedule today", "schedule the workout", "schedule the session", "book the workout")):
        requested = "schedule_original_session"
    elif any(phrase in lowered for phrase in ("skip today", "skip the workout")):
        requested = "skip_today"
    elif any(phrase in lowered for phrase in ("do the original", "original workout")):
        requested = "do_original_workout"
    elif any(word in lowered for word in ("reschedule", "move the workout", "change the time")):
        return "State the exact target date and time. No schedule change has been made.", []
    if not requested:
        return None

    today = get_local_now().date()
    morning_state = session.get(MorningBriefState, today)
    result = evaluate_morning_decision(
        session,
        allow_incomplete=bool(morning_state and morning_state.answer_anyway),
        target=today,
        evaluated_at=get_local_now(),
    )
    allowed = {item["type"] for item in result.permitted_actions}
    if requested not in allowed:
        return "That change is not permitted by the current decision. Ask for today's recommendation first.", []
    staged = stage_decision_actions(session, result, action_types={requested})
    return f"Confirm: {button_label(requested)}.", staged


def cancel_interaction(session: Session, interaction_id: str) -> bool:
    row = session.get(PendingInteraction, interaction_id)
    if not row or row.status != "pending":
        return False
    row.status = "rejected"
    row.failure_reason = "user_cancelled"
    return True


def apply_interaction(session: Session, interaction_id: str) -> tuple[str, str]:
    row = session.get(PendingInteraction, interaction_id)
    now = get_local_now().replace(tzinfo=None)
    if not row or row.status != "pending":
        return "stale", "This action is no longer available."
    if row.expires_at < now:
        row.status = "expired"
        row.failure_reason = "expired"
        return "stale", "This action expired. Ask again for a current proposal."
    if row.action_type == "confirm_safety_report":
        if row.program_version != program_version(session):
            row.status = "superseded"
            row.failure_reason = "program_changed"
            return "stale", "Program data changed. Restate the report if it is still relevant."
        payload = json.loads(row.payload_json)
        session.add(AthleteSafetyReport(
            report_type=payload["report_type"],
            report_text=payload["report_text"],
            confirmed_at=now,
            active=True,
        ))
        row.status = "applied"
        row.applied_at = now
        return "applied", "Safety report confirmed."

    record = session.get(DecisionRecord, row.decision_id) if row.decision_id else None
    if not record:
        row.status = "superseded"
        row.failure_reason = "decision_missing"
        return "stale", "The source decision is no longer available."
    if row.program_version != program_version(session) or row.calendar_version != calendar_version(session):
        row.status = "superseded"
        row.failure_reason = "program_or_calendar_changed"
        return "stale", "Program or calendar data changed. Ask for a fresh proposal."

    source = DecisionResult(**json.loads(record.result_json))
    target = date.fromisoformat(source.evaluated_at[:10])
    current = evaluate_morning_decision(
        session,
        allow_incomplete=source.best_effort,
        target=target,
        evaluated_at=get_local_now(),
    )
    currently_permitted = {item["type"] for item in current.permitted_actions}
    if row.action_type not in currently_permitted:
        row.status = "superseded"
        row.failure_reason = "action_no_longer_permitted"
        return "stale", "The underlying decision changed. Ask for a fresh proposal."

    payload = json.loads(row.payload_json)
    if payload.get("action") == "schedule_session":
        if payload.get("modifications"):
            row.status = "failed"
            row.failure_reason = "workout_modification_forbidden"
            return "failed", "Workout modifications are not permitted by this action."
        from coach.garmin_compiler import compile_and_schedule
        if not compile_and_schedule(session, payload):
            row.status = "failed"
            row.failure_reason = "garmin_schedule_failed"
            return "failed", "Garmin scheduling failed. The original program session was not changed."

    row.status = "applied"
    row.applied_at = now
    if row.action_type == "skip_today":
        message = "Skip choice confirmed."
    elif row.action_type == "do_original_workout":
        message = "Original workout confirmed without modifications."
    else:
        message = "Original program session scheduled."
    return "applied", message
