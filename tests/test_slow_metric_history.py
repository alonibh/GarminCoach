from datetime import date, datetime

from db import MetricCapability, SlowMetricObservation, SyncState
from metrics.slow_metric_history import (
    RecordObservationOutcome,
    build_slow_metric_history_report,
    record_numeric_observation,
    record_text_observation,
)


NOW = datetime(2026, 7, 31, 9)


def test_writer_is_scoped_forward_only_and_idempotent(session):
    first = record_numeric_observation(
        session, metric="vo2max", scope_kind="activity", scope_key="running",
        observed_on=date(2026, 7, 20), observed_at=datetime(2026, 7, 20, 8), value=47.2,
        source_kind="test", source_key="activity:1", created_at=NOW,
    )
    assert first.outcome == RecordObservationOutcome.RECORDED
    assert record_numeric_observation(
        session, metric="vo2max", scope_kind="activity", scope_key="running",
        observed_on=date(2026, 7, 20), observed_at=datetime(2026, 7, 20, 8), value=47.2,
        source_kind="test", source_key="activity:1", created_at=NOW,
    ).outcome == RecordObservationOutcome.DUPLICATE_SOURCE
    assert record_numeric_observation(
        session, metric="vo2max", scope_kind="activity", scope_key="cycling",
        observed_on=date(2026, 7, 21), observed_at=datetime(2026, 7, 21, 8), value=50.0,
        source_kind="test", source_key="activity:2", created_at=NOW,
    ).outcome == RecordObservationOutcome.RECORDED
    assert record_numeric_observation(
        session, metric="vo2max", scope_kind="activity", scope_key="running",
        observed_on=date(2026, 7, 19), observed_at=None, value=46.0,
        source_kind="test", source_key="activity:old", created_at=NOW,
    ).outcome == RecordObservationOutcome.OLDER_THAN_HEAD
    assert session.query(SlowMetricObservation).count() == 2


def test_writer_rejects_invalid_and_status_is_device_scoped(session):
    assert record_numeric_observation(
        session, metric="fitness_age", scope_kind="account", scope_key="account",
        observed_on=date(2026, 7, 20), observed_at=None, value=True,
        source_kind="test", source_key="one", created_at=NOW,
    ).outcome == RecordObservationOutcome.INVALID
    assert record_text_observation(
        session, metric="training_status", scope_kind="device", scope_key="watch_a",
        observed_on=date(2026, 7, 20), observed_at=None, value=" Productive ",
        source_kind="test", source_key="one", created_at=NOW,
    ).outcome == RecordObservationOutcome.RECORDED
    assert record_text_observation(
        session, metric="training_status", scope_kind="device", scope_key="watch_a",
        observed_on=date(2026, 7, 20), observed_at=None, value="bad\nstatus",
        source_kind="test", source_key="two", created_at=NOW,
    ).outcome == RecordObservationOutcome.INVALID


def test_report_keeps_domains_and_old_device_history_separate(session):
    for scope, value, key in (("running", 47.0, "run"), ("cycling", 52.0, "cycle"), ("legacy_unverified", 44.0, "legacy")):
        record_numeric_observation(session, metric="vo2max", scope_kind="activity", scope_key=scope,
                                   observed_on=date(2026, 7, 20), observed_at=None, value=value,
                                   source_kind="test", source_key=key, created_at=NOW)
    session.add(SyncState(key="garmin_device_model_key", value="watch_new"))
    session.add(SyncState(key="garmin_device_display_name", value="Watch New"))
    session.add(MetricCapability(metric="training_status", scope_kind="device", scope_key="watch_new",
                                 support_state="supported", evidence_source="test", updated_at=NOW))
    record_text_observation(session, metric="training_status", scope_kind="device", scope_key="watch_old",
                            observed_on=date(2026, 7, 20), observed_at=None, value="Productive",
                            source_kind="test", source_key="old", created_at=NOW)
    session.flush()
    report = build_slow_metric_history_report(session, as_of_day=date(2026, 7, 31))
    assert report.vo2_running.current_value == 47.0
    assert report.vo2_cycling.current_value == 52.0
    assert report.vo2_legacy.legacy_unverified is True
    assert report.training_status.state == "SUPPORTED_NO_DATA"
