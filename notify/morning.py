"""Durable morning data-wait and 11:30 deadline flow."""
from __future__ import annotations

from datetime import datetime, time
import json

from coach.coach import generate_daily_suggestion
from db import DecisionRecord, MorningBriefState, NotificationOutbox, get_session
from metrics.freshness import morning_freshness
from time_utils import get_local_date, get_local_now


def _state(session) -> MorningBriefState:
    today = get_local_date()
    row = session.get(MorningBriefState, today)
    if not row:
        now = get_local_now().replace(tzinfo=None)
        row = MorningBriefState(day=today, status="waiting", updated_at=now)
        session.add(row)
    return row


def _now_naive() -> datetime:
    return get_local_now().replace(tzinfo=None)


def _sent_brief_at(session) -> datetime | None:
    """Return today's actual briefing time, including pre-state-machine sends."""
    day = get_local_date()
    sent = (
        session.query(NotificationOutbox)
        .filter(
            NotificationOutbox.event_type == "morning_briefing",
            NotificationOutbox.status == "sent",
            NotificationOutbox.sent_at >= datetime.combine(day, time.min),
            NotificationOutbox.sent_at <= datetime.combine(day, time.max),
        )
        .order_by(NotificationOutbox.sent_at)
        .first()
    )
    if sent:
        return sent.sent_at

    # Compatibility with briefings sent directly before the durable outbox was
    # introduced. CoachMessage is the only durable receipt for those sends.
    from db import CoachMessage
    message = (
        session.query(CoachMessage)
        .filter(
            CoachMessage.role == "suggestion",
            CoachMessage.created_at >= datetime.combine(day, time.min),
            CoachMessage.created_at <= datetime.combine(day, time.max),
        )
        .order_by(CoachMessage.created_at)
        .first()
    )
    return message.created_at.replace(tzinfo=None) if message and message.created_at else None


def reconcile_sent_brief(session, row: MorningBriefState | None = None) -> bool:
    """Keep the daily state machine aligned with the delivery ledger."""
    row = row or _state(session)
    sent_at = row.briefing_sent_at or _sent_brief_at(session)
    if not sent_at:
        return False
    row.briefing_sent_at = sent_at.replace(tzinfo=None)
    row.status = "complete"
    row.updated_at = _now_naive()
    return True


def start_priority_fetch() -> bool:
    """Mark required facts pending and start the shared-lock priority fetch."""
    from metrics.freshness import mark_priority_pending
    from sync import sync_runner

    with get_session() as session:
        row = _state(session)
        if reconcile_sent_brief(session, row):
            return False
        mark_priority_pending(session)
        row.status = "fetching"
        row.updated_at = _now_naive()
    return sync_runner.try_start_priority_sync()


def priority_sync_finished() -> None:
    """Automatically issue the briefing once the priority facts are committed."""
    with get_session() as session:
        row = _state(session)
        row.last_priority_fetch_at = _now_naive()
        facts = morning_freshness(session)
        if reconcile_sent_brief(session, row):
            _enqueue_late_material_update(session)
            return
        if facts["ready"] or row.answer_anyway:
            row.status = "evaluating"
            generate_daily_suggestion(session, allow_incomplete=row.answer_anyway)
            row.briefing_sent_at = _now_naive()
            row.status = "complete"
        else:
            row.status = "waiting"
        row.updated_at = _now_naive()


def _enqueue_late_material_update(session) -> None:
    """Notify only when new daytime readiness changes the same-day call to Poor."""
    now = get_local_now()
    if now.hour < 7 or now.hour >= 22:
        return
    from coach.decision_engine import evaluate_morning_decision
    day = get_local_date()
    sent_brief = (
        session.query(NotificationOutbox)
        .filter(
            NotificationOutbox.event_type == "morning_briefing",
            NotificationOutbox.status == "sent",
            NotificationOutbox.sent_at >= datetime.combine(day, time.min),
            NotificationOutbox.sent_at <= datetime.combine(day, time.max),
        )
        .order_by(NotificationOutbox.sent_at, NotificationOutbox.id)
        .first()
    )
    if not sent_brief or not sent_brief.decision_id:
        return
    first = session.get(DecisionRecord, sent_brief.decision_id)
    if not first:
        return
    original = json.loads(first.result_json)
    current = evaluate_morning_decision(session, target=day, evaluated_at=now)
    if original.get("decision_type") == "ADVISE_SKIP_SESSION" or current.decision_type != "ADVISE_SKIP_SESSION":
        return
    existing = session.query(NotificationOutbox).filter_by(
        idempotency_key=f"late-update:{current.idempotency_key}"
    ).first()
    if existing:
        return
    from coach.renderer import render_morning
    text, _markup, interaction_ids = render_morning(session, current)
    if not text:
        return
    from notify.outbox import enqueue_notification
    enqueue_notification(
        session,
        event_type="late_material_update",
        due_at=now.replace(tzinfo=None),
        payload={"text": f"Update: {text}", "interaction_ids": interaction_ids},
        decision_id=current.decision_id,
        idempotency_key=f"late-update:{current.idempotency_key}",
    )


def morning_deadline() -> bool:
    """At 11:30, stop waiting and issue one explicit best-effort briefing."""
    with get_session() as session:
        row = _state(session)
        if reconcile_sent_brief(session, row) or row.deadline_prompt_sent_at:
            return False
        facts = morning_freshness(session)
        if facts["ready"]:
            generate_daily_suggestion(session)
            row.briefing_sent_at = _now_naive()
            row.status = "complete"
            row.updated_at = _now_naive()
            return True

        row.answer_anyway = True
        row.status = "evaluating"
        generate_daily_suggestion(session, allow_incomplete=True)
        row.briefing_sent_at = _now_naive()
        row.status = "complete"
        row.updated_at = _now_naive()
        return True


def answer_anyway(day_key: str) -> bool:
    if day_key != get_local_date().strftime("%Y%m%d"):
        return False
    with get_session() as session:
        row = _state(session)
        if row.briefing_sent_at:
            return False
        row.answer_anyway = True
        row.status = "evaluating"
        generate_daily_suggestion(session, allow_incomplete=True)
        row.briefing_sent_at = _now_naive()
        row.status = "complete"
        row.updated_at = _now_naive()
        return True
