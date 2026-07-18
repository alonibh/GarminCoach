"""Durable morning data-wait and 11:30 deadline flow."""
from __future__ import annotations

from datetime import datetime

from coach.coach import generate_daily_suggestion
from db import MorningBriefState, get_session
from metrics.freshness import ERROR, morning_freshness
from notify.telegram import send_message
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
            return
        if facts["ready"] or row.answer_anyway:
            row.status = "evaluating"
            generate_daily_suggestion(session, allow_incomplete=row.answer_anyway)
            row.briefing_sent_at = _now_naive()
            row.status = "complete"
        else:
            row.status = "waiting"
        row.updated_at = _now_naive()


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
        send_message(text, reply_markup=markup)
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
