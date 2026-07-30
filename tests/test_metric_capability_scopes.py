from datetime import datetime, timedelta
import inspect as pyinspect

from sqlalchemy import create_engine, inspect, text

import db
import sync.sync_service as sync_service
from db import MetricCapability
from metrics import freshness
from metrics.capability_registry import (
    ACCOUNT_SCOPE_KEY,
    LEGACY_UNVERIFIED_ACTIVITY_SCOPE_KEY,
    SCALE_SCOPE_KEY,
)


def _row(session, metric, **kwargs):
    return session.get(MetricCapability, freshness.capability_ref(session, metric, **kwargs).identity)


def test_fresh_schema_allows_same_metric_in_distinct_scopes(session):
    session.add_all([
        MetricCapability(metric="training_readiness", scope_kind="device", scope_key="model_a", support_state="supported", evidence_source="test", updated_at=datetime(2026, 7, 1)),
        MetricCapability(metric="training_readiness", scope_kind="device", scope_key="model_b", support_state="unsupported", evidence_source="test", updated_at=datetime(2026, 7, 1)),
    ])
    session.commit()
    assert session.query(MetricCapability).count() == 2


def test_watch_change_keeps_account_activity_scale_and_old_device_evidence(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "Forerunner 265"})
    freshness.note_capability_observed(session, "training_readiness")
    freshness.note_capability_observed(session, "fitness_age")
    freshness.note_capability_observed(session, "vo2max", activity_domain="running")
    freshness.note_capability_probe(session, "body_composition", "empty")
    first = freshness.capability_ref(session).scope_key

    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "vivoactive 5"})
    assert freshness.capability_state(session) == "unsupported"
    assert freshness.capability_state(session, "fitness_age") == "supported"
    assert freshness.capability_state(session, "vo2max", activity_domain="running") == "supported"
    assert freshness.capability_state(session, "body_composition") == "unknown"
    assert session.get(MetricCapability, ("training_readiness", "device", first)).support_state == "supported"

    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "Forerunner 265"})
    assert freshness.capability_state(session) == "supported"


def test_scope_isolation_override_probe_and_activity_domains(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "Forerunner 265"})
    freshness.note_capability_observed(session, "training_readiness")
    freshness.note_capability_observed(session, "recovery_time_connect")
    freshness.note_capability_observed(session, "vo2max", activity_domain="running")
    freshness.note_capability_probe(session, "vo2max", "empty", activity_domain="cycling")
    freshness.set_capability_override(session, "vo2max", "unsupported", activity_domain="cycling")

    assert freshness.capability_state(session, "recovery_time_connect") == "supported"
    assert freshness.capability_state(session, "vo2max", activity_domain="running") == "supported"
    assert freshness.capability_state(session, "vo2max", activity_domain="cycling") == "unsupported"
    freshness.set_capability_override(session, "vo2max", None, activity_domain="cycling")
    assert freshness.capability_state(session, "vo2max", activity_domain="cycling") == "unknown"
    assert _row(session, "vo2max", activity_domain="cycling").last_probe_outcome == "empty"


def test_diagnostics_identify_scope_and_current_device_without_private_identity(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "Forerunner 265"})
    freshness.note_capability_observed(session, "fitness_age")
    diagnostics = freshness.capability_diagnostics(session)
    assert diagnostics["device"]["model_key"].startswith("unknown_")
    assert all({"scope_kind", "scope_key", "effective_state"} <= row.keys() for row in diagnostics["capabilities"])
    assert any(row["current_device"] for row in diagnostics["capabilities"] if row["scope_kind"] == "device")
    assert "normalized_name" not in diagnostics["device"]


def test_unknown_probe_cadence_is_per_scoped_row(session):
    freshness.note_capability_probe(session, "vo2max", "empty", activity_domain="running")
    assert freshness.capability_fetch_decision(session, "vo2max", "stage1", activity_domain="running") == "skip_unknown_not_due"
    assert freshness.capability_fetch_decision(session, "vo2max", "stage1", activity_domain="cycling") == "probe_unknown"
    assert freshness.capability_state(session, "body_composition") == "unknown"


def test_body_composition_remains_unprobed_and_has_no_sync_endpoint_call(session):
    freshness.note_capability_from_device(session, {"lastUsedDeviceName": "Forerunner 265"})
    assert freshness.capability_state(session, "body_composition") == "unknown"
    assert "body_composition" not in pyinspect.getsource(sync_service)


def test_legacy_capability_migration_preserves_rows_and_provenance(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    other_tables = [table for table in db.Base.metadata.sorted_tables if table.name != "device_capabilities"]
    db.Base.metadata.create_all(engine, tables=other_tables)
    observed = datetime(2026, 7, 2, 8)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE device_capabilities (
                metric VARCHAR(64) PRIMARY KEY, support_state VARCHAR(16), evidence_source VARCHAR(64),
                first_observed_at DATETIME, last_observed_at DATETIME, override_state VARCHAR(16),
                device_model_key VARCHAR(96), registry_version VARCHAR(32), source_verified_on DATE,
                last_probe_at DATETIME, last_probe_outcome VARCHAR(32), updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("INSERT INTO sync_state (key, value) VALUES ('garmin_device_model_key', 'current_model')"))
        values = [
            ("training_readiness", "supported", "garmin_observation", observed, observed, None, "old_model", "v1", "2026-07-01", observed, "observed", observed),
            ("fitness_age", "unsupported", "registry:legacy", None, None, "supported", None, "v1", None, observed, "empty", observed),
            ("recovery_time_connect", "unknown", "unresolved", None, None, None, None, "v1", None, observed, "authentication_error", observed),
            ("vo2max", "supported", "garmin_observation", observed, observed, None, None, "v1", None, observed, "observed", observed),
            ("body_composition", "unknown", "unresolved", None, None, None, None, "v1", None, None, None, observed),
        ]
        conn.execute(text("""
            INSERT INTO device_capabilities VALUES (
                :metric, :support_state, :evidence_source, :first_observed_at, :last_observed_at,
                :override_state, :device_model_key, :registry_version, :source_verified_on,
                :last_probe_at, :last_probe_outcome, :updated_at
            )
        """), [
            dict(zip(("metric", "support_state", "evidence_source", "first_observed_at", "last_observed_at", "override_state", "device_model_key", "registry_version", "source_verified_on", "last_probe_at", "last_probe_outcome", "updated_at"), value))
            for value in values
        ])

    db._migrate_add_columns(engine)
    with engine.begin() as conn:
        migrated = conn.execute(text("SELECT * FROM device_capabilities")).mappings().all()
        marker = conn.execute(text("SELECT 1 FROM app_migrations WHERE migration_key = :key"), {"key": db._CAPABILITY_SCOPE_MIGRATION_KEY}).first()
    assert len(migrated) == 5 and marker
    rows = {(row["metric"], row["scope_kind"]): row for row in migrated}
    assert rows[("training_readiness", "device")]["scope_key"] == "old_model"
    assert rows[("fitness_age", "account")]["scope_key"] == ACCOUNT_SCOPE_KEY
    assert rows[("recovery_time_connect", "account")]["last_probe_outcome"] == "authentication_error"
    assert rows[("vo2max", "activity")]["scope_key"] == LEGACY_UNVERIFIED_ACTIVITY_SCOPE_KEY
    assert rows[("body_composition", "scale")]["scope_key"] == SCALE_SCOPE_KEY
    assert rows[("training_readiness", "device")]["last_observed_at"] is not None
    assert set(inspect(engine).get_pk_constraint("device_capabilities")["constrained_columns"]) == {"metric", "scope_kind", "scope_key"}
    db._migrate_add_columns(engine)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM device_capabilities")).scalar() == 5
