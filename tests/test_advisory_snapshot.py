from datetime import datetime, timedelta, timezone
import json

import pytest

from coach.advisory_snapshot import PRIVACY_CONTRACT_VERSION, RECOVERY_METRICS, SNAPSHOT_VERSION, build_advisory_snapshot, serialize_advisory_snapshot
from db import Activity, DailyHealth, Sleep
from metrics import freshness


def test_v3_schema_is_compact_bounded_and_read_only(session):
    generated = datetime(2026, 8, 1, 20, tzinfo=timezone.utc)
    session.add_all([
        Sleep(day=generated.date(), total_s=7.5 * 3600, score=82),
        DailyHealth(day=generated.date(), hrv_overnight=65, resting_hr=54, body_battery_current=78, stress_avg=24),
        *[Activity(id=index + 1, activity_type="running", name=f"private title {index}", start_time=generated.replace(tzinfo=None) - timedelta(days=index), duration_s=1800, calories=100, avg_hr=145, training_load=42) for index in range(6)],
    ])
    session.commit()
    before = (len(session.new), len(session.dirty))
    snapshot = build_advisory_snapshot(session, generated_at=generated)
    assert list(snapshot) == ["snapshot_version", "privacy_contract_version", "generated_at", "timezone", "date_context", "official_recommendation", "data_freshness", "profile", "current_recovery", "training_aggregates", "recovery_trends_28_days", "slow_fitness_summary", "recent_activity_facts_7_days", "active_program", "planned_sessions_next_7_days"]
    assert snapshot["snapshot_version"] == SNAPSHOT_VERSION
    assert snapshot["privacy_contract_version"] == PRIVACY_CONTRACT_VERSION
    assert set(snapshot["current_recovery"]) == set(RECOVERY_METRICS)
    assert all(set(item) == {"value", "observed_at", "capability", "freshness"} for item in snapshot["current_recovery"].values())
    assert len(snapshot["recent_activity_facts_7_days"]["items"]) == 5
    rendered = serialize_advisory_snapshot(snapshot)
    assert len(rendered) <= 16_000
    assert "private title" not in rendered and "calories" not in rendered and "avg_hr" not in rendered
    for forbidden in ("calendar_next_7_days", "training_trends_6_weeks", "recent_activities_14_days", "acute_load", "chronic_load", "acwr"):
        assert forbidden not in rendered
    assert before == (len(session.new), len(session.dirty))


def test_generated_at_requires_aware_time_and_serialization_is_deterministic(session):
    with pytest.raises(ValueError):
        build_advisory_snapshot(session, generated_at=datetime(2026, 8, 1))
    snapshot = build_advisory_snapshot(session, generated_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert serialize_advisory_snapshot(snapshot) == serialize_advisory_snapshot(snapshot)
    assert json.loads(serialize_advisory_snapshot(snapshot))["generated_at"].endswith("Z")


def test_current_recovery_never_leaks_missing_or_error_values(session):
    today = datetime.now().date()
    session.add(DailyHealth(day=today, hrv_status="BALANCED", hrv_weekly_avg=45, recovery_time_minutes=120))
    freshness.record_signal(session, freshness.HRV_STATUS, today, freshness.MISSING, "fake")
    freshness.record_signal(session, freshness.RECOVERY_TIME, today, freshness.ERROR, "fake")
    session.commit()
    recovery = build_advisory_snapshot(session)["current_recovery"]
    assert recovery["garmin_hrv_status"]["value"] is None
    assert recovery["recovery_time_minutes"]["value"] is None
