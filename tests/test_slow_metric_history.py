from datetime import date, datetime

from db import MetricCapability, SlowMetricObservation, SyncState
from metrics.slow_metric_history import (
    NumericObservationInput,
    RecordObservationOutcome,
    build_slow_metric_history_report,
    record_numeric_observation,
    record_numeric_observation_batch,
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


def test_numeric_batch_is_order_independent_and_uses_numeric_activity_ids(session):
    observations = [
        NumericObservationInput("vo2max", "activity", "running", date(2026, 7, 20), datetime(2026, 7, 20, 8), 55.0, "test", "activity:10:running:2026-07-20T08:00:00", NOW, 10),
        NumericObservationInput("vo2max", "activity", "running", date(2026, 7, 20), datetime(2026, 7, 20, 8), 44.0, "test", "activity:2:running:2026-07-20T08:00:00", NOW, 2),
    ]
    first = record_numeric_observation_batch(session, observations=reversed(observations), as_of_day=date(2026, 7, 31))
    assert [item.result.outcome for item in first.items] == [RecordObservationOutcome.RECORDED, RecordObservationOutcome.RECORDED]
    assert [row.numeric_value for row in session.query(SlowMetricObservation).order_by(SlowMetricObservation.source_key)] == [55.0, 44.0]
    assert record_numeric_observation_batch(session, observations=observations, as_of_day=date(2026, 7, 31)).items[-1].result.outcome == RecordObservationOutcome.DUPLICATE_SOURCE


def test_writer_rejects_datetime_future_aware_and_control_inputs(session):
    assert record_numeric_observation(
        session, metric="fitness_age", scope_kind="account", scope_key="account", observed_on=datetime(2026, 7, 20),
        observed_at=None, value=35, source_kind="test", source_key="one", created_at=NOW,
    ).outcome == RecordObservationOutcome.INVALID


def test_numeric_canonical_head_uses_integer_activity_id_not_source_text(session):
    def record(activity_id, value):
        return record_numeric_observation(
            session, metric="vo2max", scope_kind="activity", scope_key="running", observed_on=date(2026, 7, 20),
            observed_at=datetime(2026, 7, 20, 8), value=value, source_kind="test",
            source_key=f"activity:{activity_id}:running:2026-07-20T08:00:00", created_at=NOW,
        )
    assert record(2, 42).outcome == RecordObservationOutcome.RECORDED
    assert record(10, 50).outcome == RecordObservationOutcome.RECORDED
    assert record(5, 45).outcome == RecordObservationOutcome.OLDER_THAN_HEAD
    report = build_slow_metric_history_report(session, as_of_day=date(2026, 7, 31))
    assert report.vo2_running.current_value == 50


def test_status_uses_observation_time_not_fingerprint_order(session):
    session.add(SyncState(key="garmin_device_model_key", value="watch_a"))
    assert record_text_observation(
        session, metric="training_status", scope_kind="device", scope_key="watch_a", observed_on=date(2026, 7, 20),
        observed_at=datetime(2026, 7, 20, 9), value="Productive", source_kind="test", source_key="productive", created_at=NOW,
    ).outcome == RecordObservationOutcome.RECORDED
    assert record_text_observation(
        session, metric="training_status", scope_kind="device", scope_key="watch_a", observed_on=date(2026, 7, 20),
        observed_at=datetime(2026, 7, 20, 10), value="Recovery", source_kind="test", source_key="recovery", created_at=NOW,
    ).outcome == RecordObservationOutcome.RECORDED
    assert record_text_observation(
        session, metric="training_status", scope_kind="device", scope_key="watch_a", observed_on=date(2026, 7, 20),
        observed_at=datetime(2026, 7, 20, 8), value="Maintaining", source_kind="test", source_key="old", created_at=NOW,
    ).outcome == RecordObservationOutcome.OLDER_THAN_HEAD
    assert record_numeric_observation(
        session, metric="fitness_age", scope_kind="account", scope_key="account", observed_on=date(2026, 8, 1),
        observed_at=None, value=35, source_kind="test\n", source_key="one", created_at=NOW, as_of_day=date(2026, 7, 31),
    ).outcome == RecordObservationOutcome.INVALID
