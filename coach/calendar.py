import logging
from datetime import date, datetime, timedelta
import pytz

try:
    from icalevents.icalevents import events
except ImportError:
    events = None

import config

logger = logging.getLogger(__name__)

def get_todays_schedule() -> list[dict]:
    """Fetch today's events from the configured ICS URL."""
    if not config.ICS_CALENDAR_URL or events is None:
        return []
        
    try:
        # Fetch events from now until the end of the day
        start_of_day = datetime.combine(date.today(), datetime.min.time())
        end_of_day = datetime.combine(date.today(), datetime.max.time())
        
        # icalevents.events handles the timezone and RRULE expansion
        cal_events = events(
            url=config.ICS_CALENDAR_URL,
            start=start_of_day,
            end=end_of_day
        )
        
        schedule = []
        for e in cal_events:
            # Skip all-day events if they don't block time
            if e.all_day:
                continue
                
            schedule.append({
                "title": e.summary,
                "start": e.start.astimezone().strftime("%H:%M"),
                "end": e.end.astimezone().strftime("%H:%M")
            })
            
        # Sort chronologically
        schedule.sort(key=lambda x: x["start"])
        return schedule
        
    except Exception as e:
        logger.error(f"Failed to fetch calendar: {e}")
        return []
