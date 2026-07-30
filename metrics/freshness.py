"""Capability-aware, per-signal freshness for morning decisions."""
from __future__ import annotations

from datetime import date, datetime, timezone
import logging

from sqlalchemy.orm import Session

from db import (
    DailyHealth,
    MetricCapability,
    ObservationFreshness,
    Sleep,
    SyncState,
)
from time_utils import get_local_date, get_local_tz
from metrics.capability_registry import (
    CAPABILITY_KEYS,
    DEVICE_CAPABILITY_KEYS,
    GARMIN_CAPABILITY_REGISTRY_VERSION,
    CapabilityRef,
    DeviceIdentity,
    SOURCES,
    capability_ref_for,
    fetch_decision,
    registry_rule,
    resolve_device_identity,
    scope_kind_for,
)

logger = logging.getLogger(__name__)


TRAINING_READINESS = "training_readiness"
SLEEP = "sleep"
SLEEP_SCORE = "sleep_score"
HRV = "hrv"
HRV_STATUS = "hrv_status"
RECOVERY_TIME = "recovery_time"
RESTING_HR = "resting_hr"
STRESS = "stress"

FRESH = "fresh"
EXPECTED_PENDING = "expected_pending"
MISSING = "missing"
STALE = "stale"
UNSUPPORTED = "unsupported"
ERROR = "error"


def training_readiness_capability_from_device(payload: object) -> tuple[str, str] | None:
    identity = resolve_device_identity(payload)
    rule = registry_rule(identity.model_key if identity else None, TRAINING_READINESS)
    return (rule.state, identity.model_key) if rule and identity else None


def _identity_from_state(session: Session) -> DeviceIdentity | None:
    key = session.get(SyncState, "garmin_device_model_key")
    if not key or not key.value:
        return None
    return DeviceIdentity(
        key.value,
        (session.get(SyncState, "garmin_device_display_name") or SyncState(key="", value="")).value or key.value,
        (session.get(SyncState, "garmin_device_normalized_name") or SyncState(key="", value="")).value or "",
        "persisted",
    )


def _set_state(session: Session, key: str, value: str) -> None:
    row = session.get(SyncState, key) or SyncState(key=key)
    row.value = value
    session.add(row)


def capability_ref(
    session: Session,
    metric: str = TRAINING_READINESS,
    *,
    activity_domain: str | None = None,
    scope_kind: str | None = None,
    scope_key: str | None = None,
) -> CapabilityRef:
    """Resolve exactly one capability scope; there is never cross-scope fallback."""
    if (scope_kind is None) != (scope_key is None):
        raise ValueError("scope_kind and scope_key must be supplied together")
    if scope_kind is not None:
        expected = scope_kind_for(metric)
        if scope_kind != expected:
            raise ValueError(f"{metric} is {expected}-scoped, not {scope_kind}-scoped")
        return CapabilityRef(metric, scope_kind, scope_key)
    identity = _identity_from_state(session)
    return capability_ref_for(
        metric,
        device_model_key=identity.model_key if identity else None,
        activity_domain=activity_domain,
    )


def _capability_row(session: Session, ref: CapabilityRef) -> MetricCapability | None:
    return session.get(MetricCapability, ref.identity)


def _new_capability(ref: CapabilityRef, now: datetime, *, source: str = "unresolved") -> MetricCapability:
    return MetricCapability(
        metric=ref.metric,
        scope_kind=ref.scope_kind,
        scope_key=ref.scope_key,
        support_state="unknown",
        evidence_source=source,
        device_model_key=ref.scope_key if ref.scope_kind == "device" else None,
        updated_at=now,
    )


def note_capability_from_device(
    session: Session,
    payload: object,
    metric: str = TRAINING_READINESS,
    *,
    observed_at: datetime | None = None,
) -> MetricCapability | None:
    previous = _identity_from_state(session)
    identity = resolve_device_identity(payload, previous)
    if not identity:
        return None
    now = observed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    newly_detected = previous is None or previous.model_key != identity.model_key
    _set_state(session, "garmin_device_model_key", identity.model_key)
    _set_state(session, "garmin_device_display_name", identity.display_name)
    _set_state(session, "garmin_device_normalized_name", identity.normalized_name)
    _set_state(session, "garmin_capability_registry_version", GARMIN_CAPABILITY_REGISTRY_VERSION)
    _set_state(session, "garmin_device_last_seen_at", now.isoformat())
    for capability in DEVICE_CAPABILITY_KEYS:
        ref = capability_ref_for(capability, device_model_key=identity.model_key)
        row = _capability_row(session, ref) or _new_capability(ref, now)
        rule = registry_rule(identity.model_key, capability)
        # Current-device observations survive an idempotent same-model refresh.
        observed = row.support_state == "supported" and row.evidence_source == "garmin_observation"
        if rule and not observed:
            row.support_state = rule.state
            row.evidence_source = f"registry:{rule.source_id}"
            row.source_verified_on = SOURCES[rule.source_id].verified_on
        row.registry_version = GARMIN_CAPABILITY_REGISTRY_VERSION
        row.updated_at = now
        session.add(row)
    _set_state(session, "garmin_capability_newly_detected", "1" if newly_detected else "0")
    if scope_kind_for(metric) != "device":
        return None
    return _capability_row(session, capability_ref_for(metric, device_model_key=identity.model_key))


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


def capability_state(
    session: Session,
    metric: str = TRAINING_READINESS,
    *,
    activity_domain: str | None = None,
    scope_kind: str | None = None,
    scope_key: str | None = None,
) -> str:
    row = _capability_row(session, capability_ref(
        session, metric, activity_domain=activity_domain,
        scope_kind=scope_kind, scope_key=scope_key,
    ))
    if not row:
        return "unknown"
    return row.override_state or row.support_state or "unknown"


def note_capability_observed(
    session: Session,
    metric: str = TRAINING_READINESS,
    *,
    observed_at: datetime | None = None,
    source: str = "garmin_observation",
    activity_domain: str | None = None,
    scope_kind: str | None = None,
    scope_key: str | None = None,
) -> MetricCapability:
    now = observed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    ref = capability_ref(
        session, metric, activity_domain=activity_domain,
        scope_kind=scope_kind, scope_key=scope_key,
    )
    row = _capability_row(session, ref) or _new_capability(ref, now, source=source)
    contradicted_registry = row.support_state == "unsupported" and row.evidence_source.startswith("registry:")
    row.support_state = "supported"
    if contradicted_registry:
        logger.warning("Garmin capability observation contradicts registry: capability=%s scope=%s", metric, ref.scope_key)
    row.evidence_source = source
    row.source_verified_on = None
    if ref.scope_kind == "device":
        row.device_model_key = ref.scope_key
        row.registry_version = GARMIN_CAPABILITY_REGISTRY_VERSION
    row.first_observed_at = row.first_observed_at or now
    row.last_observed_at = now
    row.updated_at = now
    row.last_probe_at = now
    row.last_probe_outcome = "observed"
    session.add(row)
    return row


def note_capability_probe(
    session: Session, metric: str, outcome: str, *, observed_at: datetime | None = None,
    activity_domain: str | None = None, scope_kind: str | None = None, scope_key: str | None = None,
) -> None:
    if outcome not in {"observed", "empty", "ordinary_error", "authentication_error", "rate_limited"}:
        raise ValueError("Unknown capability probe outcome")
    now = observed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    ref = capability_ref(session, metric, activity_domain=activity_domain, scope_kind=scope_kind, scope_key=scope_key)
    row = _capability_row(session, ref) or _new_capability(ref, now)
    if outcome in {"observed", "empty", "ordinary_error"}:
        row.last_probe_at = now
    row.last_probe_outcome, row.updated_at = outcome, now
    session.add(row)


def capability_fetch_decision(
    session: Session, metric: str, context: str, *, activity_domain: str | None = None,
    scope_kind: str | None = None, scope_key: str | None = None,
) -> str:
    ref = capability_ref(session, metric, activity_domain=activity_domain, scope_kind=scope_kind, scope_key=scope_key)
    row = _capability_row(session, ref)
    return fetch_decision(
        capability_state(session, metric, activity_domain=activity_domain, scope_kind=scope_kind, scope_key=scope_key), last_probe_at=row.last_probe_at if row else None,
        newly_detected=False, context=context, capability=metric,
        interval_days=__import__("config").CAPABILITY_PROBE_INTERVAL_DAYS,
    )


def capability_diagnostics(session: Session) -> dict:
    identity = _identity_from_state(session)
    rows = []
    for row in session.query(MetricCapability).order_by(
        MetricCapability.metric, MetricCapability.scope_kind, MetricCapability.scope_key
    ):
        rows.append({"key": row.metric, "scope_kind": row.scope_kind, "scope_key": row.scope_key,
                     "current_device": bool(identity and row.scope_kind == "device" and row.scope_key == identity.model_key),
                     "state": row.support_state, "effective_state": row.override_state or row.support_state or "unknown",
                     "evidence_source": row.evidence_source, "source_verified_on": row.source_verified_on.isoformat() if row.source_verified_on else None,
                     "last_probe_at": row.last_probe_at.isoformat() if row.last_probe_at else None,
                     "last_probe_outcome": row.last_probe_outcome, "overridden": bool(row.override_state)})
    return {"device": None if identity is None else {"model_key": identity.model_key, "display_name": identity.display_name}, "registry_version": GARMIN_CAPABILITY_REGISTRY_VERSION, "capabilities": rows}


def set_capability_override(
    session: Session, metric: str, state: str | None, *, activity_domain: str | None = None,
    scope_kind: str | None = None, scope_key: str | None = None,
) -> None:
    if state not in {None, "supported", "unsupported"}:
        raise ValueError("Capability override must be supported, unsupported, or None")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ref = capability_ref(session, metric, activity_domain=activity_domain, scope_kind=scope_kind, scope_key=scope_key)
    row = _capability_row(session, ref) or _new_capability(ref, now)
    # Old rows used administrator_override as their only evidence marker.
    # Once the override is cleared, expose no invented historical evidence.
    if row.evidence_source == "administrator_override":
        row.evidence_source = "unresolved"
        row.support_state = "unknown"
        row.source_verified_on = None
    row.override_state = state
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
    # Unknown is a bounded probe state, not permanent morning-decision authority.
    if capability == "supported":
        critical.append(TRAINING_READINESS)
    missing_critical = [signal for signal in critical if not rows.get(signal) or rows[signal].state != FRESH]
    noncritical = [SLEEP_SCORE, HRV_STATUS, RECOVERY_TIME]
    if capability in {"unsupported", "unknown"}:
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
