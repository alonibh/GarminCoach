import logging
import os
from datetime import date, datetime, timedelta
import pytz

try:
    from icalevents.icalevents import events
except ImportError:
    events = None

import config

logger = logging.getLogger(__name__)

def get_upcoming_schedule(days=3) -> list[dict]:
    """Fetch upcoming events from the configured ICS URL(s)."""
    if not config.ICS_CALENDAR_URL or events is None:
        return []
        
    schedule = []
    
    # Use the user's configured timezone so event times match their wall clock.
    try:
        local_tz = pytz.timezone(os.getenv("USER_TIMEZONE", "Asia/Jerusalem"))
    except Exception:
        local_tz = pytz.utc
    
    # Split by comma to support multiple calendars
    urls = [url.strip() for url in config.ICS_CALENDAR_URL.split(',')]
    
    try:
        # Fetch events from now until the end of the next few days
        start_time = datetime.now(pytz.utc)
        end_time = start_time + timedelta(days=days)
        
        for url in urls:
            if not url: continue
            
            # icalevents.events handles the timezone and RRULE expansion
            cal_events = events(
                url=url,
                start=start_time,
                end=end_time
            )
            
            for e in cal_events:
                # Skip all-day events if they don't block time
                if e.all_day:
                    continue
                    
                schedule.append({
                    "title": e.summary,
                    "start": e.start.astimezone(local_tz).strftime("%Y-%m-%d %H:%M"),
                    "end": e.end.astimezone(local_tz).strftime("%H:%M")
                })
            
        # Sort chronologically across all combined calendars
        schedule.sort(key=lambda x: x["start"])
        return schedule
        
    except Exception as e:
        logger.error(f"Failed to fetch calendar: {e}")
        return []
