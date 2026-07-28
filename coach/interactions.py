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

from coach.decision_engine import DecisionResult, evaluate_morning_decision
from coach.onboarding import active_program
from db import (
    DecisionRecord,
    PendingInteraction,
    PlannedSession,
    ProgramCursor,
    ProgramSession,
    SyncState,
)
from time_utils import get_local_now

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
        if str(item_workout) == str(workout_id) and (not item_date or str(item_date)[:10] == target_day.isoformat()):
            try:
                return int(scheduled_id)
            except (TypeError, ValueError):
                return None
    return None


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
                PlannedSession.status.notin_(("completed", "cancelled")),
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
        if planned is None or planned.status in {"completed", "cancelled"}:
            row.status = "superseded"
            return FlowTurn("That workout is no longer current.", None)
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
            if planned is None or planned.status in {"completed", "cancelled"}:
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
