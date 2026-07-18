"""Compatibility entry point for durable pre-workout reminders."""
from __future__ import annotations

from datetime import date
from typing import Any

from db import PlannedSession, get_session
from notify.outbox import enqueue_pre_workout_reminder


def schedule_pre_workout_reminder(payload: dict[str, Any]) -> None:
    time_text = payload.get("suggested_time")
    target_text = payload.get("target_date")
    if not time_text or not target_text:
        return
    try:
        target = date.fromisoformat(target_text)
    except ValueError:
        return
    with get_session() as session:
        query = session.query(PlannedSession).filter_by(
            target_date=target, suggested_time=time_text,
        ).filter(PlannedSession.status.notin_(("completed", "cancelled")))
        if payload.get("program_session_id"):
            query = query.filter_by(program_session_id=payload["program_session_id"])
        planned = query.order_by(PlannedSession.id.desc()).first()
        if planned:
            enqueue_pre_workout_reminder(session, planned)
