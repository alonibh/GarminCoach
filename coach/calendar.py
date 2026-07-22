import logging
import hashlib
import json
import urllib.request
from urllib.parse import urlsplit
from datetime import date, datetime, timedelta
import pytz

try:
    from icalevents.icalevents import events
except ImportError:
    events = None

import config

logger = logging.getLogger(__name__)


def validate_ics_url(url: str) -> str:
    """Allow only HTTPS calendar feeds hosted by Google or Apple."""
    parsed = urlsplit((url or "").strip())
    host = (parsed.hostname or "").casefold()
    allowed = host == "calendar.google.com" or host.endswith(".icloud.com")
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path
    ):
        raise ValueError("Use a private HTTPS Google Calendar or iCloud ICS URL")
    return parsed.geturl()


class _SafeCalendarRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_ics_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_calendar(url: str) -> bytes:
    safe_url = validate_ics_url(url)
    opener = urllib.request.build_opener(_SafeCalendarRedirect())
    request = urllib.request.Request(safe_url, headers={"User-Agent": "GarminCoach/1"})
    with opener.open(request, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "").casefold()
        if "text/calendar" not in content_type and "application/octet-stream" not in content_type:
            raise ValueError("Calendar server returned an unexpected content type")
        payload = response.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("Calendar feed is too large")
    return payload


def _configured_calendar_urls() -> list[str]:
    if not config.MULTI_USER_ENABLED:
        return [url.strip() for url in config.ICS_CALENDAR_URL.split(",") if url.strip()]
    from secret_vault import UserSecretVault
    from tenant_context import require_tenant
    value = UserSecretVault().read(require_tenant().user_id).get("calendar_ics_url", "")
    return [value] if value else []

def get_upcoming_schedule_result(days=3) -> dict:
    """Fetch events while preserving unconfigured/error states."""
    urls = _configured_calendar_urls()
    if not urls or events is None:
        return {"events": [], "state": "unconfigured", "error": None}
        
    schedule = []
    
    # Use the user's configured timezone so event times match their wall clock.
    try:
        from time_utils import get_local_tz
        local_tz = get_local_tz()
    except Exception:
        local_tz = pytz.utc
    
    try:
        # Fetch events from now until the end of the next few days
        start_time = datetime.now(pytz.utc)
        end_time = start_time + timedelta(days=days)
        
        for url in urls:
            if not url: continue
            
            # icalevents.events handles the timezone and RRULE expansion
            calendar_source = _download_calendar(url) if config.MULTI_USER_ENABLED else None
            cal_events = events(
                url=None if calendar_source is not None else url,
                string_content=calendar_source,
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
