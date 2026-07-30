"""Versioned, offline Garmin watch capability registry and policy helpers.

The registry intentionally contains only source-backed model rules.  A model
that is not represented here is *unknown*, never an inferred unsupported
device.  Source URLs are audit metadata; this module performs no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
import unicodedata
from types import MappingProxyType
from typing import Literal, Mapping, Pattern


GARMIN_CAPABILITY_REGISTRY_VERSION = "2026-07-30-v2"
CAPABILITY_KEYS = (
    "training_readiness", "training_status", "recovery_time_device",
    "recovery_time_connect", "hrv_status", "body_battery", "fitness_age", "vo2max",
    "body_composition",
)
SupportState = Literal["supported", "unsupported", "unknown"]
FetchDecision = Literal["fetch_supported", "probe_unknown", "skip_unsupported", "skip_unknown_not_due"]
ScopeKind = Literal["device", "account", "scale", "activity"]

ACCOUNT_SCOPE_KEY = "account"
SCALE_SCOPE_KEY = "scale"
UNKNOWN_DEVICE_SCOPE_KEY = "unknown_device"
LEGACY_UNVERIFIED_ACTIVITY_SCOPE_KEY = "legacy_unverified"


@dataclass(frozen=True)
class CapabilityRef:
    """The durable, non-PII identity of one metric capability."""

    metric: str
    scope_kind: ScopeKind
    scope_key: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.metric, self.scope_kind, self.scope_key)


# This is deliberately the only ownership mapping.  Call sites may supply an
# activity domain, but they never choose a scope kind for a metric themselves.
CAPABILITY_SCOPE_KINDS: Mapping[str, ScopeKind] = MappingProxyType({
    "training_readiness": "device",
    "training_status": "device",
    "recovery_time_device": "device",
    "recovery_time_connect": "account",
    "hrv_status": "device",
    "body_battery": "device",
    "fitness_age": "account",
    "vo2max": "activity",
    "body_composition": "scale",
})
DEVICE_CAPABILITY_KEYS = tuple(
    metric for metric in CAPABILITY_KEYS if CAPABILITY_SCOPE_KINDS[metric] == "device"
)


def scope_kind_for(metric: str) -> ScopeKind:
    try:
        return CAPABILITY_SCOPE_KINDS[metric]
    except KeyError as exc:
        raise ValueError(f"Unknown capability metric: {metric}") from exc


def normalize_activity_domain(value: object) -> str:
    """Return the narrow activity domain exposed by existing summaries."""
    normalized = normalize_device_name(value)
    if "run" in normalized:
        return "running"
    if "cycl" in normalized or "ride" in normalized or "bik" in normalized:
        return "cycling"
    return ""


def capability_ref_for(
    metric: str,
    *,
    device_model_key: str | None = None,
    activity_domain: str | None = None,
) -> CapabilityRef:
    kind = scope_kind_for(metric)
    if kind == "device":
        return CapabilityRef(metric, kind, device_model_key or UNKNOWN_DEVICE_SCOPE_KEY)
    if kind == "account":
        return CapabilityRef(metric, kind, ACCOUNT_SCOPE_KEY)
    if kind == "scale":
        return CapabilityRef(metric, kind, SCALE_SCOPE_KEY)
    domain = normalize_activity_domain(activity_domain)
    if not domain:
        raise ValueError(f"{metric} requires an explicit normalized activity domain")
    return CapabilityRef(metric, kind, domain)


def legacy_capability_ref(metric: str, device_model_key: str | None) -> CapabilityRef:
    """Map a metric-only legacy row without inventing activity evidence."""
    if scope_kind_for(metric) == "activity":
        return CapabilityRef(metric, "activity", LEGACY_UNVERIFIED_ACTIVITY_SCOPE_KEY)
    return capability_ref_for(metric, device_model_key=device_model_key)


@dataclass(frozen=True)
class RegistrySource:
    source_id: str
    title: str
    official_url: str
    verified_on: date
    exhaustive_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityRule:
    state: Literal["supported", "unsupported"]
    source_id: str
    note: str = ""


@dataclass(frozen=True)
class ModelRule:
    model_key: str
    display_name: str
    patterns: tuple[Pattern[str], ...]
    capabilities: Mapping[str, CapabilityRule]


@dataclass(frozen=True)
class DeviceIdentity:
    model_key: str
    display_name: str
    normalized_name: str
    matched_source_field: str


SOURCES = MappingProxyType({
    "training_status_faq": RegistrySource("training_status_faq", "Garmin Training Status FAQ VxKazDQ2mkAmDoQbJriEBA", "https://support.garmin.com/en-US/?faq=VxKazDQ2mkAmDoQbJriEBA", date(2026, 7, 29)),
    "recovery_time_faq": RegistrySource("recovery_time_faq", "Garmin Recovery Time FAQ 8ImmxVkZMh4EYYq5Zp2bR8", "https://support.garmin.com/en-US/?faq=8ImmxVkZMh4EYYq5Zp2bR8", date(2026, 7, 29)),
    "hrv_status_faq": RegistrySource("hrv_status_faq", "Garmin HRV Status FAQ HnFAR4oFRF4kHeqYme3bU6", "https://support.garmin.com/en-US/?faq=HnFAR4oFRF4kHeqYme3bU6", date(2026, 7, 29)),
    "body_battery_faq": RegistrySource("body_battery_faq", "Garmin Body Battery FAQ VOFJAsiXut9K19k1qEn5W5", "https://support.garmin.com/en-US/?faq=VOFJAsiXut9K19k1qEn5W5", date(2026, 7, 29)),
    "fitness_age_faq": RegistrySource("fitness_age_faq", "Garmin Fitness Age FAQ CM1YJmMrrNAbEpM9PapJ07", "https://support.garmin.com/en-US/?faq=CM1YJmMrrNAbEpM9PapJ07", date(2026, 7, 29)),
    "unified_training_status_faq": RegistrySource("unified_training_status_faq", "Garmin Unified Training Status FAQ EjPECQK58qA0xzJ5X74vm7", "https://support.garmin.com/en-US/?faq=EjPECQK58qA0xzJ5X74vm7", date(2026, 7, 29)),
    "vo2max_faq": RegistrySource("vo2max_faq", "Garmin VO2 max FAQ lWqSVlq3w76z5WoihLy5f8", "https://support.garmin.com/en-US/?faq=lWqSVlq3w76z5WoihLy5f8", date(2026, 7, 29)),
    "vivoactive_5_manual": RegistrySource("vivoactive_5_manual", "vivoactive 5 Owner's Manual", "https://www8.garmin.com/manuals/webhelp/GUID-5D183A14-BB43-4A9B-B441-5F824214CE40/EN-US/", date(2026, 7, 29)),
    "vivoactive_5_specs": RegistrySource("vivoactive_5_specs", "vivoactive 5 official specifications", "https://www.garmin.com/en-US/p/1057989/pn/010-02862-00/", date(2026, 7, 29)),
})

_VA5 = MappingProxyType({
    "training_readiness": CapabilityRule("unsupported", "vivoactive_5_specs", "Absent from the reviewed official Vivoactive 5 feature set."),
    "training_status": CapabilityRule("unsupported", "training_status_faq", "Official compatibility material reviewed for Vivoactive 5."),
    "recovery_time_device": CapabilityRule("supported", "vivoactive_5_manual"),
    "hrv_status": CapabilityRule("supported", "vivoactive_5_manual"),
    "body_battery": CapabilityRule("supported", "vivoactive_5_manual"),
})

MODEL_RULES = (
    ModelRule("vivoactive_5", "vívoactive 5", (re.compile(r"\bvivoactive\s*5\b"),), _VA5),
)

_HUMAN_DEVICE_FIELDS = (
    "lastUsedDeviceName", "productDisplayName", "displayName", "modelName", "productName",
    "deviceName", "deviceType",
)
_APPLICATION_KEY_FIELD = "lastUsedDeviceApplicationKey"


def normalize_device_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("™", " ").replace("®", " ")
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).split())
    # Garmin exposes both display names and compact application keys.  Make
    # their word boundaries equivalent without attempting broad family matches.
    return re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", normalized)


def _usable_device_value(value: object) -> bool:
    if not isinstance(value, str) or "\ufffd" in value:
        return False
    normalized = normalize_device_name(value)
    return bool(normalized and re.search(r"[a-z]", normalized) and normalized not in {
        "garmin", "garmin device", "device", "watch",
    })


def _nested_field_values(payload: object):
    """Yield nested identity candidates only after every direct tier failed."""
    if not isinstance(payload, dict):
        return
    for value in payload.values():
        children = value if isinstance(value, (list, tuple)) else (value,)
        for child in children:
            if not isinstance(child, dict):
                continue
            for key in (*_HUMAN_DEVICE_FIELDS, _APPLICATION_KEY_FIELD):
                raw = child.get(key)
                if _usable_device_value(raw):
                    yield key, raw
            yield from _nested_field_values(child)


def _identity_for(field: str, raw: str) -> DeviceIdentity:
    normalized = normalize_device_name(raw)
    for rule in MODEL_RULES:
        if any(pattern.search(normalized) for pattern in rule.patterns):
            return DeviceIdentity(rule.model_key, rule.display_name, normalized, field)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return DeviceIdentity(f"unknown_{digest}", raw.strip(), normalized, field)


def resolve_device_identity(payload: object, previous: DeviceIdentity | None = None) -> DeviceIdentity | None:
    if not isinstance(payload, dict):
        return previous
    # Tier 1 is deliberately terminal: a current human-readable watch name is
    # more trustworthy than stale application keys and unrelated nested devices.
    direct = [(field, payload[field]) for field in _HUMAN_DEVICE_FIELDS if _usable_device_value(payload.get(field))]
    if direct:
        for field, raw in direct:
            identity = _identity_for(field, raw)
            if not identity.model_key.startswith("unknown_"):
                return identity
        return _identity_for(*direct[0])
    raw_key = payload.get(_APPLICATION_KEY_FIELD)
    if _usable_device_value(raw_key):
        return _identity_for(_APPLICATION_KEY_FIELD, raw_key)
    for field, raw in _nested_field_values(payload):
        return _identity_for(field, raw)
    return previous


def model_rule(model_key: str | None) -> ModelRule | None:
    return next((rule for rule in MODEL_RULES if rule.model_key == model_key), None)


def registry_rule(model_key: str | None, capability: str) -> CapabilityRule | None:
    rule = model_rule(model_key)
    return rule.capabilities.get(capability) if rule else None


def fetch_decision(state: str, *, last_probe_at: datetime | None, newly_detected: bool, context: str, capability: str, interval_days: int) -> FetchDecision:
    if state == "supported":
        return "fetch_supported"
    if state == "unsupported":
        return "skip_unsupported"
    allowed = {"stage1", "scheduled", "full"}
    if context == "priority" and capability == "training_readiness":
        allowed.add("priority")
    if context not in allowed:
        return "skip_unknown_not_due"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # A model change clears probe timestamps.  A persistent "newly detected"
    # marker must not repeatedly bypass the interval for an already-probed row.
    due = last_probe_at is None or now - last_probe_at >= timedelta(days=interval_days)
    return "probe_unknown" if due else "skip_unknown_not_due"
