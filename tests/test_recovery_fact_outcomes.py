from datetime import date, datetime, timezone
from contextlib import contextmanager

from db import DailyHealth, ObservationFreshness
from metrics import freshness
from sync import sync_service as svc


TARGET = date(2026, 7, 4)


def _outcome(session, signal):
    return session.get(ObservationFreshness, (signal, TARGET))


def test_full_sync_hrv_error_is_not_replaced_by_stored_value(session, monkeypatch):
    session.add(DailyHealth(day=TARGET, hrv_overnight=52, hrv_status="BALANCED"))
    monkeypatch.setattr(svc.client, "hrv", lambda _day: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(svc.client, "user_summary", lambda _day: {})

    assert svc._sync_daily_health_core(session, TARGET, current_optional=False)[0] is False
    svc._record_full_sync_freshness(session, TARGET, "2026-07-04T08:00:00+00:00")

    for signal in (freshness.HRV, freshness.HRV_STATUS):
        row = _outcome(session, signal)
        assert (row.state, row.error_code) == (freshness.ERROR, "timeout")


def test_valid_hrv_without_status_is_missing_status_and_replaces_metadata(session):
    row = DailyHealth(
        day=TARGET, hrv_status="OLD", hrv_weekly_avg=41,
        hrv_feedback_phrase="OLD_PHRASE",
    )
    normalized = svc.normalize_hrv_data({"hrvSummary": {
        "calendarDate": TARGET.isoformat(), "lastNightAvg": 51,
    }}, TARGET)
    assert svc._apply_normalized_hrv(session, row, normalized) is True
    session.flush()
    svc._record_full_sync_freshness(session, TARGET, None)
    assert _outcome(session, freshness.HRV).state == freshness.FRESH
    assert _outcome(session, freshness.HRV_STATUS).state == freshness.MISSING
    assert (row.hrv_weekly_avg, row.hrv_status, row.hrv_feedback_phrase) == (None, None, None)


def test_recovery_time_replaces_all_snapshot_metadata_atomically(session):
    row = DailyHealth(day=TARGET)
    first = {
        "calendarDate": TARGET.isoformat(), "timestamp": "2026-07-04T05:00:00Z",
        "trainingReadiness": 70, "recoveryTime": 120,
        "recoveryTimeChangePhrase": "REACHED_ZERO",
    }
    second = {
        "calendarDate": TARGET.isoformat(), "timestamp": "2026-07-04T06:00:00Z",
        "trainingReadiness": 70, "recoveryTime": 60,
    }
    assert svc._persist_recovery_time(session, row, first, fallback_observed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    assert svc._persist_recovery_time(session, row, second, fallback_observed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    assert (row.recovery_time_source_minutes, row.recovery_time_minutes, row.recovery_time_change_phrase) == (60, 60, None)

    zero_without_source = {**second, "timestamp": "2026-07-04T07:00:00Z", "recoveryTime": None, "recoveryTimeChangePhrase": "REACHED_ZERO"}
    assert svc._persist_recovery_time(session, row, zero_without_source, fallback_observed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    assert (row.recovery_time_source_minutes, row.recovery_time_minutes, row.recovery_time_change_phrase) == (None, 0, "REACHED_ZERO")


def test_recovery_time_missing_and_unsupported_are_distinct(session):
    row = DailyHealth(day=TARGET, recovery_time_minutes=120)
    assert not svc._persist_recovery_time(session, row, {"calendarDate": TARGET.isoformat(), "trainingReadiness": 70}, fallback_observed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    svc._record_full_sync_freshness(session, TARGET, None)
    assert _outcome(session, freshness.RECOVERY_TIME).state == freshness.MISSING

    freshness.set_capability_override(session, "recovery_time_connect", "unsupported")
    svc._record_full_sync_freshness(session, TARGET, None)
    assert _outcome(session, freshness.RECOVERY_TIME).state == freshness.UNSUPPORTED


def test_full_sync_readiness_failure_records_recovery_time_error(session, monkeypatch):
    freshness.note_capability_observed(session)
    freshness.set_capability_override(session, "training_status", "unsupported")
    monkeypatch.setattr(svc.client, "training_readiness", lambda _day: (_ for _ in ()).throw(TimeoutError()))
    svc._sync_current_optional_health(session, TARGET, context="full")
    svc._record_full_sync_freshness(session, TARGET, None)
    row = _outcome(session, freshness.RECOVERY_TIME)
    assert (row.state, row.error_code) == (freshness.ERROR, "timeout")


def test_supported_readiness_hero_shows_only_fresh_supporting_recovery_facts(session, monkeypatch):
    import app as app_module

    @contextmanager
    def bound_session():
        yield session

    freshness.note_capability_observed(session)
    session.add(DailyHealth(
        day=TARGET, training_readiness=74, hrv_overnight=48,
        hrv_status="BALANCED", hrv_weekly_avg=45, hrv_7d_coverage_days=6,
        recovery_time_minutes=0,
    ))
    for signal in (freshness.TRAINING_READINESS, freshness.HRV, freshness.HRV_STATUS, freshness.RECOVERY_TIME):
        freshness.record_signal(session, signal, TARGET, freshness.FRESH, "test")
    session.commit()
    monkeypatch.setattr(app_module, "get_session", bound_session)
    monkeypatch.setattr(app_module, "get_local_date", lambda: TARGET)

    readiness = app_module._readiness_tiles()[0]
    assert readiness["value"] == 74 and readiness["age"] == "Moderate"
    values = " ".join(row["value"] for row in readiness["supporting_signals"])
    assert "Garmin status: Balanced" in values
    assert "local data coverage 6/7 nights" in values
    assert "0 min remaining" in values

    freshness.record_signal(session, freshness.RECOVERY_TIME, TARGET, freshness.ERROR, "test", error_code="timeout")
    session.commit()
    assert all(row["label"] != "Recovery Time" for row in app_module._readiness_tiles()[0]["supporting_signals"])
