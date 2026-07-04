from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from db import DailyHealth, Sleep, SyncState
from time_utils import get_local_date, get_local_tz


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _localize_sleep_time(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    local_tz = get_local_tz()
    return local_tz.localize(dt).astimezone(timezone.utc)


def _state_dt(session: Session, key: str) -> datetime | None:
    row = session.get(SyncState, key)
    return _parse_iso(row.value if row else None)


def proactive_metrics_ready(session: Session) -> bool:
    """True when today's morning brief can rely on synced overnight data."""
    today = get_local_date()
    sleep = session.get(Sleep, today)
    if not (sleep and sleep.total_s and sleep.total_s > 0):
        return False

    health = session.get(DailyHealth, today)
    has_recovery_signal = bool(
        sleep.score is not None
        or (
            health
            and (
                health.hrv_overnight is not None
                or health.resting_hr is not None
                or health.body_battery_high is not None
                or health.body_battery_current is not None
            )
        )
    )
    if not has_recovery_signal:
        return False

    device_upload = _state_dt(session, "device_last_upload")
    last_sync_at = _state_dt(session, "last_sync_at")
    if not device_upload or not last_sync_at or last_sync_at < device_upload:
        return False

    sleep_end = _localize_sleep_time(sleep.sleep_end_time)
    if sleep_end:
        return device_upload >= sleep_end

    return device_upload.astimezone(get_local_tz()).date() == today
