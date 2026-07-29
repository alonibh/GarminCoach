from datetime import date, datetime, timedelta

import config
from coach.advisory_snapshot import RECOVERY_METRICS, build_advisory_snapshot
from db import Activity, DailyHealth, DailyMetrics, Sleep, SyncState
from metrics import freshness


def test_snapshot_has_metric_and_bounded_section_wrappers(session, monkeypatch):
    today = datetime.now().date()
    session.add_all(
        [
            Sleep(day=today, total_s=7.5 * 3600, score=82),
            DailyHealth(
                day=today,
                hrv_overnight=65,
                hrv_baseline_low=45,
                hrv_baseline_high=85,
                resting_hr=54,
                body_battery_current=78,
                training_readiness=75,
                stress_avg=24,
            ),
            DailyMetrics(
                day=today,
                readiness=75,
                acute_load=320,
                chronic_load=290,
                acwr=1.1,
            ),
            SyncState(key="last_sync_at", value=datetime.now().isoformat()),
        ]
    )
    for index in range(3):
        session.add(
            Activity(
                id=index + 1,
                activity_type="running",
                name=f"Run {index}",
                start_time=datetime.now() - timedelta(days=index),
                duration_s=1800,
            )
        )
    session.commit()
    monkeypatch.setattr("config.ASK_COACH_MAX_RECENT_ACTIVITIES", 2)
    before_counts = {
        model: session.query(model).count()
        for model in (Activity, Sleep, DailyHealth, DailyMetrics)
    }
    assert not session.new and not session.dirty

    snapshot = build_advisory_snapshot(session)

    assert snapshot["snapshot_version"] == "ask-coach-v2"
    assert snapshot["generated_at"].endswith("Z")
    assert set(snapshot["recovery"]) == set(RECOVERY_METRICS)
    for wrapper in snapshot["recovery"].values():
        assert set(wrapper) == {"value", "observed_at", "status"}
        assert wrapper["status"] in {"available", "missing", "stale", "incomplete"}
    for section in (
        "planned_sessions",
        "calendar_next_7_days",
        "recent_activities_14_days",
    ):
        assert set(snapshot[section]) == {"items", "truncated", "omitted_count"}
    assert snapshot["recent_activities_14_days"]["truncated"] is True
    assert snapshot["recent_activities_14_days"]["omitted_count"] == 1
    assert before_counts == {
        model: session.query(model).count()
        for model in (Activity, Sleep, DailyHealth, DailyMetrics)
    }
    assert not session.new and not session.dirty
    assert '"id"' not in str(snapshot)


def test_snapshot_gates_recovery_facts_by_current_freshness_and_never_uses_legacy_readiness(session):
    today = datetime.now().date()
    session.add(DailyHealth(
        day=today, hrv_status="BALANCED", hrv_weekly_avg=45,
        recovery_time_minutes=120, recovery_time_change_phrase="REACHED_ZERO",
        training_readiness=None,
    ))
    session.add(DailyMetrics(day=today, readiness=75))
    freshness.record_signal(session, freshness.HRV_STATUS, today, freshness.MISSING, "get_hrv_data")
    freshness.record_signal(session, freshness.RECOVERY_TIME, today, freshness.ERROR, "get_training_readiness", error_code="timeout")
    session.commit()

    recovery = build_advisory_snapshot(session)["recovery"]
    assert recovery["garmin_hrv_status"]["value"] is None
    assert recovery["hrv_weekly_avg"]["value"] is None
    assert recovery["recovery_time_minutes"]["value"] is None
    assert recovery["recovery_time_change_phrase"]["value"] is None
    assert recovery["training_readiness"]["value"] is None

    freshness.record_signal(session, freshness.HRV_STATUS, today, freshness.FRESH, "get_hrv_data")
    freshness.record_signal(session, freshness.RECOVERY_TIME, today, freshness.FRESH, "get_training_readiness")
    session.commit()
    fresh = build_advisory_snapshot(session)["recovery"]
    assert fresh["garmin_hrv_status"]["value"] == "BALANCED"
    assert fresh["recovery_time_minutes"]["value"] == 120
