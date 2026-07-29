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


GARMIN_CAPABILITY_REGISTRY_VERSION = "2026-07-29-v1"
CAPABILITY_KEYS = (
    "training_readiness", "training_status", "recovery_time_device",
    "recovery_time_connect", "hrv_status", "body_battery", "fitness_age", "vo2max",
)
SupportState = Literal["supported", "unsupported", "unknown"]
FetchDecision = Literal["fetch_supported", "probe_unknown", "skip_unsupported", "skip_unknown_not_due"]


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
    "recovery_time_connect": CapabilityRule("unsupported", "vivoactive_5_manual", "Recovery Time is device-only for this model."),
    "hrv_status": CapabilityRule("supported", "vivoactive_5_manual"),
    "body_battery": CapabilityRule("supported", "vivoactive_5_manual"),
    "vo2max": CapabilityRule("supported", "vivoactive_5_manual"),
})

MODEL_RULES = (
    ModelRule("vivoactive_5", "vívoactive 5", (re.compile(r"\bvivoactive\s*5\b"),), _VA5),
)

_DEVICE_FIELDS = (
    "lastUsedDeviceName", "productDisplayName", "displayName", "modelName", "productName",
    "deviceName", "deviceType", "lastUsedDeviceApplicationKey",
)


def normalize_device_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("™", " ").replace("®", " ")
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).split())


def _field_values(payload: object):
    if not isinstance(payload, dict):
        return
    for key in _DEVICE_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and normalize_device_name(value):
            yield key, value
    # Nested values are lower priority than direct current-watch fields.
    for value in payload.values():
        if isinstance(value, dict):
            yield from _field_values(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from _field_values(item)


def resolve_device_identity(payload: object, previous: DeviceIdentity | None = None) -> DeviceIdentity | None:
    unknown: DeviceIdentity | None = None
    for field, raw in _field_values(payload):
        normalized = normalize_device_name(raw)
        for rule in MODEL_RULES:
            if any(pattern.search(normalized) for pattern in rule.patterns):
                return DeviceIdentity(rule.model_key, rule.display_name, normalized, field)
        if unknown is None:
            # A usable but unlisted name gets a deterministic key and cannot match a family.
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            unknown = DeviceIdentity(f"unknown_{digest}", raw.strip(), normalized, field)
    return unknown or previous


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
    due = newly_detected or last_probe_at is None or now - last_probe_at >= timedelta(days=interval_days)
    return "probe_unknown" if due else "skip_unknown_not_due"
