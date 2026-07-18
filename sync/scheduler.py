"""APScheduler-based auto-sync. Runs in-process with the FastAPI app."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from sync import sync_runner
from sync.garmin_client import client
from db import CoachMessage, get_session

_scheduler: BackgroundScheduler | None = None


def _scheduled_sync() -> None:
    # Only auto-sync if we already have a valid cached session.
    if not client.is_authenticated():
        try:
            client.login()  # resume from cached token only
        except Exception:
            return
    # Go through the shared guard so a scheduled sync never collides with a
    # manual one (and vice versa).
    sync_runner.try_start_sync(full=False)


def _morning_brief_sent_today() -> bool:
    from time_utils import get_local_date

    today = get_local_date()
    with get_session() as session:
        recent = (
            session.query(CoachMessage)
            .filter_by(role="suggestion")
            .order_by(CoachMessage.created_at.desc())
            .limit(10)
            .all()
        )
        for msg in recent:
            if msg.created_at and msg.created_at.date() == today and msg.created_at.hour < 17:
                return True
    return False


def _maybe_send_ready_morning_brief() -> bool:
    from coach.coach import generate_daily_suggestion
    from metrics.freshness import proactive_metrics_ready

    if _morning_brief_sent_today():
        return True
    with get_session() as session:
        if not proactive_metrics_ready(session):
            return False
        generate_daily_suggestion(session)
        return True


def _morning_watch() -> None:
    """Poll lightly until the watch upload has produced usable overnight data."""
    if not client.is_authenticated():
        try:
            client.login()
        except Exception:
            return

    if _morning_brief_sent_today():
        return
    from notify.morning import start_priority_fetch
    start_priority_fetch()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    from time_utils import get_local_tz
    sched = BackgroundScheduler(daemon=True, timezone=get_local_tz())
    for t_str in config.AUTO_SYNC_TIMES:
        try:
            hour_str, minute_str = t_str.split(":")
            hour = int(hour_str)
            minute = int(minute_str)
            sched.add_job(
                _scheduled_sync,
                CronTrigger(hour=hour, minute=minute),
                id=f"autosync_{hour}_{minute}",
                replace_existing=True,
            )
        except ValueError:
            pass

    sched.add_job(
        _morning_watch,
        CronTrigger(
            hour=f"{config.MORNING_WATCH_START_HOUR}-{config.MORNING_WATCH_END_HOUR}",
            minute=f"*/{config.MORNING_WATCH_INTERVAL_MINUTES}",
        ),
        id="morning_watch",
        replace_existing=True,
    )

    from notify.morning import morning_deadline
    sched.add_job(
        morning_deadline,
        CronTrigger(hour=11, minute=30),
        id="morning_deadline",
        replace_existing=True,
    )

    # Deterministic weekly summary: Saturday 20:00 local.
    from notify.weekly import send_weekly_summary
    sched.add_job(
        send_weekly_summary,
        CronTrigger(day_of_week="sat", hour=20, minute=0),
        id="weekly_summary",
        replace_existing=True,
    )

    # Database-backed jobs survive restarts; this poller simply drains what is due.
    from notify.outbox import process_due_notifications
    sched.add_job(
        process_due_notifications,
        CronTrigger(minute="*", second=15),
        id="notification_outbox",
        replace_existing=True,
    )

    sched.start()
    _scheduler = sched
    return sched
