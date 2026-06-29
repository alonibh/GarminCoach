from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Any

from notify.telegram import send_message
from sync.scheduler import start_scheduler

logger = logging.getLogger(__name__)

def _send_reminder(time_str: str) -> None:
    msg = f"🔔 *Workout Reminder*\n\nYour session is scheduled for {time_str}. Time to start warming up!"
    try:
        send_message(msg)
    except Exception as e:
        logger.error(f"Failed to send pre-workout reminder: {e}")

def schedule_pre_workout_reminder(payload: dict[str, Any]) -> None:
    """Schedule a one-off pre-workout reminder for 1 hour before the suggested time."""
    time_str = payload.get("suggested_time")
    if not time_str:
        return
        
    try:
        hour, minute = map(int, time_str.split(":"))
        
        # If compiled in the evening, the target date is tomorrow
        if datetime.now().hour >= 17:
            target_date = date.today() + timedelta(days=1)
        else:
            target_date = date.today()
            
        workout_time = datetime(
            target_date.year, target_date.month, target_date.day, hour, minute
        )
        
        reminder_time = workout_time - timedelta(hours=1)
        
        if reminder_time > datetime.now():
            sched = start_scheduler()
            if sched:
                sched.add_job(
                    _send_reminder,
                    "date",
                    run_date=reminder_time,
                    args=[time_str],
                    id=f"reminder_{target_date.isoformat()}_{time_str}",
                    replace_existing=True
                )
                logger.info(f"Scheduled pre-workout reminder for {reminder_time}")
    except Exception as e:
        logger.warning(f"Failed to schedule pre-workout reminder: {e}")
