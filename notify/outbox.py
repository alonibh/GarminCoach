"""Durable notification queue with quiet hours and delivery revalidation."""
from __future__ import annotations

from datetime import datetime, time, timedelta
import json

from sqlalchemy.orm import Session

from db import (
    DecisionRecord,
    MorningBriefState,
    NotificationOutbox,
    PlannedSession,
    get_session,
)
from notify.telegram import send_message
from time_utils import get_local_now


def enqueue_notification(
    session: Session,
    *,
    event_type: str,
    due_at: datetime,
    payload: dict,
    idempotency_key: str,
    decision_id: str | None = None,
    quiet_hour_policy: str = "defer",
) -> NotificationOutbox:
    existing = session.query(NotificationOutbox).filter_by(idempotency_key=idempotency_key).first()
    if existing:
        return existing
    now = get_local_now().replace(tzinfo=None)
    row = NotificationOutbox(
        event_type=event_type,
        due_at=due_at.replace(tzinfo=None),
        quiet_hour_policy=quiet_hour_policy,
        payload_json=json.dumps(payload, sort_keys=True),
        decision_id=decision_id,
        status="pending",
        attempts=0,
        idempotency_key=idempotency_key,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def enqueue_pre_workout_reminder(session: Session, planned: PlannedSession) -> NotificationOutbox | None:
    if not planned.suggested_time or planned.status in {"completed", "cancelled"}:
        return None
    try:
        hour, minute = map(int, planned.suggested_time.split(":"))
        workout_at = datetime.combine(planned.target_date, time(hour, minute))
    except (TypeError, ValueError):
        return None
    due = workout_at - timedelta(hours=1)
    return enqueue_notification(
        session,
        event_type="pre_workout_reminder",
        due_at=due,
        payload={
            "planned_session_id": planned.id,
            "target_date": planned.target_date.isoformat(),
            "start_time": planned.suggested_time,
            "title": planned.title,
        },
        idempotency_key=f"preworkout:{planned.id}:{planned.target_date}:{planned.suggested_time}",
    )


def _quiet_defer(now: datetime) -> datetime | None:
    if now.hour >= 22:
        return datetime.combine(now.date() + timedelta(days=1), time(7, 0))
    if now.hour < 7:
        return datetime.combine(now.date(), time(7, 0))
    return None


def _decision_is_current(session: Session, row: NotificationOutbox) -> bool:
    if not row.decision_id:
        return True
    record = session.get(DecisionRecord, row.decision_id)
    if not record:
        return False
    source = json.loads(record.result_json)
    try:
        decision_day = datetime.fromisoformat(
            source.get("decision_date") or source["idempotency_key"].split(":", 2)[1]
        ).date()
    except (KeyError, IndexError, ValueError):
        decision_day = datetime.fromisoformat(source["evaluated_at"]).date()
    from coach.decision_engine import evaluate_morning_decision
    current = evaluate_morning_decision(
        session,
        allow_incomplete=bool(source.get("best_effort")),
        target=decision_day,
        evaluated_at=get_local_now(),
    )
    return current.idempotency_key == source["idempotency_key"]


def _materialize(session: Session, row: NotificationOutbox, now: datetime) -> tuple[str, dict | None] | None:
    payload = json.loads(row.payload_json)
    if row.event_type == "morning_briefing":
        if not _decision_is_current(session, row):
            return None
        from coach.interactions import reply_markup_for_ids
        return payload["text"], reply_markup_for_ids(session, payload.get("interaction_ids", []))

    if row.event_type == "morning_deadline":
        state = session.get(MorningBriefState, now.date())
        if state and state.briefing_sent_at:
            return None
        return payload["text"], payload.get("reply_markup")

    if row.event_type == "pre_workout_reminder":
        planned = session.get(PlannedSession, payload["planned_session_id"])
        if not planned or planned.status in {"completed", "cancelled"}:
            return None
        if planned.target_date.isoformat() != payload["target_date"] or planned.suggested_time != payload["start_time"]:
            return None
        workout_at = datetime.combine(
            planned.target_date, datetime.strptime(planned.suggested_time, "%H:%M").time()
        )
        if workout_at <= now:
            return None
        from coach.calendar import find_calendar_conflict, get_upcoming_schedule_result
        calendar = get_upcoming_schedule_result(days=2)
        if calendar["state"] == "error":
            enqueue_notification(
                session,
                event_type="calendar_access_error",
                due_at=now,
                payload={"text": "Calendar check failed before the planned workout."},
                idempotency_key=f"calendar-error:{planned.id}:{planned.target_date}",
                quiet_hour_policy="defer",
            )
        conflict = find_calendar_conflict(
            calendar["events"], planned.target_date, planned.suggested_time, planned.duration_min
        )
        if conflict:
            from coach.interactions import reply_markup, stage_calendar_conflict
            staged = stage_calendar_conflict(session, planned, conflict)
            text = (
                f"Calendar conflict: {planned.title} at {planned.suggested_time} "
                f"overlaps {conflict.get('title', 'another event')}."
            )
            return text, reply_markup(staged)
        title = planned.title.strip()
        workout_name = title if title.casefold().endswith("workout") else f"{title} workout"
        return f"Workout reminder — you have the {workout_name} one hour from now.", None

    if row.event_type == "weekly_summary":
        from notify.weekly import build_weekly_summary
        return build_weekly_summary(session, datetime.fromisoformat(payload["week_end"]).date()), None

    if row.event_type == "late_material_update":
        if not _decision_is_current(session, row):
            return None
        from coach.interactions import reply_markup_for_ids
        return payload["text"], reply_markup_for_ids(session, payload.get("interaction_ids", []))

    if row.event_type in {"calendar_access_error", "calendar_conflict"}:
        return payload["text"], payload.get("reply_markup")
    return None


def deliver_notification(session: Session, row: NotificationOutbox, now: datetime) -> str:
    """Deliver one row and return sent/cancelled/failed/deferred/retry."""
    now = now.replace(tzinfo=None)
    if row.status != "pending" or row.due_at > now:
        return "retry"
    if row.quiet_hour_policy == "defer":
        deferred = _quiet_defer(now)
        if deferred:
            row.due_at = deferred
            return "deferred"
    materialized = _materialize(session, row, now)
    if not materialized:
        row.status = "cancelled"
        row.last_error = "revalidation_failed"
        return "cancelled"
    text, markup = materialized
    row.attempts += 1
    try:
        delivered = send_message(text, reply_markup=markup)
    except Exception as exc:
        delivered = False
        row.last_error = type(exc).__name__
    if delivered:
        row.status = "sent"
        row.sent_at = now
        return "sent"
    if row.attempts >= 5:
        row.status = "failed"
        row.last_error = row.last_error or "telegram_delivery_failed"
        return "failed"
    row.due_at = now + timedelta(minutes=5)
    row.last_error = row.last_error or "telegram_delivery_failed"
    return "retry"


def process_due_notifications(now: datetime | None = None, *, limit: int = 25) -> dict:
    now = (now or get_local_now()).replace(tzinfo=None)
    result = {"sent": 0, "cancelled": 0, "failed": 0, "deferred": 0, "retry": 0}
    with get_session() as session:
        rows = (
            session.query(NotificationOutbox)
            .filter(NotificationOutbox.status == "pending", NotificationOutbox.due_at <= now)
            .order_by(NotificationOutbox.due_at, NotificationOutbox.id)
            .limit(limit)
            .all()
        )
        for row in rows:
            outcome = deliver_notification(session, row, now)
            result[outcome] += 1
    return result
