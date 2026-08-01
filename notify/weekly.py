"""Saturday weekly-summary enqueue compatibility wrapper."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from db import get_session
from notify.outbox import enqueue_notification
from notify.weekly_report import build_weekly_summary_report, render_weekly_summary
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
    if overnight_today_ready is None:
        overnight_today_ready = week_end < generated_at.date()
    return render_weekly_summary(build_weekly_summary_report(
        session, week_end=week_end, generated_at=generated_at,
        overnight_today_ready=overnight_today_ready,
    ))


def send_weekly_summary() -> None:
    today = get_local_date()
    now = get_local_now().replace(tzinfo=None)
    with get_session() as session:
        enqueue_notification(
            session,
            event_type="weekly_summary",
            due_at=now,
            payload={"week_end": today.isoformat()},
            idempotency_key=f"weekly:{today.isoformat()}",
        )


if __name__ == "__main__":
    send_weekly_summary()
