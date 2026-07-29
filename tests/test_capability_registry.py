from datetime import datetime, timedelta

from db import DeviceCapability
from metrics import freshness
from metrics.capability_registry import (
    CAPABILITY_KEYS,
    GARMIN_CAPABILITY_REGISTRY_VERSION,
    MODEL_RULES,
    SOURCES,
    fetch_decision,
    resolve_device_identity,
)


def test_registry_is_source_backed_and_strictly_named():
    assert GARMIN_CAPABILITY_REGISTRY_VERSION
    assert len(SOURCES) == len(set(SOURCES))
    assert all(source.official_url.startswith("https://") and source.verified_on for source in SOURCES.values())
    assert len({rule.model_key for rule in MODEL_RULES}) == len(MODEL_RULES)
    for model in MODEL_RULES:
        for key, capability in model.capabilities.items():
            assert key in CAPABILITY_KEYS
            assert capability.source_id in SOURCES


def test_vivoactive_identity_variants_and_similar_model_safety():
    for raw in ("vívoactive 5", "vivoactive 5", "vivoactive5", "VIVOACTIVE™ 5"):
        assert resolve_device_identity({"lastUsedDeviceName": raw}).model_key == "vivoactive_5"
    assert resolve_device_identity({"lastUsedDeviceApplicationKey": "vivoactive5"}).model_key == "vivoactive_5"
    assert resolve_device_identity({"lastUsedDeviceName": "vivoactive 6"}).model_key != "vivoactive_5"
    assert resolve_device_identity({"lastUsedDeviceName": "forerunner 255"}).model_key != "vivoactive_5"


def test_direct_identity_precedence_and_unknown_normalization():
    assert resolve_device_identity({
        "lastUsedDeviceName": "Forerunner 265", "lastUsedDeviceApplicationKey": "vivoactive5",
    }).model_key != "vivoactive_5"
    assert resolve_device_identity({
        "lastUsedDeviceName": "Mystery Watch X", "nested": {"modelName": "vivoactive 5"},
    }).display_name == "Mystery Watch X"
    assert resolve_device_identity({
        "lastUsedDeviceName": "v\ufffdvoactive 5", "lastUsedDeviceApplicationKey": "vivoactive5",
    }).model_key == "vivoactive_5"
    assert resolve_device_identity({"lastUsedDeviceName": "Forerunner265"}).model_key == resolve_device_identity(
        {"lastUsedDeviceName": "Forerunner 265"}
    ).model_key


def test_vivoactive_5_persists_all_registry_capabilities(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "vívoactive 5"})
    expected = {
        "training_readiness": "unsupported", "training_status": "unsupported",
        "recovery_time_device": "supported", "recovery_time_connect": "unsupported",
        "hrv_status": "supported", "body_battery": "supported", "vo2max": "supported",
        "fitness_age": "unknown",
    }
    assert {key: freshness.capability_state(session, key) for key in CAPABILITY_KEYS} == expected
    assert session.query(DeviceCapability).count() == len(CAPABILITY_KEYS)


def test_observation_and_override_survive_same_model_refresh_and_reset_on_change(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "vivoactive 5"})
    freshness.note_capability_observed(session, "training_readiness")
    freshness.set_capability_override(session, "fitness_age", "supported")
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "vivoactive5"})
    assert freshness.capability_state(session, "training_readiness") == "supported"
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "forerunner 255"})
    assert freshness.capability_state(session, "training_readiness") == "unknown"
    assert freshness.capability_state(session, "fitness_age") == "supported"


def test_model_change_resets_evidence_but_preserves_override(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "Forerunner 265"})
    freshness.note_capability_observed(session, "training_readiness")
    freshness.set_capability_override(session, "training_readiness", "unsupported")
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "vivoactive 5"})
    row = session.get(DeviceCapability, "training_readiness")
    assert row.support_state == "unsupported"  # new model registry evidence
    assert row.override_state == "unsupported"
    assert row.last_observed_at is None and row.source_verified_on is not None
    freshness.set_capability_override(session, "training_readiness", None)
    assert freshness.capability_state(session, "training_readiness") == "unsupported"


def test_unknown_probe_policy_is_bounded():
    now = datetime.now()
    assert fetch_decision("unknown", last_probe_at=None, newly_detected=False, context="stage1", capability="training_status", interval_days=7) == "probe_unknown"
    assert fetch_decision("unknown", last_probe_at=now, newly_detected=False, context="incremental", capability="training_status", interval_days=7) == "skip_unknown_not_due"
    assert fetch_decision("unknown", last_probe_at=now, newly_detected=False, context="priority", capability="training_readiness", interval_days=7) == "skip_unknown_not_due"
    assert fetch_decision("unknown", last_probe_at=now - timedelta(days=8), newly_detected=False, context="priority", capability="training_readiness", interval_days=7) == "probe_unknown"
    assert fetch_decision("unsupported", last_probe_at=None, newly_detected=True, context="stage1", capability="training_status", interval_days=7) == "skip_unsupported"


def test_auth_and_rate_limit_probe_outcomes_preserve_cadence(session):
    observed = datetime(2026, 7, 1, 8)
    freshness.note_capability_probe(session, "training_status", "empty", observed_at=observed)
    row = session.get(DeviceCapability, "training_status")
    freshness.note_capability_probe(session, "training_status", "authentication_error", observed_at=observed + timedelta(days=8))
    assert row.last_probe_at == observed and row.last_probe_outcome == "authentication_error"
    freshness.note_capability_probe(session, "training_status", "rate_limited", observed_at=observed + timedelta(days=9))
    assert row.last_probe_at == observed and row.last_probe_outcome == "rate_limited"
