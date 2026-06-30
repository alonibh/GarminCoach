"""APScheduler-based auto-sync. Runs in-process with the FastAPI app."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from sync import sync_runner
from sync.garmin_client import client

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

    # Weekly summary (Sundays at 19:05)
    from notify.weekly import send_weekly_summary
    sched.add_job(
        send_weekly_summary,
        CronTrigger(day_of_week='sun', hour=19, minute=5),
        id="weekly_summary",
        replace_existing=True,
    )

    sched.start()
    _scheduler = sched
    return sched
