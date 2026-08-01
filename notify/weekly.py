"""Saturday weekly-summary enqueue compatibility wrapper."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from db import get_session
from notify.outbox import enqueue_notification
from notify.weekly_report import (
    WeeklySummaryValidationError, build_weekly_summary_report,
    render_weekly_summary, validate_week_end, weekly_overnight_ready,
)
from time_utils import get_local_date, get_local_now


def build_weekly_summary(
    session: Session,
    week_end: date,
    *,
    generated_at: datetime | None = None,
    overnight_today_ready: bool | None = None,
) -> str:
    """Compatibility API for callers that require only the rendered text."""
    generated_at = (generated_at or get_local_now()).replace(tzinfo=None)
    week_end = validate_week_end(week_end, local_day=generated_at.date())
    if overnight_today_ready is None:
        overnight_today_ready = weekly_overnight_ready(
            session, week_end=week_end, local_delivery_day=generated_at.date(),
        )
    return render_weekly_summary(build_weekly_summary_report(
        session, week_end=week_end, generated_at=generated_at,
        overnight_today_ready=overnight_today_ready,
    ))


def send_weekly_summary() -> bool:
    today = get_local_date()
    now = get_local_now().replace(tzinfo=None)
    try:
        validate_week_end(today, local_day=today)
    except WeeklySummaryValidationError:
        return False
    with get_session() as session:
        enqueue_notification(
            session,
            event_type="weekly_summary",
            due_at=now,
            payload={"week_end": today.isoformat()},
            idempotency_key=f"weekly:{today.isoformat()}",
        )
    return True


if __name__ == "__main__":
    send_weekly_summary()
