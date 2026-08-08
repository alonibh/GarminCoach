"""Stage, revalidate, and atomically apply deterministic Telegram actions."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import dataclass
import hashlib
import json
import logging
from uuid import uuid4

import config
from garminconnect import GarminConnectAuthenticationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from coach.decision_engine import (
    RECOVERY_ACTION_POLICY_VERSION, DecisionResult, evaluate_morning_decision,
    evaluate_selected_workout_recovery,
)
from coach.onboarding import active_program
from coach.planned_session_status import INACTIVE_ORIGINAL_SESSION_STATUSES
from db import (
    DecisionRecord,
    PendingInteraction,
    PlannedSession,
    ProgramCursor,
    ProgramSession,
    SessionExercise,
    SyncState,
)
from time_utils import get_local_date, get_local_now

logger = logging.getLogger(__name__)


def _ensure_authenticated(garmin_client) -> None:
    ensure = getattr(garmin_client, "ensure_authenticated", None)
    if ensure is not None:
        ensure()
    else:
        garmin_client.login()


def _record_garmin_failure(
    row: PendingInteraction,
    *,
    operation: str,
    stage: str,
    exc: Exception,
    garmin_client=None,
) -> str | None:
    if isinstance(exc, GarminConnectAuthenticationError):
        marker = getattr(garmin_client, "mark_session_expired", None)
        if marker is not None:
            marker()
    row.status = "failed"
    row.failure_reason = (
        f"garmin_{operation}_failed:{stage}:{type(exc).__name__}"
    )
    logger.error(
        "garmin_mutation_failed operation=%s stage=%s exception_type=%s",
        operation,
        stage,
        type(exc).__name__,
    )
    if isinstance(exc, GarminConnectAuthenticationError):
        return "Garmin is no longer connected. Reconnect Garmin and try again."
    return None


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
    exercises = session.query(SessionExercise).join(ProgramSession).filter(
        ProgramSession.program_id == program.id
    ).order_by(SessionExercise.program_session_id, SessionExercise.order_index, SessionExercise.id).all()
    return _hash({
        "program": program.id,
        "updated_at": program.updated_at,
        "cursor": {
            "next": cursor.next_program_session_id if cursor else None,
            "last_activity": cursor.last_completed_activity_id if cursor else None,
            "policy": cursor.policy_version if cursor else None,
        },
        "sessions": [(item.id, item.sequence_order, item.name) for item in sessions],
        "execution": [
            (item.id, item.program_session_id, item.order_index, item.exercise_key,
             item.sets, item.reps, item.duration_seconds, item.weight_kg,
             item.rest_seconds,
             item.warmup_enabled, item.warmup_reps, item.warmup_duration_seconds,
             item.warmup_weight_kg)
            for item in exercises
        ],
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


def _schedule_payload(session: Session, result: DecisionResult, action: dict, *, persist: bool = True) -> dict | None:
    session_id = int(action["program_session_id"])
    program_session = session.get(ProgramSession, session_id)
    if not program_session:
        raise ValueError("Program session no longer exists")
    target_day = date.fromisoformat(action["target_date"])
    suggested_time = action.get("suggested_time")
    if not suggested_time:
        from coach.calendar import get_upcoming_schedule_result
        from coach.scheduling import next_available_time
        calendar = get_upcoming_schedule_result(days=7)
        if calendar["state"] != "fresh":
            return None
        suggestion = next_available_time(
            session,
            now=get_local_now().replace(tzinfo=None),
            schedule=calendar["events"],
            start_day=target_day,
            max_days=1,
            persist=persist,
        )
        if not suggestion or suggestion.program_session_id != session_id:
            return None
        suggested_time = suggestion.start.strftime("%H:%M")

    return {
        "action": "schedule_session",
        "program_session_id": session_id,
        "activity_type": program_session.sport_type or "strength_training",
        "title": program_session.name,
        "target_date": action["target_date"],
        "suggested_time": suggested_time,
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
    if action_types is None or "choose_recovery_outcome" in action_types:
        recovery = stage_recovery_choice(session, result)
        if recovery:
            return [recovery]
    selected = [
        action for action in result.permitted_actions
        if action_types is None or action["type"] in action_types
    ]
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


def _recovery_snapshot(planned: PlannedSession) -> dict:
    """The exact local state that a recovery callback is allowed to change."""
    return {
        "id": planned.id, "program_session_id": planned.program_session_id,
        "title": planned.title, "target_date": planned.target_date.isoformat(),
        "suggested_time": planned.suggested_time, "duration_min": planned.duration_min,
        "activity_type": planned.activity_type, "intensity": planned.intensity,
        "status": planned.status, "garmin_workout_id": planned.garmin_workout_id,
        "updated_at": planned.updated_at.isoformat() if planned.updated_at else None,
    }


def stage_recovery_choice(session: Session, result: DecisionResult) -> PendingInteraction | None:
    """Stage one local-only choice set; this deliberately never reads a private calendar."""
    action = next((item for item in result.permitted_actions if item.get("type") == "choose_recovery_outcome"), None)
    if not action or action.get("policy_version") != RECOVERY_ACTION_POLICY_VERSION:
        return None
    if result.decision_date != get_local_date().isoformat():
        return None
    planned = session.get(PlannedSession, action.get("planned_session_id"))
    if not planned:
        return None
    # An applied or processing choice is a durable athlete decision/lease for
    # this selected session/day.  Inspect all matching legacy rows before
    # considering a pending one so a historical duplicate cannot expose buttons.
    matching = []
    for old in session.query(PendingInteraction).filter_by(
        action_type="choose_recovery_outcome", target_id=planned.id
    ).all():
        try:
            old_payload = json.loads(old.payload_json)
        except (TypeError, ValueError):
            continue
        if old_payload.get("decision_date") != result.decision_date:
            if old.status == "processing":
                # An older lease may only be reclaimed by its chosen callback;
                # never overwrite it with a different decision's buttons.
                return old
            if old.status == "pending":
                old.status, old.failure_reason = "superseded", "source_decision_superseded"
            continue
        matching.append(old)
    if any(old.status == "applied" for old in matching):
        return None
    processing = next((old for old in matching if old.status == "processing"), None)
    if processing is not None:
        return processing
    pending_same = next((old for old in matching if old.status == "pending" and old.decision_id == result.decision_id), None)
    if pending_same is not None:
        return pending_same
    for old in matching:
        if old.status == "pending":
            old.status, old.failure_reason = "superseded", "source_decision_superseded"
    now = get_local_now().replace(tzinfo=None)
    expires = min(now + timedelta(hours=6), datetime.combine(get_local_date(), datetime.max.time()))
    payload = {
        "policy_version": RECOVERY_ACTION_POLICY_VERSION, "nonce": uuid4().hex[:8],
        "allowed_choices": list(action["choices"]), "recommended_choice": action.get("recommended_choice"),
        "selected_choice": None, "processing_started_at": None, "decision_date": result.decision_date,
        "planned_session": _recovery_snapshot(planned),
        "source_decision_idempotency_key": result.idempotency_key,
    }
    row = PendingInteraction(
        interaction_id=str(uuid4()), decision_id=result.decision_id,
        action_type="choose_recovery_outcome", target_type="planned_session", target_id=planned.id,
        payload_json=json.dumps(payload, sort_keys=True), program_version=program_version(session),
        sync_version=sync_version(session), calendar_version="", created_at=now,
        expires_at=expires, status="pending",
    )
    session.add(row)
    session.flush()
    return row


def prepare_recovery_morning(session: Session, result: DecisionResult, *, plan_only: bool = False) -> tuple[str | None, list[str]]:
    """Small orchestration boundary: rendering stays pure and staging stays local."""
    from coach.renderer import render_morning
    text, _markup, _ids = render_morning(session, result, plan_only=plan_only)
    if plan_only:
        return text, []
    if any(action.get("type") == "choose_recovery_outcome" for action in result.permitted_actions):
        row = stage_recovery_choice(session, result)
        return text, [row.interaction_id] if row else []
    staged = stage_decision_actions(session, result)
    return text, [row.interaction_id for row in staged]


def button_label(action_type: str) -> str:
    return {
        "schedule_original_session": "Approve and schedule",
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
    if len(interactions) == 1 and interactions[0].action_type == "choose_recovery_outcome":
        item = interactions[0]
        try:
            nonce = json.loads(item.payload_json)["nonce"]
        except (KeyError, TypeError, ValueError):
            return None
        return {"inline_keyboard": [[
            {"text": "Keep workout", "callback_data": f"rc:{item.interaction_id}:{nonce}:o"},
            {"text": "30-min walk", "callback_data": f"rc:{item.interaction_id}:{nonce}:w"},
            {"text": "Rest", "callback_data": f"rc:{item.interaction_id}:{nonce}:r"},
        ]]}
    if len(interactions) == 1 and interactions[0].action_type == "schedule_original_session":
        item = interactions[0]
        try:
            is_manual = json.loads(item.payload_json).get("flow_type") == "schedule"
        except (TypeError, ValueError):
            is_manual = False
        if is_manual:
            return {
                "inline_keyboard": [[
                    {"text": "Schedule", "callback_data": f"decision_action_{item.interaction_id}"},
                    {"text": "Change date or time", "callback_data": f"decision_different_time_{item.interaction_id}"},
                ], [{"text": "Cancel", "callback_data": f"decision_cancel_{item.interaction_id}"}]]
            }
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


def cancel_interaction(session: Session, interaction_id: str) -> bool:
    row = session.get(PendingInteraction, interaction_id)
    if not row or row.status != "pending":
        return False
    row.status = "rejected"
    row.failure_reason = "user_cancelled"
    return True


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
    else:
        text = "Action dismissed. Nothing was changed."
    return text


def mark_delivery_failed(session: Session, interaction_ids: list[str], reason: str) -> None:
    for interaction_id in interaction_ids:
        row = session.get(PendingInteraction, interaction_id)
        if row and row.status == "pending":
            row.status = "failed"
            row.failure_reason = f"delivery_failed:{reason}"[:1000]


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
        if str(item_workout) == str(workout_id) and isinstance(item_date, str) and item_date[:10] == target_day.isoformat():
            try:
                return int(scheduled_id)
            except (TypeError, ValueError):
                return None
    return None


def _scheduled_occurrence_ids(raw, workout_id: int, target_day: date) -> list[int]:
    found: list[int] = []
    for item in _walk_dicts(raw):
        nested = item.get("workout")
        nested_id = nested.get("workoutId") if isinstance(nested, dict) else None
        workout = item.get("workoutId") or nested_id
        day = item.get("date") or item.get("calendarDate") or item.get("workoutDate")
        occurrence = item.get("scheduledWorkoutId") or item.get("workoutScheduleId") or item.get("id")
        if str(workout) == str(workout_id) and isinstance(day, str) and day[:10] == target_day.isoformat():
            try:
                parsed = int(occurrence)
            except (TypeError, ValueError):
                continue
            if parsed not in found:
                found.append(parsed)
    return found


def _apply_interaction(session: Session, interaction_id: str) -> tuple[str, str]:
    row = session.get(PendingInteraction, interaction_id)
    now = get_local_now().replace(tzinfo=None)
    if not row or row.status != "processing":
        return "stale", "This action is no longer available."
    if row.expires_at < now:
        row.status = "expired"
        row.failure_reason = "expired"
        return "stale", "This action expired. Ask again for a current proposal."
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
            garmin_client = None
            stage = "authenticate"
            try:
                from sync.garmin_registry import current_garmin_client

                with current_garmin_client() as garmin_client:
                    _ensure_authenticated(garmin_client)
                    stage = "read_back"
                    scheduled = garmin_client.api.get_scheduled_workouts(
                        planned.target_date.year, planned.target_date.month
                    )
                    occurrence_id = _scheduled_occurrence_id(
                        scheduled, planned.garmin_workout_id, planned.target_date,
                    )
                    if occurrence_id is None:
                        raise ValueError("Garmin scheduled occurrence could not be verified")
                    stage = "schedule"
                    garmin_client.api.unschedule_workout(occurrence_id)
            except Exception as exc:
                auth_message = _record_garmin_failure(
                    row,
                    operation="cancel",
                    stage=stage,
                    exc=exc,
                    garmin_client=garmin_client,
                )
                return (
                    "failed",
                    auth_message
                    or "Garmin could not verify the scheduled occurrence. Nothing was cancelled.",
                )
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
        from coach.calendar import get_upcoming_schedule_result
        from coach.scheduling import available_start_times
        days = max(2, (target_day - now.date()).days + 1)
        calendar = get_upcoming_schedule_result(days=days)
        if calendar["state"] != "fresh":
            row.status = "superseded"
            row.failure_reason = "calendar_access_error"
            return "stale", "Calendar could not be checked. No time change was made."
        try:
            selected_time = datetime.strptime(payload["suggested_time"], "%H:%M").time()
        except (KeyError, TypeError, ValueError):
            row.status = "superseded"
            row.failure_reason = "schedule_slot_changed"
            return "stale", "That workout time is no longer available. Choose a new date and time."
        valid_starts = available_start_times(
            session,
            now=now,
            schedule=calendar["events"],
            target_day=target_day,
            duration_min=planned.duration_min or 60,
            limit=96,
        )
        if selected_time not in valid_starts:
            row.status = "superseded"
            row.failure_reason = "schedule_slot_changed"
            return "stale", "That workout time is no longer available. Choose a new date and time."
        if planned.garmin_workout_id and target_day != planned.target_date:
            garmin_client = None
            stage = "authenticate"
            try:
                from sync.garmin_registry import current_garmin_client

                with current_garmin_client() as garmin_client:
                    _ensure_authenticated(garmin_client)
                    stage = "read_back"
                    scheduled = garmin_client.api.get_scheduled_workouts(
                        planned.target_date.year, planned.target_date.month,
                    )
                    old_occurrence_id = _scheduled_occurrence_id(
                        scheduled, planned.garmin_workout_id, planned.target_date,
                    )
                    if old_occurrence_id is None:
                        raise ValueError("old Garmin occurrence could not be verified")
                    stage = "schedule"
                    garmin_client.api.schedule_workout(
                        planned.garmin_workout_id, target_day.isoformat()
                    )
                    try:
                        garmin_client.api.unschedule_workout(old_occurrence_id)
                    except Exception:
                        stage = "cleanup"
                        # Remove the new occurrence so a failed move cannot
                        # leave both the old and new Garmin calendar entries.
                        newly_scheduled = garmin_client.api.get_scheduled_workouts(
                            target_day.year, target_day.month,
                        )
                        new_occurrence_id = _scheduled_occurrence_id(
                            newly_scheduled, planned.garmin_workout_id, target_day,
                        )
                        if new_occurrence_id is not None:
                            garmin_client.api.unschedule_workout(new_occurrence_id)
                        raise
            except Exception as exc:
                auth_message = _record_garmin_failure(
                    row,
                    operation="reschedule",
                    stage=stage,
                    exc=exc,
                    garmin_client=garmin_client,
                )
                return (
                    "failed",
                    auth_message
                    or "Garmin could not safely move the workout. Nothing was changed locally.",
                )
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
        from coach.garmin_compiler import compile_and_schedule_for_interaction

        result = compile_and_schedule_for_interaction(session, payload)
        if not result.ok:
            row.status = "failed"
            row.failure_reason = (
                f"garmin_schedule_failed:{result.stage}:"
                f"{result.exception_type}"
            )
            return "failed", result.user_message
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
        from coach.garmin_compiler import compile_and_schedule_for_interaction
        result = compile_and_schedule_for_interaction(session, payload)
        if not result.ok:
            row.status = "failed"
            row.failure_reason = (
                f"garmin_schedule_failed:{result.stage}:"
                f"{result.exception_type}"
            )
            return "failed", result.user_message
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


@dataclass(frozen=True)
class GarminInteractionClaim:
    interaction_id: str
    action_type: str
    title: str
    claimed: bool

    @property
    def progress_text(self) -> str:
        verb = {
            "schedule_original_session": "Scheduling",
            "reschedule_planned_time": "Moving",
            "cancel_planned_session": "Cancelling",
        }[self.action_type]
        return f"{verb} {self.title}…"


@dataclass(frozen=True)
class RecoveryChoiceClaim:
    interaction_id: str
    selected_choice: str
    title: str
    claimed: bool

    @property
    def progress_text(self) -> str:
        return {
            "original": f"Keeping {self.title}…",
            "active_recovery": "Scheduling Active Recovery…",
            "rest": "Setting Rest…",
        }[self.selected_choice]


_RECOVERY_CODES = {"o": "original", "w": "active_recovery", "r": "rest"}
_RECOVERY_LEASE = timedelta(minutes=10)


def claim_recovery_choice(session: Session, callback_data: str) -> RecoveryChoiceClaim | None:
    """Claim a one-row recovery choice set before any remote mutation starts."""
    parts = callback_data.split(":")
    if len(parts) != 4 or parts[0] != "rc" or parts[3] not in _RECOVERY_CODES:
        return None
    row = session.get(PendingInteraction, parts[1])
    if not row or row.action_type != "choose_recovery_outcome":
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    choice, now = _RECOVERY_CODES[parts[3]], get_local_now().replace(tzinfo=None)
    title = (payload.get("planned_session") or {}).get("title") or "workout"
    if payload.get("nonce") != parts[2] or choice not in payload.get("allowed_choices", []):
        return None
    old = row.status
    if old == "processing":
        try:
            reclaimable = payload.get("selected_choice") == choice and datetime.fromisoformat(payload["processing_started_at"]) + _RECOVERY_LEASE <= now
        except (KeyError, TypeError, ValueError):
            reclaimable = False
        if not reclaimable:
            return RecoveryChoiceClaim(row.interaction_id, choice, title, False)
    elif old != "pending" or row.expires_at < now:
        if old == "pending" and row.expires_at < now:
            row.status, row.failure_reason = "expired", "expired"
            session.commit()
        return RecoveryChoiceClaim(row.interaction_id, choice, title, False)
    payload["selected_choice"], payload["processing_started_at"] = choice, now.isoformat()
    claimed = session.execute(update(PendingInteraction).where(
        PendingInteraction.interaction_id == row.interaction_id,
        PendingInteraction.status == old, PendingInteraction.payload_json == row.payload_json,
    ).values(status="processing", payload_json=json.dumps(payload, sort_keys=True)).execution_options(synchronize_session=False)).rowcount == 1
    if claimed:
        row.status = "processing"
        row.payload_json = json.dumps(payload, sort_keys=True)
        session.commit()  # durable claim before Garmin/local mutation work
    return RecoveryChoiceClaim(row.interaction_id, choice, title, claimed)


def _recovery_snapshot_changed(planned: PlannedSession, snapshot: dict) -> bool:
    return _recovery_snapshot(planned) != snapshot


def _recovery_fail(
    session: Session,
    row: PendingInteraction,
    reason: str,
    *,
    stale: bool = False,
    compensation_incomplete: bool = False,
    reconnect_required: bool = False,
    remote_state_restored: bool = False,
) -> tuple[str, str]:
    """Settle a claimed choice without making an unsupported promise to the user."""
    row = session.get(PendingInteraction, row.interaction_id) or row
    row.status, row.failure_reason = ("superseded" if stale else "failed"), reason
    session.commit()
    if stale:
        return "stale", "This choice is no longer current. Nothing was changed."
    if reconnect_required:
        if compensation_incomplete:
            return (
                "failed",
                "Garmin disconnected and Garmin Calendar could not be fully verified. "
                "Review Garmin Calendar and reconnect Garmin before choosing again.",
            )
        if remote_state_restored:
            return (
                "failed",
                "Garmin disconnected, but the original remote state was restored. "
                "Reconnect Garmin before trying again.",
            )
        return "failed", "Garmin is no longer connected. Reconnect Garmin and try again."
    if compensation_incomplete:
        return (
            "failed",
            "GarminCoach could not fully verify Garmin Calendar after this failure. "
            "Review Garmin Calendar before choosing again.",
        )
    return "failed", "GarminCoach could not apply this choice. The original workout remains unchanged."


def _occurrences(api, workout_id: int | None, target: date) -> list[int]:
    if not workout_id:
        return []
    return _scheduled_occurrence_ids(api.get_scheduled_workouts(target.year, target.month), workout_id, target)


def _replace_local_calendar_event(
    session: Session, planned: PlannedSession, replacement: PlannedSession | None = None,
) -> None:
    """Replace one source event with at most one canonical local recovery event."""
    state = session.get(SyncState, "coach_calendar_events")
    try:
        events = json.loads(state.value) if state and state.value else []
    except (TypeError, ValueError):
        events = []
    if not isinstance(events, list):
        events = []
    source_time = planned.suggested_time or "18:30"
    removed, kept = False, []
    for event in events:
        matches_source = (
            isinstance(event, dict)
            and event.get("date") == planned.target_date.isoformat()
            and event.get("title") == planned.title
            and event.get("start_time", "") == source_time
        )
        if matches_source and not removed:
            removed = True
            continue
        kept.append(event)
    if replacement is not None:
        replacement_event = {
            "title": replacement.title,
            "date": planned.target_date.isoformat(),
            "start_time": source_time,
            "duration_min": 30,
        }
        # Preserve unrelated entries and retain only one exact replacement.
        deduped = []
        seen_replacement = False
        for event in kept:
            if event == replacement_event:
                if seen_replacement:
                    continue
                seen_replacement = True
            deduped.append(event)
        if not seen_replacement:
            deduped.append(replacement_event)
        kept = deduped
    session.merge(SyncState(key="coach_calendar_events", value=json.dumps(kept, sort_keys=True)))


def _recovery_session(session: Session, source: PlannedSession, walk_id: int, now: datetime) -> PlannedSession:
    """Return the one exact active local row for the canonical walk."""
    from coach.active_recovery import ACTIVE_RECOVERY_WORKOUT_NAME

    expected = {
        "program_session_id": None, "activity_type": "walking",
        "title": ACTIVE_RECOVERY_WORKOUT_NAME, "target_date": source.target_date,
        "suggested_time": source.suggested_time or "18:30", "duration_min": 30,
        "intensity": "recovery", "garmin_workout_id": walk_id,
        "source": "recovery_choice",
    }
    active = {"approved", "scheduled", "planned"}
    candidates = session.query(PlannedSession).filter_by(
        target_date=source.target_date, source="recovery_choice",
    ).filter(PlannedSession.status.in_(active)).all()
    exact = [item for item in candidates if all(getattr(item, key) == value for key, value in expected.items())]
    if len(exact) == 1:
        return exact[0]
    if candidates:
        raise ValueError("recovery_session_conflict")
    recovery = PlannedSession(status="approved", created_at=now, updated_at=now, **expected)
    session.add(recovery)
    session.flush()
    return recovery


def _compensate_remote_recovery(api, *, original_id: int | None, walk_id: int | None,
                                 target: date, before_originals: list[int],
                                 before_walks: list[int], schedule_attempted: bool,
                                 garmin_client=None) -> bool:
    """Restore the proven pre-operation occurrence sets, never deleting templates."""
    try:
        current_originals = _occurrences(api, original_id, target)
        current_walks = _occurrences(api, walk_id, target)
        # A schedule attempt whose new occurrence is no longer observable is
        # unresolved, but must not prevent safe restoration of an original
        # occurrence that this invocation may already have removed.
        unresolved_walk = (
            schedule_attempted
            and not set(current_walks) - set(before_walks)
            and not before_walks
        )
        for occurrence in set(current_walks) - set(before_walks):
            api.unschedule_workout(occurrence)
        if before_originals and not current_originals and original_id:
            api.schedule_workout(original_id, target.isoformat())
        final_originals = _occurrences(api, original_id, target)
        final_walks = _occurrences(api, walk_id, target)
        # Garmin gives a newly scheduled restoration a new occurrence ID.  The
        # original contract is cardinality on the exact day; walks, which may
        # predate this invocation, retain their exact proven IDs.
        return (
            not unresolved_walk
            and len(final_originals) == len(before_originals)
            and set(final_walks) == set(before_walks)
        )
    except Exception as exc:
        if isinstance(exc, GarminConnectAuthenticationError):
            marker = getattr(garmin_client, "mark_session_expired", None)
            if callable(marker):
                marker()
        return False


def apply_claimed_recovery_choice(session: Session, interaction_id: str) -> tuple[str, str]:
    """Revalidate then apply original/walk/rest with Garmin compensation on commit failure."""
    row = session.get(PendingInteraction, interaction_id)
    if not row or row.action_type != "choose_recovery_outcome" or row.status != "processing":
        return "stale", "This choice is no longer available."
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return _recovery_fail(session, row, "invalid_payload")
    planned, source = session.get(PlannedSession, row.target_id), session.get(DecisionRecord, row.decision_id)
    if (not planned or not source or row.expires_at < get_local_now().replace(tzinfo=None)
            or payload.get("decision_date") != get_local_date().isoformat()
            or payload.get("policy_version") != RECOVERY_ACTION_POLICY_VERSION
            or row.program_version != program_version(session) or row.sync_version != sync_version(session)
            or _recovery_snapshot_changed(planned, payload.get("planned_session") or {})
            or planned.status in INACTIVE_ORIGINAL_SESSION_STATUSES
            or (planned.activity_type or "").lower() == "rest" or (planned.intensity or "").lower() == "recovery"):
        return _recovery_fail(session, row, "stale_state", stale=True)
    linked = session.get(ProgramSession, planned.program_session_id) if planned.program_session_id else None
    if linked and linked.session_role == "optional_recovery":
        return _recovery_fail(session, row, "optional_recovery", stale=True)
    current = evaluate_selected_workout_recovery(session, planned_session_id=planned.id, target=planned.target_date, evaluated_at=get_local_now().replace(tzinfo=None))
    action = next((item for item in current.permitted_actions if item.get("type") == "choose_recovery_outcome"), None)
    choice = payload.get("selected_choice")
    if not action or choice not in action.get("choices", []) or current.idempotency_key != payload.get("source_decision_idempotency_key"):
        return _recovery_fail(session, row, "decision_changed", stale=True)
    if session.query(PendingInteraction).filter(PendingInteraction.action_type == "choose_recovery_outcome", PendingInteraction.target_id == planned.id, PendingInteraction.status == "applied", PendingInteraction.interaction_id != row.interaction_id).first():
        return _recovery_fail(session, row, "already_applied", stale=True)
    now = get_local_now().replace(tzinfo=None)
    if choice == "original":
        if planned.garmin_workout_id:
            client = None
            try:
                from sync.garmin_registry import current_garmin_client
                with current_garmin_client() as client:
                    _ensure_authenticated(client)
                    if len(_occurrences(client.api, planned.garmin_workout_id, planned.target_date)) != 1:
                        return _recovery_fail(session, row, "original_occurrence_missing")
            except Exception as exc:
                if isinstance(exc, GarminConnectAuthenticationError):
                    marker = getattr(client, "mark_session_expired", None)
                    if callable(marker):
                        marker()
                    return _recovery_fail(session, row, "garmin_read:GarminConnectAuthenticationError", reconnect_required=True)
                return _recovery_fail(session, row, f"garmin_read:{type(exc).__name__}")
        row.status, row.applied_at, row.failure_reason = "applied", now, ""
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            return _recovery_fail(session, row, f"local_persistence:{type(exc).__name__}")
        return "applied", f"{planned.title} kept for today."
    if choice == "rest" and not planned.garmin_workout_id:
        try:
            planned.status, planned.updated_at = "rest_selected", now
            _replace_local_calendar_event(session, planned)
            row.status, row.applied_at, row.failure_reason = "applied", now, ""
            session.commit()
        except Exception as exc:
            session.rollback()
            return _recovery_fail(session, row, f"local_persistence:{type(exc).__name__}")
        return "applied", "Rest selected for today."

    # All remote work is followed by exactly one verified success path or one
    # compensation path.  No operation below returns directly after mutation.
    walk_id: int | None = None
    before_originals: list[int] = []
    before_walks: list[int] = []
    schedule_attempted = False
    original_unschedule_attempted = False
    remote_failure: tuple[str, Exception | None] | None = None
    client = None
    try:
        if choice == "active_recovery":
            from coach.active_recovery import ActiveRecoveryFailureKind, ensure_active_recovery_workout
            template = ensure_active_recovery_workout(session)
            if not template.ok or not template.workout_id:
                return _recovery_fail(
                    session, row, "template_unavailable",
                    reconnect_required=template.failure == ActiveRecoveryFailureKind.RECONNECT_REQUIRED,
                )
            walk_id = template.workout_id
        from sync.garmin_registry import current_garmin_client
        with current_garmin_client() as client:
            _ensure_authenticated(client)
            before_originals = _occurrences(client.api, planned.garmin_workout_id, planned.target_date)
            if len(before_originals) > 1:
                remote_failure = ("ambiguous_original_occurrence", None)
            # Establish every cardinality precondition before the first
            # schedule/unschedule call.  Ambiguous originals deliberately do
            # not even inspect a walk unless future diagnostics require it.
            if remote_failure is None and walk_id:
                before_walks = _occurrences(client.api, walk_id, planned.target_date)
                if len(before_walks) > 1:
                    remote_failure = ("ambiguous_walk_occurrence", None)
            if remote_failure is None and walk_id:
                if not before_walks:
                    schedule_attempted = True
                    client.api.schedule_workout(walk_id, planned.target_date.isoformat())
                    if len(_occurrences(client.api, walk_id, planned.target_date)) != 1:
                        remote_failure = ("walk_schedule_verification_failed", None)
            if remote_failure is None and before_originals:
                original_unschedule_attempted = True
                client.api.unschedule_workout(before_originals[0])
                if _occurrences(client.api, planned.garmin_workout_id, planned.target_date):
                    remote_failure = ("original_unschedule_verification_failed", None)
            if remote_failure is None:
                final_originals = _occurrences(client.api, planned.garmin_workout_id, planned.target_date)
                final_walks = _occurrences(client.api, walk_id, planned.target_date)
                expected_walk_count = 1 if walk_id else 0
                if final_originals or len(final_walks) != expected_walk_count:
                    remote_failure = ("final_remote_verification_failed", None)
    except Exception as exc:
        remote_failure = (f"garmin_mutation:{type(exc).__name__}", exc)
    if remote_failure is not None:
        reason, exc = remote_failure
        if isinstance(exc, GarminConnectAuthenticationError):
            marker = getattr(client, "mark_session_expired", None)
            if callable(marker):
                marker()
            if not (schedule_attempted or original_unschedule_attempted):
                return _recovery_fail(session, row, reason, reconnect_required=True)
        if not (schedule_attempted or original_unschedule_attempted):
            # A rejected precondition/read-only failure has no remote state to
            # restore.  In particular, ambiguity must never touch existing
            # walk occurrences during a speculative compensation pass.
            return _recovery_fail(session, row, reason)
        compensated = False
        cleanup_auth_failed = False
        if client is not None:
            try:
                from sync.garmin_registry import current_garmin_client
                with current_garmin_client() as cleanup_client:
                    compensated = _compensate_remote_recovery(
                        cleanup_client.api, original_id=planned.garmin_workout_id, walk_id=walk_id,
                        target=planned.target_date, before_originals=before_originals,
                        before_walks=before_walks, schedule_attempted=schedule_attempted,
                        garmin_client=cleanup_client,
                    )
                    cleanup_auth_failed = bool(getattr(cleanup_client, "expired", False))
            except Exception as cleanup_exc:
                if isinstance(cleanup_exc, GarminConnectAuthenticationError):
                    marker = getattr(client, "mark_session_expired", None)
                    if callable(marker):
                        marker()
        return _recovery_fail(
            session, row, reason,
            compensation_incomplete=not compensated,
            reconnect_required=isinstance(exc, GarminConnectAuthenticationError) or cleanup_auth_failed,
            remote_state_restored=(isinstance(exc, GarminConnectAuthenticationError) or cleanup_auth_failed) and compensated,
        )
    try:
        if choice == "active_recovery":
            recovery = _recovery_session(session, planned, walk_id, now)
            planned.status = "replaced_by_active_recovery"
            _replace_local_calendar_event(session, planned, recovery)
            from notify.outbox import enqueue_pre_workout_reminder
            if datetime.combine(recovery.target_date, datetime.strptime(recovery.suggested_time or "18:30", "%H:%M").time()) > now:
                enqueue_pre_workout_reminder(session, recovery)
        else:
            planned.status = "rest_selected"
            _replace_local_calendar_event(session, planned)
        planned.updated_at = now
        row.status, row.applied_at, row.failure_reason = "applied", now, ""
        session.commit()
    except Exception as exc:
        session.rollback()
        compensated = False
        cleanup_auth_failed = False
        try:
            from sync.garmin_registry import current_garmin_client
            with current_garmin_client() as cleanup_client:
                compensated = _compensate_remote_recovery(
                    cleanup_client.api, original_id=planned.garmin_workout_id, walk_id=walk_id,
                    target=planned.target_date, before_originals=before_originals,
                    before_walks=before_walks, schedule_attempted=schedule_attempted,
                    garmin_client=cleanup_client,
                )
                cleanup_auth_failed = bool(getattr(cleanup_client, "expired", False))
        except Exception as cleanup_exc:
            if isinstance(cleanup_exc, GarminConnectAuthenticationError):
                marker = getattr(client, "mark_session_expired", None)
                if callable(marker):
                    marker()
        return _recovery_fail(
            session, row, f"local_persistence:{type(exc).__name__}",
            compensation_incomplete=not compensated,
            reconnect_required=cleanup_auth_failed,
            remote_state_restored=cleanup_auth_failed and compensated,
        )
    return ("applied", "Active Recovery — 30 Minute Walk scheduled for today.") if choice == "active_recovery" else ("applied", "Rest selected for today.")


def claim_garmin_interaction(
    session: Session, interaction_id: str
) -> GarminInteractionClaim | None:
    """Durably claim one user-confirmed Garmin mutation before dispatch."""
    row = session.get(PendingInteraction, interaction_id)
    if row is None or row.action_type not in {
        "schedule_original_session",
        "reschedule_planned_time",
        "cancel_planned_session",
    }:
        return None
    payload = json.loads(row.payload_json)
    planned = (
        session.get(PlannedSession, row.target_id)
        if row.action_type in {"reschedule_planned_time", "cancel_planned_session"}
        else None
    )
    title = (
        planned.title
        if planned is not None
        else payload.get("title") or "workout"
    )
    if row.status != "pending":
        return GarminInteractionClaim(
            interaction_id=interaction_id,
            action_type=row.action_type,
            title=title,
            claimed=False,
        )
    claimed = (
        session.execute(
            update(PendingInteraction)
            .where(
                PendingInteraction.interaction_id == interaction_id,
                PendingInteraction.status == "pending",
            )
            .values(status="processing")
            .execution_options(synchronize_session=False)
        ).rowcount
        == 1
    )
    if claimed:
        row.status = "processing"
    return GarminInteractionClaim(
        interaction_id=interaction_id,
        action_type=row.action_type,
        title=title,
        claimed=claimed,
    )


def apply_claimed_interaction(
    session: Session, interaction_id: str
) -> tuple[str, str]:
    row = session.get(PendingInteraction, interaction_id)
    if row is None or row.status != "processing":
        return "stale", "This action is no longer available."
    return _apply_interaction(session, interaction_id)


def apply_interaction(session: Session, interaction_id: str) -> tuple[str, str]:
    """Claim and apply an interaction for non-webhook compatibility callers."""
    row = session.get(PendingInteraction, interaction_id)
    if row is None or row.status != "pending":
        return "stale", "This action is no longer available."
    row.status = "processing"
    session.flush()
    return _apply_interaction(session, interaction_id)


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


@dataclass(frozen=True)
class FlowTurn:
    text: str
    reply_markup: dict | None


def _flow_markup(
    row: PendingInteraction, labels: list[str], kind: str
) -> dict:
    payload = json.loads(row.payload_json)
    nonce = payload["nonce"]
    buttons = [
        {
            "text": label,
            "callback_data": (
                f"flow:{row.interaction_id}:{nonce}:{kind}:{index}"
            ),
        }
        for index, label in enumerate(labels)
    ]
    return {
        "inline_keyboard": [
            buttons[index : index + 2]
            for index in range(0, len(buttons), 2)
        ]
        + [[
            {
                "text": "Cancel",
                "callback_data": (
                    f"flow:{row.interaction_id}:{nonce}:cancel:0"
                ),
            }
        ]]
    }


def _new_flow(
    session: Session,
    *,
    flow_type: str,
    payload: dict,
    target_type: str,
    target_id: int | None,
) -> PendingInteraction:
    now = get_local_now().replace(tzinfo=None)
    payload = {
        "flow_type": flow_type,
        "flow_step": payload["flow_step"],
        **payload,
        "nonce": uuid4().hex[:8],
        "page": 0,
    }
    row = PendingInteraction(
        interaction_id=str(uuid4()),
        decision_id=None,
        action_type="button_flow",
        target_type=target_type,
        target_id=target_id,
        payload_json=json.dumps(payload, sort_keys=True),
        program_version=program_version(session),
        sync_version=sync_version(session),
        calendar_version=calendar_version(session),
        created_at=now,
        expires_at=now + timedelta(hours=1),
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


def begin_schedule_flow(session: Session) -> FlowTurn:
    program = active_program(session)
    if program is None:
        return FlowTurn("No active training program is available.", None)
    cursor = session.get(ProgramCursor, program.id)
    program_session = (
        session.get(ProgramSession, cursor.next_program_session_id)
        if cursor and cursor.next_program_session_id
        else (
            session.query(ProgramSession)
            .filter(ProgramSession.program_id == program.id)
            .order_by(ProgramSession.sequence_order, ProgramSession.id)
            .first()
        )
    )
    if program_session is None:
        return FlowTurn("The active program has no schedulable session.", None)
    today = get_local_now().date()
    offered_dates = [
        (today + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    row = _new_flow(
        session,
        flow_type="schedule",
        payload={
            "flow_step": "choose_date",
            "program_session_id": program_session.id,
            "offered_dates": offered_dates,
            "offered_times": [],
        },
        target_type="program_session",
        target_id=program_session.id,
    )
    labels = [
        date.fromisoformat(value).strftime("%a %d %b")
        for value in offered_dates
    ]
    return FlowTurn(
        f"Choose a date for {program_session.name}.",
        _flow_markup(row, labels, "date"),
    )


def begin_reschedule_flow(
    session: Session, planned_session_id: int | None = None
) -> FlowTurn:
    now = get_local_now().replace(tzinfo=None)
    if planned_session_id is not None:
        planned_rows = [session.get(PlannedSession, planned_session_id)]
        planned_rows = [row for row in planned_rows if row is not None]
    else:
        planned_rows = (
            session.query(PlannedSession)
            .filter(
                PlannedSession.target_date >= now.date(),
                PlannedSession.status.notin_(("completed", "cancelled", "replaced_by_active_recovery", "rest_selected")),
            )
            .order_by(PlannedSession.target_date, PlannedSession.suggested_time)
            .limit(8)
            .all()
        )
    if not planned_rows:
        return FlowTurn("No upcoming workout is available to reschedule.", None)
    ids = [row.id for row in planned_rows]
    flow_step = "choose_session" if len(ids) > 1 else "choose_date"
    target = ids[0] if len(ids) == 1 else None
    offered_dates = [
        (now.date() + timedelta(days=offset)).isoformat()
        for offset in range(7)
    ]
    row = _new_flow(
        session,
        flow_type="reschedule",
        payload={
            "flow_step": flow_step,
            "planned_session_id": target,
            "offered_planned_session_ids": ids,
            "offered_dates": offered_dates,
            "offered_times": [],
        },
        target_type="planned_session",
        target_id=target,
    )
    if flow_step == "choose_session":
        labels = [
            f"{planned.title} · {planned.target_date:%a}"
            for planned in planned_rows
        ]
        return FlowTurn(
            "Choose the workout to reschedule.",
            _flow_markup(row, labels, "session"),
        )
    labels = [
        date.fromisoformat(value).strftime("%a %d %b")
        for value in offered_dates
    ]
    return FlowTurn(
        f"Choose a new date for {planned_rows[0].title}.",
        _flow_markup(row, labels, "date"),
    )


def begin_alternate_time(
    session: Session, interaction_id: str
) -> FlowTurn:
    source = session.get(PendingInteraction, interaction_id)
    if (
        source is None
        or source.status != "pending"
        or source.action_type != "schedule_original_session"
    ):
        return FlowTurn("This proposal is no longer available.", None)
    payload = json.loads(source.payload_json)
    source.status = "superseded"
    source.failure_reason = "different_time_requested"
    offered_dates = [
        (get_local_now().date() + timedelta(days=offset)).isoformat()
        for offset in range(7)
    ]
    row = _new_flow(
        session,
        flow_type="schedule",
        payload={
            "flow_step": "choose_date",
            "program_session_id": payload["program_session_id"],
            "offered_dates": offered_dates,
            "offered_times": [],
        },
        target_type="program_session",
        target_id=int(payload["program_session_id"]),
    )
    labels = [
        date.fromisoformat(value).strftime("%a %d %b")
        for value in offered_dates
    ]
    return FlowTurn(
        "Choose another date.",
        _flow_markup(row, labels, "date"),
    )


def _flow_stale(row: PendingInteraction | None, now: datetime) -> bool:
    return bool(
        row is None
        or row.status != "pending"
        or row.action_type != "button_flow"
        or row.expires_at < now
    )


def advance_button_flow(session: Session, callback_data: str) -> FlowTurn:
    parts = callback_data.split(":")
    if len(parts) != 5 or parts[0] != "flow":
        return FlowTurn("This choice is no longer available.", None)
    _, interaction_id, nonce, kind, raw_index = parts
    row = session.get(PendingInteraction, interaction_id)
    now = get_local_now().replace(tzinfo=None)
    if _flow_stale(row, now):
        if row and row.status == "pending":
            row.status = "expired"
        return FlowTurn("This choice expired. Start again from the menu.", None)
    payload = json.loads(row.payload_json)
    if payload.get("nonce") != nonce:
        return FlowTurn("This choice is no longer current.", None)
    if kind == "cancel":
        row.status = "rejected"
        row.failure_reason = "user_cancelled"
        return FlowTurn("Flow cancelled. Nothing was changed.", None)
    try:
        index = int(raw_index)
    except ValueError:
        return FlowTurn("This choice is invalid.", None)

    if payload["flow_step"] == "choose_session" and kind == "session":
        offered = payload["offered_planned_session_ids"]
        if not 0 <= index < len(offered):
            return FlowTurn("This choice is invalid.", None)
        planned = session.get(PlannedSession, int(offered[index]))
        if planned is None or planned.status in {"completed", "cancelled", "replaced_by_active_recovery", "rest_selected"}:
            row.status = "superseded"
            return FlowTurn("That workout is no longer current.", None)
        if payload.get("flow_type") == "cancel":
            cancel_now = get_local_now().replace(tzinfo=None)
            versions = (program_version(session), sync_version(session), calendar_version(session))
            cancel_row = PendingInteraction(
                interaction_id=str(uuid4()),
                decision_id=None,
                action_type="cancel_planned_session",
                target_type="planned_session",
                target_id=planned.id,
                payload_json=json.dumps({"planned_session_id": planned.id}, sort_keys=True),
                program_version=versions[0],
                sync_version=versions[1],
                calendar_version=versions[2],
                created_at=cancel_now,
                expires_at=cancel_now + timedelta(hours=1),
                status="pending",
            )
            session.add(cancel_row)
            row.status = "superseded"
            row.failure_reason = "session_selected"
            session.flush()
            text = f"Cancel {planned.title} on {planned.target_date:%a %d %b}?"
            return FlowTurn(text, reply_markup([cancel_row]))
        payload["planned_session_id"] = planned.id
        payload["flow_step"] = "choose_date"
        row.target_id = planned.id
        row.payload_json = json.dumps(payload, sort_keys=True)
        labels = [
            date.fromisoformat(value).strftime("%a %d %b")
            for value in payload["offered_dates"]
        ]
        return FlowTurn(
            f"Choose a new date for {planned.title}.",
            _flow_markup(row, labels, "date"),
        )

    if payload["flow_step"] == "choose_date" and kind == "date":
        offered = payload["offered_dates"]
        if not 0 <= index < len(offered):
            return FlowTurn("This choice is invalid.", None)
        payload["target_date"] = offered[index]
        target_day = date.fromisoformat(payload["target_date"])
        if payload.get("flow_type") == "reschedule":
            planned = session.get(PlannedSession, int(payload["planned_session_id"]))
            duration_min = planned.duration_min if planned else None
        else:
            program_session = session.get(ProgramSession, int(payload["program_session_id"]))
            duration_min = (program_session.duration_min or 60) if program_session else None
        if not duration_min:
            return FlowTurn("That workout is no longer current.", None)
        from coach.calendar import get_upcoming_schedule_result
        from coach.scheduling import available_start_times
        days = max(2, (target_day - now.date()).days + 1)
        calendar = get_upcoming_schedule_result(days=days)
        if calendar["state"] != "fresh":
            return FlowTurn("Times cannot safely be checked right now. Choose another date or Cancel.", _flow_markup(row, [date.fromisoformat(value).strftime("%a %d %b") for value in offered], "date"))
        starts = available_start_times(
            session, now=now, schedule=calendar["events"], target_day=target_day,
            duration_min=duration_min, limit=8,
        )
        payload["offered_times"] = [value.strftime("%H:%M") for value in starts]
        if not payload["offered_times"]:
            row.payload_json = json.dumps(payload, sort_keys=True)
            return FlowTurn("No available time fits on that date. Choose another date or Cancel.", _flow_markup(row, [date.fromisoformat(value).strftime("%a %d %b") for value in offered], "date"))
        payload["flow_step"] = "choose_time"
        row.payload_json = json.dumps(payload, sort_keys=True)
        return FlowTurn(
            "Choose a time.",
            _flow_markup(row, payload["offered_times"], "time"),
        )

    if payload["flow_step"] == "choose_time" and kind == "time":
        offered = payload["offered_times"]
        if not 0 <= index < len(offered):
            return FlowTurn("This choice is invalid.", None)
        selected_time = offered[index]
        target_date = payload["target_date"]
        if payload["flow_type"] == "reschedule":
            planned = session.get(
                PlannedSession, int(payload["planned_session_id"])
            )
            if planned is None or planned.status in {"completed", "cancelled", "replaced_by_active_recovery", "rest_selected"}:
                row.status = "superseded"
                return FlowTurn("That workout is no longer current.", None)
            row.action_type = "reschedule_planned_time"
            row.target_type = "planned_session"
            row.target_id = planned.id
            row.payload_json = json.dumps(
                {
                    "flow_type": "reschedule",
                    "flow_step": "confirm",
                    "planned_session_id": planned.id,
                    "target_date": target_date,
                    "suggested_time": selected_time,
                    "offered_times": offered,
                    "page": payload.get("page", 0),
                },
                sort_keys=True,
            )
            text = (
                f"Confirm: move {planned.title} to "
                f"{target_date} at {selected_time}."
            )
        else:
            program_session = session.get(
                ProgramSession, int(payload["program_session_id"])
            )
            if program_session is None:
                row.status = "superseded"
                return FlowTurn("That program session is no longer current.", None)
            row.action_type = "schedule_original_session"
            row.target_type = "program_session"
            row.target_id = program_session.id
            row.payload_json = json.dumps(
                {
                    "action": "schedule_session",
                    "flow_type": "schedule",
                    "flow_step": "confirm",
                    "program_session_id": program_session.id,
                    "activity_type": (
                        program_session.sport_type or "strength_training"
                    ),
                    "title": program_session.name,
                    "target_date": target_date,
                    "suggested_time": selected_time,
                    "duration_min": program_session.duration_min or 60,
                    "intensity": "normal",
                    "modifications": [],
                    "offered_times": offered,
                    "page": payload.get("page", 0),
                },
                sort_keys=True,
            )
            text = (
                f"Confirm: schedule {program_session.name} on "
                f"{target_date} at {selected_time}."
            )
        return FlowTurn(text, reply_markup([row]))
    return FlowTurn("This choice is no longer current.", None)


def stage_sync_confirmation(session: Session) -> FlowTurn:
    now = get_local_now().replace(tzinfo=None)
    row = PendingInteraction(
        interaction_id=str(uuid4()),
        decision_id=None,
        action_type="start_sync",
        target_type="sync",
        target_id=None,
        payload_json=json.dumps({"action": "start_sync"}, sort_keys=True),
        program_version=program_version(session),
        sync_version=sync_version(session),
        calendar_version=calendar_version(session),
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status="pending",
    )
    session.add(row)
    session.flush()
    return FlowTurn("Start a Garmin sync now?", reply_markup([row]))


def begin_cancel_flow(session: Session) -> FlowTurn:
    """Show a single-workout confirmation or a selection list, then confirm."""
    now = get_local_now().replace(tzinfo=None)
    planned_rows = (
        session.query(PlannedSession)
        .filter(
            PlannedSession.target_date >= now.date(),
            PlannedSession.status == "approved",
        )
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time)
        .limit(8)
        .all()
    )
    if not planned_rows:
        return FlowTurn("No approved upcoming workout is available to cancel.", None)
    versions = (program_version(session), sync_version(session), calendar_version(session))
    if len(planned_rows) == 1:
        planned = planned_rows[0]
        row = PendingInteraction(
            interaction_id=str(uuid4()),
            decision_id=None,
            action_type="cancel_planned_session",
            target_type="planned_session",
            target_id=planned.id,
            payload_json=json.dumps({"planned_session_id": planned.id}, sort_keys=True),
            program_version=versions[0],
            sync_version=versions[1],
            calendar_version=versions[2],
            created_at=now,
            expires_at=now + timedelta(hours=1),
            status="pending",
        )
        session.add(row)
        session.flush()
        text = f"Cancel {planned.title} on {planned.target_date:%a %d %b}?"
        return FlowTurn(text, reply_markup([row]))
    ids = [p.id for p in planned_rows]
    flow_row = _new_flow(
        session,
        flow_type="cancel",
        payload={
            "flow_step": "choose_session",
            "offered_planned_session_ids": ids,
        },
        target_type="planned_session",
        target_id=None,
    )
    labels = [
        f"{planned.title} · {planned.target_date:%a}"
        for planned in planned_rows
    ]
    return FlowTurn("Choose a workout to cancel.", _flow_markup(flow_row, labels, "session"))


def stage_cancel_choices(session: Session) -> FlowTurn:
    now = get_local_now().replace(tzinfo=None)
    planned_rows = (
        session.query(PlannedSession)
        .filter(
            PlannedSession.target_date >= now.date(),
            PlannedSession.status == "approved",
        )
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time)
        .limit(8)
        .all()
    )
    if not planned_rows:
        return FlowTurn("No approved upcoming workout is available to cancel.", None)
    versions = (program_version(session), sync_version(session), calendar_version(session))
    interactions = []
    for planned in planned_rows:
        row = PendingInteraction(
            interaction_id=str(uuid4()),
            decision_id=None,
            action_type="cancel_planned_session",
            target_type="planned_session",
            target_id=planned.id,
            payload_json=json.dumps(
                {
                    "planned_session_id": planned.id,
                    "selection_label": f"{planned.title} · {planned.target_date:%a}",
                },
                sort_keys=True,
            ),
            program_version=versions[0],
            sync_version=versions[1],
            calendar_version=versions[2],
            created_at=now,
            expires_at=now + timedelta(hours=1),
            status="pending",
        )
        session.add(row)
        interactions.append(row)
    session.flush()
    return FlowTurn("Choose a workout to cancel.", reply_markup(interactions))
