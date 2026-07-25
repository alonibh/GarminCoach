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
    if message and message.created_at:
        pending_outbox = (
            session.query(NotificationOutbox)
            .filter(
                NotificationOutbox.event_type == "morning_briefing",
                NotificationOutbox.status == "pending",
                NotificationOutbox.created_at >= datetime.combine(day, time.min),
                NotificationOutbox.created_at <= datetime.combine(day, time.max),
            )
            .first()
        )
        if pending_outbox:
            return None
        return message.created_at.replace(tzinfo=None)
    return None


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
            enqueue_late_material_update(session)
            return
        if facts["ready"] or row.answer_anyway:
            row.status = "evaluating"
            sent = generate_daily_suggestion(session, allow_incomplete=row.answer_anyway)
            if sent:
                row.briefing_sent_at = _now_naive()
                row.status = "complete"
            else:
                row.status = "waiting_for_program"
        else:
            row.status = "waiting"
        row.updated_at = _now_naive()


def enqueue_late_material_update(session) -> bool:
    """Send one correction when new facts materially change today's call."""
    now = get_local_now()
    if now.hour < 7 or now.hour >= 22:
        return False
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
        return False
    first = session.get(DecisionRecord, sent_brief.decision_id)
    if not first:
        return False
    original = json.loads(first.result_json)
    current = evaluate_morning_decision(session, target=day, evaluated_at=now)
    became_poor = (
        original.get("decision_type") != "ADVISE_SKIP_SESSION"
        and current.decision_type == "ADVISE_SKIP_SESSION"
    )
    program_became_available = (
        original.get("workout_outcome") == "NO_ACTION"
        and current.workout_outcome in {
            "KEEP_PLANNED_SESSION", "PROPOSE_NEXT_SESSION", "PROGRAM_REST_DAY",
        }
    )
    was_best_effort = bool(original.get("best_effort"))
    now_complete = not current.best_effort
    became_complete = was_best_effort and now_complete

    if not (became_poor or program_became_available or became_complete):
        return False
    existing = session.query(NotificationOutbox).filter_by(
        idempotency_key=f"late-update:{current.idempotency_key}"
    ).first()
    if existing:
        return False
    from coach.renderer import render_morning
    text, _markup, interaction_ids = render_morning(session, current)
    if not text:
        return False
    from notify.outbox import enqueue_notification
    enqueue_notification(
        session,
        event_type="late_material_update",
        due_at=now.replace(tzinfo=None),
        payload={
            "text": f"*Morning Briefing Update*\n\n{text}",
            "interaction_ids": interaction_ids,
        },
        decision_id=current.decision_id,
        idempotency_key=f"late-update:{current.idempotency_key}",
    )
    return True


# Compatibility for internal callers/tests that used the original private name.
_enqueue_late_material_update = enqueue_late_material_update


def morning_deadline() -> bool:
    """At 11:30, stop waiting and issue one explicit best-effort briefing."""
    with get_session() as session:
        row = _state(session)
        if reconcile_sent_brief(session, row) or row.deadline_prompt_sent_at:
            return False
        facts = morning_freshness(session)
        if facts["ready"]:
            sent = generate_daily_suggestion(session)
            if sent:
                row.briefing_sent_at = _now_naive()
                row.status = "complete"
            else:
                row.status = "waiting_for_program"
            row.updated_at = _now_naive()
            return sent

        row.answer_anyway = True
        row.status = "evaluating"
        sent = generate_daily_suggestion(session, allow_incomplete=True)
        if sent:
            row.briefing_sent_at = _now_naive()
            row.status = "complete"
        else:
            row.status = "waiting_for_program"
        row.updated_at = _now_naive()
        return sent


def answer_anyway(day_key: str) -> bool:
    if day_key != get_local_date().strftime("%Y%m%d"):
        return False
    with get_session() as session:
        row = _state(session)
        if row.briefing_sent_at:
            return False
        row.answer_anyway = True
        row.status = "evaluating"
        sent = generate_daily_suggestion(session, allow_incomplete=True)
        if sent:
            row.briefing_sent_at = _now_naive()
            row.status = "complete"
        else:
            row.status = "waiting_for_program"
        row.updated_at = _now_naive()
        return sent
