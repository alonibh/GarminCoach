"""Capability-aware, per-signal freshness for morning decisions."""
from __future__ import annotations

from datetime import date, datetime, timezone
import re
import unicodedata

from sqlalchemy.orm import Session

from db import (
    DailyHealth,
    DeviceCapability,
    ObservationFreshness,
    Sleep,
    SyncState,
)
from time_utils import get_local_date, get_local_tz


TRAINING_READINESS = "training_readiness"
SLEEP = "sleep"
SLEEP_SCORE = "sleep_score"
HRV = "hrv"
RESTING_HR = "resting_hr"
STRESS = "stress"

FRESH = "fresh"
EXPECTED_PENDING = "expected_pending"
MISSING = "missing"
STALE = "stale"
UNSUPPORTED = "unsupported"
ERROR = "error"


_DEVICE_MODEL_FIELDS = {
    "deviceName",
    "deviceType",
    "displayName",
    # get_device_last_used() uses these two fields for the active watch.
    "lastUsedDeviceApplicationKey",
    "lastUsedDeviceName",
    "modelName",
    "productDisplayName",
    "productName",
}

# This registry contains only device models whose capability was verified
# against Garmin's official product comparison. Absence is always unknown.
_TRAINING_READINESS_UNSUPPORTED_MODELS = {
    "vivoactive_5": re.compile(r"\bvivoactive\s*5\b"),
}


def _normalized_device_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).split())


def _device_model_names(payload: object):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _DEVICE_MODEL_FIELDS and isinstance(value, str):
                yield value
            elif isinstance(value, (dict, list, tuple)):
                yield from _device_model_names(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _device_model_names(value)


def training_readiness_capability_from_device(payload: object) -> tuple[str, str] | None:
    """Resolve only officially verified models; never infer from an empty metric."""
    for raw_name in _device_model_names(payload):
        normalized = _normalized_device_name(raw_name)
        for model_key, pattern in _TRAINING_READINESS_UNSUPPORTED_MODELS.items():
            if pattern.search(normalized):
                return "unsupported", model_key
    return None


def note_capability_from_device(
    session: Session,
    payload: object,
    metric: str = TRAINING_READINESS,
    *,
    observed_at: datetime | None = None,
) -> DeviceCapability | None:
    resolved = training_readiness_capability_from_device(payload)
    if not resolved:
        return None
    state, model_key = resolved
    now = observed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.get(DeviceCapability, metric) or DeviceCapability(
        metric=metric,
        support_state=state,
        evidence_source=f"garmin_device_model:{model_key}",
        first_observed_at=now,
        updated_at=now,
    )
    row.support_state = state
    row.evidence_source = f"garmin_device_model:{model_key}"
    row.first_observed_at = row.first_observed_at or now
    row.last_observed_at = now
    row.updated_at = now
    session.add(row)
    return row


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
    return get_local_tz().localize(dt).astimezone(timezone.utc)


def _state_dt(session: Session, key: str) -> datetime | None:
    row = session.get(SyncState, key)
    return _parse_iso(row.value if row else None)


def capability_state(session: Session, metric: str = TRAINING_READINESS) -> str:
    row = session.get(DeviceCapability, metric)
    if not row:
        return "unknown"
    return row.override_state or row.support_state or "unknown"


def note_capability_observed(
    session: Session,
    metric: str = TRAINING_READINESS,
    *,
    observed_at: datetime | None = None,
    source: str = "garmin_observation",
) -> DeviceCapability:
    now = observed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.get(DeviceCapability, metric) or DeviceCapability(
        metric=metric,
        support_state="supported",
        evidence_source=source,
        first_observed_at=now,
        updated_at=now,
    )
    row.support_state = "supported"
    row.evidence_source = source
    row.first_observed_at = row.first_observed_at or now
    row.last_observed_at = now
    row.updated_at = now
    session.add(row)
    return row


def set_capability_override(session: Session, metric: str, state: str | None) -> None:
    if state not in {None, "supported", "unsupported"}:
        raise ValueError("Capability override must be supported, unsupported, or None")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.get(DeviceCapability, metric) or DeviceCapability(
        metric=metric,
        support_state="unknown",
        evidence_source="administrator_override",
        updated_at=now,
    )
    row.override_state = state
    row.evidence_source = "administrator_override" if state else row.evidence_source
    row.updated_at = now
    session.add(row)


def record_signal(
    session: Session,
    signal: str,
    observed_for: date,
    state: str,
    endpoint: str,
    *,
    fetched_at: datetime | None = None,
    device_upload_at: datetime | None = None,
    error_code: str | None = None,
    detail: str = "",
) -> ObservationFreshness:
    if state not in {FRESH, EXPECTED_PENDING, MISSING, STALE, UNSUPPORTED, ERROR}:
        raise ValueError(f"Unknown freshness state: {state}")
    now = fetched_at or datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.get(ObservationFreshness, (signal, observed_for)) or ObservationFreshness(
        signal=signal,
        observed_for=observed_for,
        state=state,
        fetched_at=now,
        source_endpoint=endpoint,
    )
    row.state = state
    row.fetched_at = now
    row.source_endpoint = endpoint
    row.device_upload_at = device_upload_at
    row.error_code = error_code
    row.detail = detail
    session.add(row)
    return row


def mark_priority_pending(session: Session, day: date | None = None) -> None:
    target = day or get_local_date()
    capability = capability_state(session)
    record_signal(session, SLEEP, target, EXPECTED_PENDING, "get_sleep_data")
    if capability != "unsupported":
        record_signal(session, TRAINING_READINESS, target, EXPECTED_PENDING, "get_training_readiness")


def morning_freshness(session: Session, day: date | None = None) -> dict:
    target = day or get_local_date()
    capability = capability_state(session)
    rows = {
        row.signal: row
        for row in session.query(ObservationFreshness).filter_by(observed_for=target).all()
    }
    critical = [SLEEP]
    if capability in {"supported", "unknown"}:
        critical.append(TRAINING_READINESS)
    missing_critical = [signal for signal in critical if not rows.get(signal) or rows[signal].state != FRESH]
    noncritical = [SLEEP_SCORE]
    if capability == "unsupported":
        noncritical.extend((HRV, RESTING_HR, STRESS))
    missing_noncritical = [signal for signal in noncritical if not rows.get(signal) or rows[signal].state != FRESH]
    return {
        "capability": capability,
        "states": {signal: row.state for signal, row in rows.items()},
        "critical_signals": critical,
        "missing_critical": missing_critical,
        "missing_noncritical": missing_noncritical,
        "ready": not missing_critical,
    }


def _legacy_metrics_ready(session: Session, day: date | None = None) -> bool:
    """Compatibility for data created before per-signal freshness existed."""
    today = day or get_local_date()
    sleep = session.get(Sleep, today)
    if not (sleep and sleep.total_s and sleep.total_s > 0):
        return False
    health = session.get(DailyHealth, today)
    has_recovery_signal = bool(
        sleep.score is not None
        or (health and any(value is not None for value in (
            health.training_readiness,
            health.hrv_overnight,
            health.resting_hr,
            health.body_battery_high,
            health.body_battery_current,
        )))
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


def synced_raw_metrics_ready(session: Session, day: date | None = None) -> bool:
    """Whether current raw facts are provably from a completed watch sync.

    This supports databases created by the full-sync path before per-signal
    freshness rows were introduced. An explicit per-signal state still takes
    precedence wherever one exists.
    """
    return _legacy_metrics_ready(session, day)


def proactive_metrics_ready(session: Session, day: date | None = None) -> bool:
    """True only when every capability-dependent critical signal is fresh."""
    today = day or get_local_date()
    has_rows = session.query(ObservationFreshness).filter_by(observed_for=today).first()
    if has_rows:
        return morning_freshness(session, today)["ready"]
    return _legacy_metrics_ready(session, today)
