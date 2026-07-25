"""APScheduler-based auto-sync. Runs in-process with the FastAPI app."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from control_db import User, get_control_session
from sync import sync_runner
from sync.garmin_client import client
from db import get_session
from tenant_context import TenantIdentity, tenant_scope

_scheduler: BackgroundScheduler | None = None


def _run_for_user(user_id: str) -> None:
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None or user.status != "active" or not user.garmin_connected:
            return
        identity = TenantIdentity(user.id, role=user.role, timezone=user.timezone)
    with tenant_scope(identity):
        try:
            authenticated = client.is_authenticated()
        except Exception:
            return
        if not authenticated:
            return
        sync_runner.try_start_sync(full=False)


def _run_user_notification_job(user_id: str, job: str) -> None:
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None or user.status != "active" or not user.telegram_linked:
            return
        identity = TenantIdentity(user.id, role=user.role, timezone=user.timezone)
    with tenant_scope(identity):
        if job == "morning_watch":
            _morning_watch()
        elif job == "morning_deadline":
            from notify.morning import morning_deadline
            morning_deadline()
        elif job == "weekly_summary":
            from notify.weekly import send_weekly_summary
            send_weekly_summary()
        elif job == "notification_outbox":
            from notify.outbox import process_due_notifications
            process_due_notifications()


def refresh_user_jobs(user_id: str) -> None:
    """Replace one athlete's jobs; safe to call after onboarding or deletion."""
    if _scheduler is None or not config.MULTI_USER_ENABLED:
        return
    prefix = f"user_{user_id}_"
    for job in _scheduler.get_jobs():
        if job.id.startswith(prefix):
            _scheduler.remove_job(job.id)
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None or user.status != "active" or not user.timezone:
            return
        timezone = user.timezone
    for index, time_string in enumerate(config.AUTO_SYNC_TIMES):
        try:
            hour, minute = (int(part) for part in time_string.split(":"))
        except (TypeError, ValueError):
            continue
        _scheduler.add_job(
            _run_for_user,
            CronTrigger(hour=hour, minute=minute, timezone=timezone),
            kwargs={"user_id": user_id},
            id=f"{prefix}sync_{index}",
            replace_existing=True,
        )
    with get_control_session() as session:
        refreshed_user = session.get(User, user_id)
        linked = bool(refreshed_user and refreshed_user.telegram_linked)
    if not linked:
        return
    notification_jobs = (
        (
            "morning_watch",
            CronTrigger(
                hour=f"{config.MORNING_WATCH_START_HOUR}-{config.MORNING_WATCH_END_HOUR}",
                minute=f"*/{config.MORNING_WATCH_INTERVAL_MINUTES}",
                timezone=timezone,
            ),
        ),
        ("morning_deadline", CronTrigger(hour=11, minute=30, timezone=timezone)),
        ("weekly_summary", CronTrigger(day_of_week="sat", hour=20, minute=0, timezone=timezone)),
        ("notification_outbox", CronTrigger(minute="*", second=15, timezone=timezone)),
    )
    for name, trigger in notification_jobs:
        _scheduler.add_job(
            _run_user_notification_job,
            trigger,
            kwargs={"user_id": user_id, "job": name},
            id=f"{prefix}{name}",
            replace_existing=True,
        )


def start_multi_user_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
        _scheduler.start()
    with get_control_session() as session:
        user_ids = [row.id for row in session.query(User).filter(User.status == "active")]
    for user_id in user_ids:
        refresh_user_jobs(user_id)
    return _scheduler


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
    from notify.morning import reconcile_sent_brief
    with get_session() as session:
        return reconcile_sent_brief(session)


def _maybe_send_ready_morning_brief() -> bool:
    from coach.coach import generate_daily_suggestion
    from metrics.freshness import proactive_metrics_ready

    if _morning_brief_sent_today():
        return True
    with get_session() as session:
        if not proactive_metrics_ready(session):
            return False
        return generate_daily_suggestion(session)


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


def stop_schedulers() -> None:
    """Stop the process-wide scheduler; safe before startup or after shutdown."""
    global _scheduler
    scheduler = _scheduler
    _scheduler = None
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
