"""Stage, revalidate, and atomically apply deterministic Telegram actions."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import re
from uuid import uuid4

import config
from sqlalchemy.orm import Session

from coach.decision_engine import DecisionResult, evaluate_morning_decision
from coach.onboarding import active_program
from db import (
    AthleteSafetyReport,
    ChatIntentAudit,
    DecisionRecord,
    MorningBriefState,
    PendingInteraction,
    PlannedSession,
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
    from coach.calendar import calendar_fingerprint
    external, state = calendar_fingerprint()
    return _hash({
        "coach": _state_value(session, "coach_calendar_events"),
        "external": external,
        "state": state,
    })


def _schedule_payload(session: Session, result: DecisionResult, action: dict) -> dict | None:
    session_id = int(action["program_session_id"])
    program_session = session.get(ProgramSession, session_id)
    if not program_session:
        raise ValueError("Program session no longer exists")
    from coach.calendar import get_upcoming_schedule_result
    from coach.scheduling import next_available_time
    target_day = date.fromisoformat(action["target_date"])
    calendar = get_upcoming_schedule_result(days=7)
    if calendar["state"] != "fresh":
        return None
    suggestion = next_available_time(
        session,
        now=get_local_now().replace(tzinfo=None),
        schedule=calendar["events"],
        start_day=target_day,
        max_days=1,
    )
    if not suggestion or suggestion.program_session_id != session_id:
        return None
    return {
        "action": "schedule_session",
        "program_session_id": session_id,
        "activity_type": program_session.sport_type or "strength_training",
        "title": program_session.name,
        "target_date": action["target_date"],
        "suggested_time": suggestion.start.strftime("%H:%M"),
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
    selected = [
        action for action in result.permitted_actions
        if action_types is None or action["type"] in action_types
    ]
    if any(
        row.active for row in session.query(AthleteSafetyReport).filter_by(active=True).all()
    ):
        selected = [action for action in selected if action["type"] != "schedule_original_session"]
    if not selected:
        return staged
    versions = (program_version(session), sync_version(session), calendar_version(session))
    for action in selected:
        action_type = action["type"]
        if action_type == "schedule_original_session":
            payload = _schedule_payload(session, result, action)
            if payload is None:
                continue
            target_type = "program_session"
            target_id = int(action["program_session_id"])
        elif action_type in {
            "keep_planned_session", "keep_calendar_time", "request_reschedule",
            "cancel_planned_session",
        }:
            payload = {
                "planned_session_id": action["planned_session_id"],
                "conflict": action.get("conflict"),
            }
            target_type = "planned_session"
            target_id = int(action["planned_session_id"])
        else:
            continue
        row = PendingInteraction(
            interaction_id=str(uuid4()),
            decision_id=result.decision_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            payload_json=json.dumps(payload, sort_keys=True),
            program_version=versions[0],
            sync_version=versions[1],
            calendar_version=versions[2],
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
        "schedule_original_session": "Approve and schedule",
        "confirm_safety_report": "Record report",
        "clear_safety_report": "Resume planning",
        "keep_planned_session": "Keep workout",
        "keep_calendar_time": "Keep workout",
        "request_reschedule": "Set another date",
        "reschedule_planned_time": "Confirm change",
        "cancel_planned_session": "Cancel workout",
        "start_sync": "Start sync",
    }[action_type]


def reply_markup(interactions: list[PendingInteraction]) -> dict | None:
    if not interactions:
        return None
    if len(interactions) == 1 and interactions[0].action_type == "schedule_original_session":
        item = interactions[0]
        return {
            "inline_keyboard": [[
                {"text": "Approve and schedule", "callback_data": f"decision_action_{item.interaction_id}"},
                {"text": "Set another date", "callback_data": f"decision_different_time_{item.interaction_id}"},
            ], [{"text": "Reject", "callback_data": f"decision_cancel_{item.interaction_id}"}]]
        }
    if len(interactions) == 1 and interactions[0].action_type == "cancel_planned_session":
        item = interactions[0]
        return {"inline_keyboard": [[
            {"text": "Keep workout", "callback_data": f"decision_cancel_{item.interaction_id}"},
            {"text": "Cancel workout", "callback_data": f"decision_action_{item.interaction_id}"},
        ]]}
    if len(interactions) == 1:
        item = interactions[0]
        dismiss = "Keep workout" if item.action_type == "reschedule_planned_time" else "Dismiss"
        return {"inline_keyboard": [[
            {"text": button_label(item.action_type), "callback_data": f"decision_action_{item.interaction_id}"},
            {"text": dismiss, "callback_data": f"decision_cancel_{item.interaction_id}"},
        ]]}
    action_types = {item.action_type for item in interactions}
    if {
        "request_reschedule", "cancel_planned_session",
    }.issubset(action_types) and action_types & {"keep_planned_session", "keep_calendar_time"}:
        ordered = []
        for action_type in (
            "keep_planned_session", "keep_calendar_time",
            "request_reschedule", "cancel_planned_session",
        ):
            item = next((row for row in interactions if row.action_type == action_type), None)
            if item:
                ordered.append({
                    "text": button_label(item.action_type),
                    "callback_data": f"decision_action_{item.interaction_id}",
                })
        return {"inline_keyboard": [ordered]}
    rows = []
    for item in interactions:
        if item.action_type == "schedule_original_session":
            rows.extend([
                [
                    {"text": "Approve and schedule", "callback_data": f"decision_action_{item.interaction_id}"},
                    {"text": "Set another date", "callback_data": f"decision_different_time_{item.interaction_id}"},
                ],
                [{"text": "Reject", "callback_data": f"decision_cancel_{item.interaction_id}"}],
            ])
        elif item.action_type == "cancel_planned_session":
            payload = json.loads(item.payload_json)
            title = payload.get("selection_label") or payload.get("title")
            rows.append([
                {"text": f"Keep {title}" if title else "Keep workout", "callback_data": f"decision_cancel_{item.interaction_id}"},
                {"text": f"Cancel {title}" if title else "Cancel workout", "callback_data": f"decision_action_{item.interaction_id}"},
            ])
        elif item.action_type == "request_reschedule":
            payload = json.loads(item.payload_json)
            title = payload.get("selection_label") or payload.get("title")
            rows.append([
                {"text": f"Change {title}" if title else "Set another date", "callback_data": f"decision_action_{item.interaction_id}"},
                {"text": "Dismiss", "callback_data": f"decision_cancel_{item.interaction_id}"},
            ])
        else:
            rows.append([
                {"text": button_label(item.action_type), "callback_data": f"decision_action_{item.interaction_id}"},
                {"text": "Dismiss", "callback_data": f"decision_cancel_{item.interaction_id}"},
            ])
    return {"inline_keyboard": rows}


def reply_markup_for_ids(session: Session, interaction_ids: list[str]) -> dict | None:
    rows = [session.get(PendingInteraction, item) for item in interaction_ids]
    return reply_markup([row for row in rows if row and row.status == "pending"])


def _stage_explicit_schedule(
    session: Session, user_text: str, now: datetime
) -> tuple[str, list[PendingInteraction]]:
    """Calculate and stage an explicit dated schedule request without using chat context."""
    from coach.calendar import get_upcoming_schedule_result
    from coach.scheduling import _parse_clock, next_available_time, requested_day

    target_day = requested_day(user_text, now.date())
    if target_day is None:
        return "State the target day, for example: today, tomorrow, or Monday.", []
    calendar = get_upcoming_schedule_result(days=7)
    if calendar["state"] == "unconfigured":
        return "Calendar is not connected, so I cannot verify a workout time.", []
    if calendar["state"] != "fresh":
        return "Calendar data is unavailable, so I cannot verify a workout time.", []
    clock_matches = re.findall(
        r"\b((?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)|(?:[01]?\d|2[0-3]):[0-5]\d)\b",
        user_text,
        re.IGNORECASE,
    )
    preferred = next((parsed for value in reversed(clock_matches) if (parsed := _parse_clock(value))), None)
    suggestion = next_available_time(
        session,
        now=now,
        schedule=calendar["events"],
        start_day=target_day,
        max_days=1,
        preferred_time=preferred,
    )
    if not suggestion:
        return f"No schedulable workout slot is available {target_day:%A}.", []
    versions = (program_version(session), sync_version(session), calendar_version(session))
    payload = {
        "action": "schedule_session",
        "program_session_id": suggestion.program_session_id,
        "activity_type": "strength_training",
        "title": suggestion.session_name,
        "target_date": suggestion.day.isoformat(),
        "suggested_time": suggestion.start.strftime("%H:%M"),
        "duration_min": suggestion.duration_min,
        "intensity": "normal",
        "modifications": [],
    }
    row = PendingInteraction(
        interaction_id=str(uuid4()), decision_id=None,
        action_type="schedule_original_session", target_type="program_session",
        target_id=suggestion.program_session_id,
        payload_json=json.dumps(payload, sort_keys=True),
        program_version=versions[0], sync_version=versions[1], calendar_version=versions[2],
        created_at=now, expires_at=now + timedelta(hours=1), status="pending",
    )
    session.add(row)
    session.flush()
    return (
        f"Please confirm: {suggestion.session_name} on {suggestion.day:%A} "
        f"at {suggestion.start:%H:%M}.",
        [row],
    )


def stage_free_text_change(session: Session, user_text: str) -> tuple[str, list[PendingInteraction]] | None:
    """Recognize a small, explicit change vocabulary; everything else stays informational."""
    lowered = " ".join(user_text.lower().split())
    now = get_local_now().replace(tzinfo=None)
    awaiting = (
        session.query(PendingInteraction)
        .filter_by(action_type="request_reschedule", status="awaiting_input")
        .order_by(PendingInteraction.created_at.desc())
        .first()
    )
    if awaiting:
        match = re.search(r"\b([01]\d|2[0-3]):([0-5]\d)\b", user_text)
        if not match:
            return "State an exact same-day time in HH:MM format.", []
        planned = session.get(PlannedSession, awaiting.target_id)
        if not planned or planned.status in {"completed", "cancelled"}:
            awaiting.status = "superseded"
            return "The planned session is no longer current.", []
        time_text = match.group(0)
        awaiting.status = "superseded"
        row = PendingInteraction(
            interaction_id=str(uuid4()), decision_id=None,
            action_type="reschedule_planned_time", target_type="planned_session",
            target_id=planned.id,
            payload_json=json.dumps({"planned_session_id": planned.id, "suggested_time": time_text}),
            program_version=program_version(session), sync_version=sync_version(session),
            calendar_version=calendar_version(session), created_at=now,
            expires_at=now + timedelta(hours=1), status="pending",
        )
        session.add(row)
        session.flush()
        from time_utils import format_chat_date
        return f"Confirm: move {planned.title} to {time_text} on {format_chat_date(planned.target_date)}.", [row]
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

    from coach.scheduling import is_schedule_request
    if is_schedule_request(user_text):
        return _stage_explicit_schedule(session, user_text, now)

    requested = None
    if any(phrase in lowered for phrase in ("schedule today", "schedule the workout", "schedule the session", "book the workout")):
        requested = "schedule_original_session"
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
    if requested == "schedule_original_session":
        match = re.search(r"\b([01]\d|2[0-3]):([0-5]\d)\b", user_text)
        if match and staged:
            payload = json.loads(staged[0].payload_json)
            payload["suggested_time"] = match.group(0)
            staged[0].payload_json = json.dumps(payload, sort_keys=True)
    return f"Confirm: {button_label(requested)}.", staged


def cancel_interaction(session: Session, interaction_id: str) -> bool:
    row = session.get(PendingInteraction, interaction_id)
    if not row or row.status != "pending":
        return False
    row.status = "rejected"
    row.failure_reason = "user_cancelled"
    return True


def _interaction_intent(action_type: str) -> str:
    return {
        "schedule_original_session": "schedule_workout",
        "reschedule_planned_time": "reschedule_workout",
        "request_reschedule": "reschedule_workout",
        "cancel_planned_session": "cancel_workout",
        "start_sync": "request_sync",
        "confirm_safety_report": "report_safety_issue",
        "clear_safety_report": "clear_safety_report",
    }.get(action_type, "unknown")


def reject_interaction(session: Session, interaction_id: str) -> str:
    """Reject a proposal without applying its underlying operation."""
    row = session.get(PendingInteraction, interaction_id)
    if not row or row.status != "pending":
        return "This choice is no longer available."
    row.status = "rejected"
    row.failure_reason = "user_rejected"
    if row.action_type == "schedule_original_session":
        text = "Proposal rejected. The workout remains pending and will not be proactively proposed again today."
    elif row.action_type in {"cancel_planned_session", "reschedule_planned_time"}:
        text = "Workout kept unchanged."
    elif row.action_type == "confirm_safety_report":
        text = "Safety report not recorded."
    elif row.action_type == "clear_safety_report":
        text = "Safety report remains active."
    else:
        text = "Action dismissed. Nothing was changed."
    session.add(ChatIntentAudit(
        message_text=f"button:{row.action_type}", provider="deterministic",
        model="closed-catalog-v2", router_mode="deterministic",
        intent=_interaction_intent(row.action_type),
        evidence_json=json.dumps({
            "input_method": "button", "interaction_id": interaction_id,
            "action_type": row.action_type, "starting_state": "confirm",
            "ending_state": None, "transition": "confirm->rejected",
            "final_outcome": "rejected", "failure_reason": row.failure_reason,
        }, sort_keys=True),
        validation_status="rejected", failure_reason=row.failure_reason,
        latency_ms=0, created_at=get_local_now().replace(tzinfo=None),
    ))
    return text


def mark_delivery_failed(session: Session, interaction_ids: list[str], reason: str) -> None:
    for interaction_id in interaction_ids:
        row = session.get(PendingInteraction, interaction_id)
        if row and row.status == "pending":
            row.status = "failed"
            row.failure_reason = f"delivery_failed:{reason}"[:1000]


def request_different_time(session: Session, interaction_id: str) -> str:
    """Turn a schedule proposal into typed date-then-time selection."""
    from db import ChatDialogueState
    row = session.get(PendingInteraction, interaction_id)
    now = get_local_now().replace(tzinfo=None)
    if not row or row.status != "pending" or row.action_type != "schedule_original_session":
        return "This proposal is no longer available."
    payload = json.loads(row.payload_json)
    row.status = "superseded"
    row.failure_reason = "different_time_requested"
    session.merge(ChatDialogueState(
        state_id=1,
        intent="schedule_workout",
        slots_json=json.dumps({
            "workout_text": payload.get("title"),
            "program_session_id": payload.get("program_session_id"),
            "duration_min": payload.get("duration_min") or 60,
            "selection_mode": True,
            "flow_nonce": uuid4().hex[:8],
            "step": "date",
        }, sort_keys=True),
        missing_slot="date",
        created_at=now,
        updated_at=now,
        expires_at=datetime(2099, 12, 31, 23, 59),
    ))
    session.flush()
    session.add(ChatIntentAudit(
        message_text="button:schedule_different_time", provider="deterministic",
        model="closed-catalog-v2", router_mode="deterministic",
        intent="schedule_workout",
        evidence_json=json.dumps({
            "input_method": "button", "interaction_id": interaction_id,
            "action_type": "schedule_different_time", "starting_state": "confirm",
            "ending_state": "date", "transition": "confirm->date",
            "final_outcome": "awaiting_input", "failure_reason": "",
        }, sort_keys=True),
        validation_status="awaiting_input", failure_reason="", latency_ms=0,
        created_at=now,
    ))
    return "Which new date should I use?"


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _scheduled_occurrence_id(raw, workout_id: int, target_day: date) -> int | None:
    """Find the Garmin scheduled-occurrence ID without trusting list ordering."""
    for item in _walk_dicts(raw):
        nested_workout = item.get("workout")
        nested_id = nested_workout.get("workoutId") if isinstance(nested_workout, dict) else None
        item_workout = item.get("workoutId") or nested_id
        item_date = item.get("date") or item.get("calendarDate") or item.get("workoutDate")
        scheduled_id = item.get("scheduledWorkoutId") or item.get("workoutScheduleId") or item.get("id")
        if str(item_workout) == str(workout_id) and (not item_date or str(item_date)[:10] == target_day.isoformat()):
            try:
                return int(scheduled_id)
            except (TypeError, ValueError):
                return None
    return None


def _apply_interaction(session: Session, interaction_id: str) -> tuple[str, str]:
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

    if row.action_type == "clear_safety_report":
        report = session.get(AthleteSafetyReport, row.target_id)
        if not report or not report.active:
            row.status = "superseded"
            row.failure_reason = "safety_report_not_active"
            return "stale", "The safety report is no longer active."
        report.active = False
        row.status = "applied"
        row.applied_at = now
        return "applied", "Safety report closed. Workout planning can resume."

    if row.action_type == "start_sync":
        is_auth = False
        if config.MULTI_USER_ENABLED:
            from sync.garmin_registry import get_garmin_registry
            from tenant_context import current_tenant
            tenant = current_tenant()
            if tenant:
                user_client = get_garmin_registry().get(tenant.user_id)
                is_auth = user_client.is_authenticated()
        else:
            from sync.garmin_client import client
            is_auth = client.is_authenticated()

        if not is_auth:
            row.status = "failed"
            row.failure_reason = "garmin_not_connected"
            return "failed", "Garmin is not connected. Please connect your Garmin account first."

        from sync import sync_runner
        started = sync_runner.try_start_sync(full=False, force=True)
        row.status = "applied" if started else "failed"
        row.applied_at = now if started else None
        row.failure_reason = "" if started else "sync_already_running"
        return ("applied", "Garmin sync started.") if started else ("failed", "A Garmin sync is already running.")

    if row.action_type == "cancel_planned_session":
        planned = session.get(PlannedSession, row.target_id)
        if not planned or planned.status != "approved":
            row.status = "superseded"
            row.failure_reason = "planned_session_changed"
            return "stale", "The scheduled workout is no longer current."
        if row.program_version != program_version(session) or row.calendar_version != calendar_version(session):
            row.status = "superseded"
            row.failure_reason = "program_or_calendar_changed"
            return "stale", "Program or calendar data changed. Ask again."
        if planned.garmin_workout_id:
            try:
                from sync.garmin_client import client
                client.login()
                scheduled = client.api.get_scheduled_workouts(planned.target_date.year, planned.target_date.month)
                occurrence_id = _scheduled_occurrence_id(
                    scheduled, planned.garmin_workout_id, planned.target_date,
                )
                if occurrence_id is None:
                    raise ValueError("Garmin scheduled occurrence could not be verified")
                client.api.unschedule_workout(occurrence_id)
            except Exception as exc:
                row.status = "failed"
                row.failure_reason = f"garmin_unschedule_failed:{type(exc).__name__}"
                return "failed", "Garmin could not verify the scheduled occurrence. Nothing was cancelled."
        planned.status = "cancelled"
        planned.updated_at = now
        events_row = session.get(SyncState, "coach_calendar_events")
        if events_row and events_row.value:
            try:
                events = json.loads(events_row.value)
            except ValueError:
                events = []
            removed = False
            kept = []
            for event in events:
                match = (
                    not removed
                    and event.get("date") == planned.target_date.isoformat()
                    and event.get("title") in {planned.title, f"\U0001f3cb\ufe0f {planned.title} @ {planned.suggested_time}"}
                )
                if match:
                    removed = True
                else:
                    kept.append(event)
            events_row.value = json.dumps(kept)
        row.status = "applied"
        row.applied_at = now
        return "applied", f"{planned.title} was cancelled."

    if row.action_type in {"keep_planned_session", "keep_calendar_time"}:
        planned = session.get(PlannedSession, row.target_id)
        if (
            not planned
            or planned.status in {"completed", "cancelled"}
            or row.program_version != program_version(session)
            or row.calendar_version != calendar_version(session)
        ):
            row.status = "superseded"
            row.failure_reason = "program_or_calendar_changed"
            return "stale", "Program or calendar data changed. Ask again."
        row.status = "applied"
        row.applied_at = now
        return "applied", "Workout kept unchanged."

    if row.action_type == "request_reschedule":
        planned = session.get(PlannedSession, row.target_id)
        if (
            not planned
            or planned.status in {"completed", "cancelled"}
            or row.program_version != program_version(session)
            or row.calendar_version != calendar_version(session)
        ):
            row.status = "superseded"
            row.failure_reason = "program_or_calendar_changed"
            return "stale", "Program or calendar data changed. Ask again."
        from db import ChatDialogueState
        row.status = "applied"
        row.applied_at = now
        session.merge(ChatDialogueState(
            state_id=1,
            intent="reschedule_workout",
            slots_json=json.dumps({
                "planned_session_id": planned.id,
                "duration_min": planned.duration_min,
                "selection_mode": True,
                "flow_nonce": uuid4().hex[:8],
                "step": "date",
            }, sort_keys=True),
            missing_slot="date",
            created_at=now,
            updated_at=now,
            expires_at=datetime(2099, 12, 31, 23, 59),
        ))
        return "awaiting_input", "Which new date should I use?"

    if row.action_type == "reschedule_planned_time":
        planned = session.get(PlannedSession, row.target_id)
        if not planned or planned.status in {"completed", "cancelled"}:
            row.status = "superseded"
            return "stale", "The planned session changed."
        if row.program_version != program_version(session) or row.calendar_version != calendar_version(session):
            row.status = "superseded"
            return "stale", "Program or calendar data changed. Ask again."
        payload = json.loads(row.payload_json)
        target_day = date.fromisoformat(payload.get("target_date") or planned.target_date.isoformat())
        from coach.calendar import find_calendar_conflict, get_upcoming_schedule_result
        days = max(2, (target_day - now.date()).days + 1)
        calendar = get_upcoming_schedule_result(days=days)
        if calendar["state"] == "error":
            row.status = "superseded"
            row.failure_reason = "calendar_access_error"
            return "stale", "Calendar could not be checked. No time change was made."
        conflict = find_calendar_conflict(
            calendar["events"], target_day, payload["suggested_time"], planned.duration_min
        )
        if conflict:
            row.status = "superseded"
            row.failure_reason = "calendar_conflict"
            return "stale", f"That time overlaps {conflict.get('title', 'another event')}. No change was made."
        if planned.garmin_workout_id and target_day != planned.target_date:
            try:
                from sync.garmin_client import client
                client.login()
                scheduled = client.api.get_scheduled_workouts(
                    planned.target_date.year, planned.target_date.month,
                )
                old_occurrence_id = _scheduled_occurrence_id(
                    scheduled, planned.garmin_workout_id, planned.target_date,
                )
                if old_occurrence_id is None:
                    raise ValueError("old Garmin occurrence could not be verified")
                client.api.schedule_workout(planned.garmin_workout_id, target_day.isoformat())
                try:
                    client.api.unschedule_workout(old_occurrence_id)
                except Exception:
                    # Compensate by removing the newly-created occurrence so a
                    # failed move does not leave two Garmin calendar entries.
                    newly_scheduled = client.api.get_scheduled_workouts(
                        target_day.year, target_day.month,
                    )
                    new_occurrence_id = _scheduled_occurrence_id(
                        newly_scheduled, planned.garmin_workout_id, target_day,
                    )
                    if new_occurrence_id is not None:
                        client.api.unschedule_workout(new_occurrence_id)
                    raise
            except Exception as exc:
                row.status = "failed"
                row.failure_reason = f"garmin_reschedule_failed:{type(exc).__name__}"
                return "failed", "Garmin could not safely move the workout. Nothing was changed locally."
        old_day = planned.target_date
        planned.target_date = target_day
        planned.suggested_time = payload["suggested_time"]
        planned.updated_at = now
        events_row = session.get(SyncState, "coach_calendar_events")
        if events_row and events_row.value:
            try:
                events = json.loads(events_row.value)
            except ValueError:
                events = []
            for event in events:
                if event.get("date") == old_day.isoformat() and event.get("title") == planned.title:
                    event["date"] = target_day.isoformat()
                    event["start_time"] = planned.suggested_time
            events_row.value = json.dumps(events)
        row.status = "applied"
        row.applied_at = now
        from notify.outbox import enqueue_pre_workout_reminder
        enqueue_pre_workout_reminder(session, planned)
        return "applied", f"{planned.title} moved to {target_day:%A} at {planned.suggested_time}."

    if row.action_type == "schedule_original_session" and row.decision_id is None:
        if (
            row.program_version != program_version(session)
            or row.calendar_version != calendar_version(session)
        ):
            row.status = "superseded"
            row.failure_reason = "program_or_calendar_changed"
            return "stale", "Program or calendar data changed. Ask again."
        payload = json.loads(row.payload_json)
        target_day = date.fromisoformat(payload["target_date"])
        from coach.calendar import get_upcoming_schedule_result
        from coach.scheduling import next_available_time

        calendar = get_upcoming_schedule_result(days=7)
        if calendar["state"] != "fresh":
            row.status = "superseded"
            row.failure_reason = "calendar_unavailable"
            return "stale", "Calendar data changed. Ask again."
        current_slot = next_available_time(
            session,
            now=now,
            schedule=calendar["events"],
            start_day=target_day,
            max_days=1,
            preferred_time=datetime.strptime(payload["suggested_time"], "%H:%M").time(),
        )
        expected = (
            int(payload["program_session_id"]),
            payload["target_date"],
            payload["suggested_time"],
        )
        actual = (
            current_slot.program_session_id,
            current_slot.day.isoformat(),
            current_slot.start.strftime("%H:%M"),
        ) if current_slot else None
        if actual != expected:
            row.status = "superseded"
            row.failure_reason = "schedule_slot_changed"
            return "stale", "The available workout time changed. Ask again."
        from coach.garmin_compiler import compile_and_schedule

        if not compile_and_schedule(session, payload):
            row.status = "failed"
            row.failure_reason = "garmin_schedule_failed"
            return "failed", "Garmin scheduling failed. No session was scheduled."
        planned = (
            session.query(PlannedSession)
            .filter_by(program_session_id=payload["program_session_id"], target_date=target_day)
            .order_by(PlannedSession.id.desc())
            .first()
        )
        if planned:
            from notify.outbox import enqueue_pre_workout_reminder
            enqueue_pre_workout_reminder(session, planned)
        row.status = "applied"
        row.applied_at = now
        return (
            "applied",
            f"{payload['title']} scheduled for {target_day:%A} at {payload['suggested_time']}.",
        )

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
        planned = (
            session.query(PlannedSession)
            .filter_by(
                program_session_id=payload.get("program_session_id"),
                target_date=date.fromisoformat(payload["target_date"]),
            )
            .order_by(PlannedSession.id.desc())
            .first()
        )
        if planned:
            from notify.outbox import enqueue_pre_workout_reminder
            enqueue_pre_workout_reminder(session, planned)

    row.status = "applied"
    row.applied_at = now
    return "applied", "Original program session scheduled."


def apply_interaction(session: Session, interaction_id: str) -> tuple[str, str]:
    """Apply and audit the final outcome of a confirmation button."""
    row = session.get(PendingInteraction, interaction_id)
    status, text = _apply_interaction(session, interaction_id)
    action_type = row.action_type if row else "unknown"
    intent = _interaction_intent(action_type)
    ending_state = "date" if status == "awaiting_input" else None
    session.add(ChatIntentAudit(
        message_text=f"button:{action_type}",
        provider="deterministic",
        model="closed-catalog-v2",
        router_mode="deterministic",
        intent=intent,
        evidence_json=json.dumps({
            "input_method": "button",
            "interaction_id": interaction_id,
            "action_type": action_type,
            "starting_state": "confirm",
            "ending_state": ending_state,
            "transition": f"confirm->{ending_state or status}",
            "final_outcome": status,
            "failure_reason": row.failure_reason if row else "interaction_missing",
        }, sort_keys=True),
        validation_status=status,
        failure_reason=row.failure_reason if row else "interaction_missing",
        latency_ms=0,
        created_at=get_local_now().replace(tzinfo=None),
    ))
    return status, text


def stage_calendar_conflict(session: Session, planned, conflict: dict) -> list[PendingInteraction]:
    now = get_local_now().replace(tzinfo=None)
    rows = []
    versions = (program_version(session), sync_version(session), calendar_version(session))
    for action_type in ("keep_calendar_time", "request_reschedule", "cancel_planned_session"):
        row = PendingInteraction(
            interaction_id=str(uuid4()), decision_id=None, action_type=action_type,
            target_type="planned_session", target_id=planned.id,
            payload_json=json.dumps({"conflict": conflict, "planned_session_id": planned.id}, sort_keys=True),
            program_version=versions[0], sync_version=versions[1],
            calendar_version=versions[2], created_at=now,
            expires_at=now + timedelta(hours=6), status="pending",
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows
