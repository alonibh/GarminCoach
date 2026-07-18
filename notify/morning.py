"""Durable morning data-wait and 11:30 deadline flow."""
from __future__ import annotations

from datetime import datetime, time
import json

from coach.coach import generate_daily_suggestion
from db import DecisionRecord, MorningBriefState, NotificationOutbox, get_session
from metrics.freshness import ERROR, morning_freshness
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


def start_priority_fetch() -> bool:
    """Mark required facts pending and start the shared-lock priority fetch."""
    from metrics.freshness import mark_priority_pending
    from sync import sync_runner

    with get_session() as session:
        row = _state(session)
        if row.briefing_sent_at:
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
        if row.briefing_sent_at:
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
    """At 11:30, turn unresolved critical data into an explicit user choice."""
    with get_session() as session:
        row = _state(session)
        if row.briefing_sent_at or row.deadline_prompt_sent_at:
            return False
        facts = morning_freshness(session)
        if facts["ready"]:
            generate_daily_suggestion(session)
            row.briefing_sent_at = _now_naive()
            row.status = "complete"
            row.updated_at = _now_naive()
            return True

        critical_states = [facts["states"].get(signal) for signal in facts["missing_critical"]]
        fetch_error = ERROR in critical_states
        text = (
            "Garmin data could not be fetched. Retry the fetch, or continue without the missing data."
            if fetch_error else
            "Required overnight data is missing. Sync your watch, then retry, or continue without it."
        )
        day_key = get_local_date().strftime("%Y%m%d")
        markup = {
            "inline_keyboard": [[
                {"text": "I synced the watch", "callback_data": f"morning_synced_{day_key}"},
                {"text": "Answer anyway", "callback_data": f"morning_anyway_{day_key}"},
            ]]
        }
        from notify.outbox import deliver_notification, enqueue_notification
        queued = enqueue_notification(
            session,
            event_type="morning_deadline",
            due_at=_now_naive(),
            payload={"text": text, "reply_markup": markup},
            idempotency_key=f"morning-deadline:{get_local_date().isoformat()}",
        )
        session.flush()
        deliver_notification(session, queued, _now_naive())
        row.deadline_prompt_sent_at = _now_naive()
        row.status = "sync_required" if not fetch_error else "fetch_error"
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
