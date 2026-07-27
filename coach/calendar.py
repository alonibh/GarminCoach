import logging
import hashlib
import json
import secrets
import threading
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from datetime import date, datetime, timedelta
import pytz

try:
    from icalevents.icalevents import events
except ImportError:
    events = None

import config

logger = logging.getLogger(__name__)

# Ask Coach must not turn each question into up to five external calendar
# requests.  Interactive calendar loads refresh this small, per-tenant cache.
_schedule_cache: dict[str, tuple[float, dict]] = {}
_schedule_cache_lock = threading.Lock()
SCHEDULE_CACHE_TTL_SECONDS = 300


def _schedule_cache_key() -> str:
    from tenant_context import current_tenant

    tenant = current_tenant()
    return tenant.user_id if tenant is not None else "legacy"


def _store_schedule_cache(result: dict) -> None:
    with _schedule_cache_lock:
        _schedule_cache[_schedule_cache_key()] = (time.monotonic(), result.copy())


def get_cached_upcoming_schedule_result(
    days: int = 7, *, max_age_seconds: int = SCHEDULE_CACHE_TTL_SECONDS
) -> dict | None:
    """Return a bounded interactive-calendar result without external I/O."""
    with _schedule_cache_lock:
        cached = _schedule_cache.get(_schedule_cache_key())
    if cached is None or time.monotonic() - cached[0] > max_age_seconds:
        return None
    result = cached[1]
    # A shorter request can safely use this cache; callers still filter dates.
    return {"events": list(result.get("events", [])), "state": result.get("state"), "error": result.get("error")}


def validate_ics_url(url: str) -> str:
    """Allow only HTTPS calendar feeds hosted by Google or Apple."""
    parsed = urlsplit((url or "").strip())
    if parsed.scheme.casefold() == "webcal":
        parsed = parsed._replace(scheme="https")
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
    return urlunsplit(parsed)


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
    from tenant_context import current_tenant
    tenant = current_tenant()
    if tenant is not None:
        from secret_vault import UserSecretVault
        values = UserSecretVault().read(tenant.user_id)
        feeds = values.get("calendar_feeds")
        if isinstance(feeds, list):
            return [item["url"] for item in feeds if isinstance(item, dict) and item.get("url")]
        legacy = values.get("calendar_ics_url", "")
        return [legacy] if legacy else []
    return [url.strip() for url in (config.ICS_CALENDAR_URL or "").split(",") if url.strip()]


def test_calendar_url(url: str, *, days: int = 30) -> tuple[str, int]:
    """Normalize, fetch, and parse a feed before persisting its private URL."""
    normalized = validate_ics_url(url)
    payload = _download_calendar(normalized)
    start = datetime.now(pytz.utc)
    parsed = events(
        string_content=payload,
        start=start,
        end=start + timedelta(days=days),
    )
    return normalized, len(parsed)


def calendar_feed_record(url: str) -> dict[str, str]:
    host = (urlsplit(url).hostname or "").casefold()
    provider = "iCloud" if host.endswith(".icloud.com") else "Google Calendar"
    return {"id": secrets.token_hex(8), "provider": provider, "url": url}

def get_upcoming_schedule_result(days=3) -> dict:
    """Fetch events while preserving unconfigured/error states."""
    urls = _configured_calendar_urls()
    if not urls or events is None:
        result = {"events": [], "state": "unconfigured", "error": None}
        _store_schedule_cache(result)
        return result
        
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
                if e.all_day:
                    event_day = e.start.date() if isinstance(e.start, datetime) else e.start
                    schedule.append({
                        "title": e.summary,
                        "start": event_day.isoformat(),
                        "end": "",
                        "all_day": True,
                    })
                    continue
                event_end = e.end or e.start
                schedule.append({
                    "title": e.summary,
                    "start": e.start.astimezone(local_tz).strftime("%Y-%m-%d %H:%M"),
                    "end": event_end.astimezone(local_tz).strftime("%H:%M"),
                    "all_day": False,
                })
            
        # Sort chronologically across all combined calendars
        schedule.sort(key=lambda x: x["start"])
        result = {"events": schedule, "state": "fresh", "error": None}
        _store_schedule_cache(result)
        return result
        
    except Exception as exc:
        logger.error("Calendar fetch failed: %s", type(exc).__name__)
        result = {"events": [], "state": "error", "error": type(exc).__name__}
        _store_schedule_cache(result)
        return result


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
