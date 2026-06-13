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
    sched = BackgroundScheduler(daemon=True)
    for hour in config.AUTO_SYNC_HOURS:
        sched.add_job(
            _scheduled_sync,
            CronTrigger(hour=hour, minute=0),
            id=f"autosync_{hour}",
            replace_existing=True,
        )
    sched.start()
    _scheduler = sched
    return sched
