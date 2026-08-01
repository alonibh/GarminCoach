"""Durable notification queue with quiet hours and delivery revalidation."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import logging

from sqlalchemy import and_, or_, update
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


logger = logging.getLogger(__name__)
_DELIVERY_LEASE = timedelta(minutes=2)


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
    if not planned.suggested_time or planned.status in {"completed", "cancelled", "replaced_by_active_recovery", "rest_selected"}:
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


def _materialize(session: Session, row: NotificationOutbox, now: datetime) -> tuple[str, dict | None] | tuple[str, None, None] | None:
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    if row.event_type == "strength_progression_ready":
        from coach.strength_progression_notifications import materialize_progression_summary
        materialized = materialize_progression_summary(session, batch_id=str(payload.get("batch_id", "")), now=now)
        if materialized is None:
            return None
        # A three-tuple is intentionally narrow: existing event types retain
        # their current Markdown/default transport behavior.
        return materialized.text, materialized.reply_markup, materialized.parse_mode
    if row.event_type == "morning_briefing":
        if not _decision_is_current(session, row):
            day = row.due_at.date()
            sent_brief = (
                session.query(NotificationOutbox)
                .filter(
                    NotificationOutbox.event_type == "morning_briefing",
                    NotificationOutbox.status == "sent",
                    NotificationOutbox.sent_at >= datetime.combine(day, time.min),
                    NotificationOutbox.sent_at <= datetime.combine(day, time.max),
                )
                .first()
            )
            if sent_brief:
                return None

            record = session.get(DecisionRecord, row.decision_id) if row.decision_id else None
            best_effort = False
            if record:
                source = json.loads(record.result_json)
                best_effort = bool(source.get("best_effort"))
            from coach.decision_engine import evaluate_morning_decision
            from coach.interactions import prepare_recovery_morning
            current = evaluate_morning_decision(
                session,
                allow_incomplete=best_effort,
                target=day,
                evaluated_at=get_local_now(),
            )
            text, interaction_ids = prepare_recovery_morning(session, current)
            if not text:
                return None
            from coach.interactions import reply_markup_for_ids
            formatted_text = f"*Morning Briefing*\n\n{text}"
            row.decision_id = current.decision_id
            row.idempotency_key = f"briefing:{current.idempotency_key}"
            row.payload_json = json.dumps(
                {"text": formatted_text, "interaction_ids": interaction_ids},
                sort_keys=True,
            )
            return formatted_text, reply_markup_for_ids(session, interaction_ids)

        from coach.interactions import reply_markup_for_ids
        return payload["text"], reply_markup_for_ids(session, payload.get("interaction_ids", []))

    if row.event_type == "morning_deadline":
        state = session.get(MorningBriefState, now.date())
        if state and state.briefing_sent_at:
            return None
        return payload["text"], payload.get("reply_markup")

    if row.event_type == "pre_workout_reminder":
        planned = session.get(PlannedSession, payload["planned_session_id"])
        if not planned or planned.status in {"completed", "cancelled", "replaced_by_active_recovery", "rest_selected"}:
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
        try:
            raw_week_end = payload.get("week_end")
            if not isinstance(raw_week_end, str) or len(raw_week_end) != 10:
                return None
            week_end = date.fromisoformat(raw_week_end)
            if week_end.isoformat() != raw_week_end or week_end > now.date():
                return None
            if now.date() > week_end + timedelta(days=6):
                return None
            from notify.weekly_report import build_weekly_summary_report, render_weekly_summary
            # Only a same-day delivery can include an overnight observation for
            # Saturday, and only when the existing freshness evidence permits it.
            overnight_ready = False
            if week_end == now.date():
                from metrics.freshness import proactive_metrics_ready
                overnight_ready = proactive_metrics_ready(session, day=week_end)
            report = build_weekly_summary_report(
                session, week_end=week_end, generated_at=now, overnight_today_ready=overnight_ready,
            )
            return render_weekly_summary(report), None, None
        except (TypeError, ValueError):
            # Delivery validation deliberately exposes neither malformed payload
            # data nor renderer exceptions to Telegram or logs.
            return None

    if row.event_type == "late_material_update":
        if not _decision_is_current(session, row):
            return None
        from coach.interactions import reply_markup_for_ids
        return payload["text"], reply_markup_for_ids(session, payload.get("interaction_ids", []))

    if row.event_type in {"calendar_access_error", "calendar_conflict"}:
        return payload["text"], payload.get("reply_markup")
    return None


def _claim_notification(session: Session, row: NotificationOutbox, now: datetime) -> bool:
    """Atomically lease a due job before making an external delivery call."""
    claimable = or_(
        NotificationOutbox.status == "pending",
        and_(NotificationOutbox.status == "delivering", NotificationOutbox.due_at <= now),
    )
    claimed = session.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == row.id,
            NotificationOutbox.due_at <= now,
            claimable,
        )
        .values(status="delivering", due_at=now + _DELIVERY_LEASE)
    )
    if claimed.rowcount != 1:
        return False
    # Persist the lease before Telegram I/O so another session cannot also send.
    session.commit()
    session.refresh(row)
    logger.info("Claimed notification row_id=%s event_type=%s", row.id, row.event_type)
    return True


def _reconcile_delivered_morning_brief(session: Session, row: NotificationOutbox, now: datetime) -> None:
    if row.event_type != "morning_briefing":
        return
    state = session.get(MorningBriefState, row.due_at.date())
    if state is None:
        return
    state.briefing_sent_at = now
    state.status = "complete"
    state.updated_at = now


def deliver_notification(session: Session, row: NotificationOutbox, now: datetime) -> str:
    """Deliver one row and return sent/cancelled/failed/deferred/retry."""
    now = now.replace(tzinfo=None)
    if row.status in {"sent", "cancelled", "failed"} or row.due_at > now:
        return "retry"
    if not _claim_notification(session, row, now):
        logger.info("Skipped unclaimable notification row_id=%s event_type=%s", row.id, row.event_type)
        return "retry"
    if row.quiet_hour_policy == "defer":
        deferred = _quiet_defer(now)
        if deferred:
            row.due_at = deferred
            row.status = "pending"
            session.commit()
            return "deferred"
    materialized = _materialize(session, row, now)
    if not materialized:
        row.status = "cancelled"
        row.last_error = "revalidation_failed"
        for interaction_id in json.loads(row.payload_json).get("interaction_ids", []):
            from coach.interactions import mark_delivery_failed
            mark_delivery_failed(session, [interaction_id], "revalidation_failed")
        if row.event_type == "strength_progression_ready":
            from coach.strength_progression_notifications import reconcile_progression_notification_outcome
            reconcile_progression_notification_outcome(session, outbox_id=row.id, outcome="cancelled", now=now,
                                                       reason="revalidation_failed")
        session.commit()
        logger.info("Cancelled notification row_id=%s event_type=%s", row.id, row.event_type)
        return "cancelled"
    text, markup = materialized[0], materialized[1]
    parse_mode = materialized[2] if len(materialized) == 3 else None
    row.attempts += 1
    try:
        delivered = (send_message(text, reply_markup=markup, parse_mode=parse_mode)
                     if len(materialized) == 3 else send_message(text, reply_markup=markup))
    except Exception as exc:
        delivered = False
        row.last_error = type(exc).__name__
    if delivered:
        row.status = "sent"
        row.sent_at = now
        _reconcile_delivered_morning_brief(session, row, now)
        if row.event_type == "strength_progression_ready":
            from coach.strength_progression_notifications import reconcile_progression_notification_outcome
            reconcile_progression_notification_outcome(session, outbox_id=row.id, outcome="sent", now=now)
        session.commit()
        logger.info("Sent notification row_id=%s event_type=%s", row.id, row.event_type)
        return "sent"
    if row.attempts >= 5:
        row.status = "failed"
        row.last_error = row.last_error or "telegram_delivery_failed"
        for interaction_id in json.loads(row.payload_json).get("interaction_ids", []):
            from coach.interactions import mark_delivery_failed
            mark_delivery_failed(session, [interaction_id], "telegram_delivery_failed")
        if row.event_type == "strength_progression_ready":
            from coach.strength_progression_notifications import reconcile_progression_notification_outcome
            reconcile_progression_notification_outcome(session, outbox_id=row.id, outcome="failed", now=now,
                                                       reason="telegram_delivery_failed")
        session.commit()
        logger.info("Failed notification row_id=%s event_type=%s", row.id, row.event_type)
        return "failed"
    row.due_at = now + timedelta(minutes=5)
    row.status = "pending"
    row.last_error = row.last_error or "telegram_delivery_failed"
    session.commit()
    logger.info("Retrying notification row_id=%s event_type=%s", row.id, row.event_type)
    return "retry"


def process_due_notifications(now: datetime | None = None, *, limit: int = 25) -> dict:
    now = (now or get_local_now()).replace(tzinfo=None)
    result = {"sent": 0, "cancelled": 0, "failed": 0, "deferred": 0, "retry": 0}
    with get_session() as session:
        # Retained local intent is retried by this existing tenant-scoped poller;
        # a bridge failure must not stop unrelated notifications.
        try:
            from coach.strength_progression_notifications import bridge_pending_progression_notifications
            with session.begin_nested():
                bridge_pending_progression_notifications(session, now=now, limit=limit)
        except Exception:
            logger.exception("strength progression notification bridge failed")
        rows = (
            session.query(NotificationOutbox)
            .filter(
                NotificationOutbox.status.in_(("pending", "delivering")),
                NotificationOutbox.due_at <= now,
            )
            .order_by(NotificationOutbox.due_at, NotificationOutbox.id)
            .limit(limit)
            .all()
        )
        for row in rows:
            outcome = deliver_notification(session, row, now)
            result[outcome] += 1
    return result
