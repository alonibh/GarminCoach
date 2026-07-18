import logging
import os
import hashlib
import json
from datetime import date, datetime, timedelta
import pytz

try:
    from icalevents.icalevents import events
except ImportError:
    events = None

import config

logger = logging.getLogger(__name__)

def get_upcoming_schedule_result(days=3) -> dict:
    """Fetch events while preserving unconfigured/error states."""
    if not config.ICS_CALENDAR_URL or events is None:
        return {"events": [], "state": "unconfigured", "error": None}
        
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
        return {"events": schedule, "state": "fresh", "error": None}
        
    except Exception as e:
        logger.error(f"Failed to fetch calendar: {e}")
        return {"events": [], "state": "error", "error": type(e).__name__}


def get_upcoming_schedule(days=3) -> list[dict]:
    return get_upcoming_schedule_result(days)["events"]


def calendar_fingerprint(days: int = 14) -> tuple[str, str]:
    result = get_upcoming_schedule_result(days)
    raw = json.dumps(result["events"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32], result["state"]


def find_calendar_conflict(
    schedule: list[dict], target_date: date, start_time: str, duration_min: int
) -> dict | None:
    if not start_time:
        return None
    try:
        start = datetime.strptime(f"{target_date.isoformat()} {start_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    end = start + timedelta(minutes=duration_min)
    for item in schedule:
        try:
            event_start = datetime.strptime(item["start"], "%Y-%m-%d %H:%M")
            event_end_time = datetime.strptime(item["end"], "%H:%M").time()
            event_end = datetime.combine(event_start.date(), event_end_time)
            if event_end <= event_start:
                event_end += timedelta(days=1)
        except (KeyError, TypeError, ValueError):
            continue
        if start < event_end and event_start < end:
            return item
    return None
