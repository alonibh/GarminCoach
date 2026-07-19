from contextlib import contextmanager
from datetime import date, datetime, timezone

from db import (
    CoachMessage,
    DeviceCapability,
    MorningBriefState,
    ObservationFreshness,
)
from metrics import freshness
from sync import sync_service


@contextmanager
def _bound_session(session):
    yield session
    session.commit()


def _device_upload(when):
    return {"lastUsedDeviceUploadTime": int(when.timestamp() * 1000)}


def _sleep_payload():
    return {
        "dailySleepDTO": {
            "sleepStartTimestampLocal": "2026-07-03 23:00:00",
            "sleepEndTimestampLocal": "2026-07-04 07:00:00",
            "sleepTimeSeconds": 7 * 3600,
            "sleepScores": {"overall": {"value": 82}},
        }
    }


def _wire_priority(monkeypatch, session):
    monkeypatch.setattr(sync_service, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr("time_utils.get_local_date", lambda: date(2026, 7, 4))
    monkeypatch.setattr(
        sync_service.client,
        "device_last_used",
        lambda: _device_upload(datetime(2026, 7, 4, 8, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(sync_service.client, "sleep", lambda _day: _sleep_payload())


def test_priority_sync_records_supported_readiness_and_per_signal_freshness(session, monkeypatch):
    _wire_priority(monkeypatch, session)
    monkeypatch.setattr(sync_service.client, "training_readiness", lambda _day: {"value": 74})

    result = sync_service.run_priority_sync()

    assert result["ready"] is True
    assert result["capability"] == "supported"
    assert session.get(DeviceCapability, freshness.TRAINING_READINESS).support_state == "supported"
    assert session.get(
        ObservationFreshness, (freshness.TRAINING_READINESS, date(2026, 7, 4))
    ).state == freshness.FRESH


def test_missing_readiness_does_not_turn_unknown_capability_into_unsupported(session, monkeypatch):
    _wire_priority(monkeypatch, session)
    monkeypatch.setattr(sync_service.client, "training_readiness", lambda _day: {})

    result = sync_service.run_priority_sync()

    assert result["ready"] is False
    assert result["capability"] == "unknown"
    assert session.get(DeviceCapability, freshness.TRAINING_READINESS) is None
    assert result["states"][freshness.TRAINING_READINESS] == freshness.MISSING


def test_vivoactive_5_device_identity_authoritatively_marks_readiness_unsupported(session, monkeypatch):
    _wire_priority(monkeypatch, session)
    monkeypatch.setattr(
        sync_service.client,
        "device_last_used",
        lambda: {
            **_device_upload(datetime(2026, 7, 4, 8, tzinfo=timezone.utc)),
            "productDisplayName": "vívoactive® 5",
        },
    )
    monkeypatch.setattr(
        sync_service.client,
        "training_readiness",
        lambda _day: (_ for _ in ()).throw(AssertionError("unsupported endpoint called")),
    )
    monkeypatch.setattr(sync_service.client, "hrv", lambda _day: {"hrvSummary": {"lastNightAvg": 48}})
    monkeypatch.setattr(
        sync_service.client,
        "resting_hr",
        lambda _day: {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 55}]}}},
    )
    monkeypatch.setattr(sync_service.client, "stress", lambda _day: {"avgStressLevel": 22})

    result = sync_service.run_priority_sync()

    capability = session.get(DeviceCapability, freshness.TRAINING_READINESS)
    assert result["capability"] == "unsupported"
    assert capability.support_state == "unsupported"
    assert capability.override_state is None
    assert capability.evidence_source == "garmin_device_model:vivoactive_5"
    assert result["states"][freshness.TRAINING_READINESS] == freshness.UNSUPPORTED


def test_real_garmin_last_used_payload_identifies_vivoactive_5(session, monkeypatch):
    _wire_priority(monkeypatch, session)
    monkeypatch.setattr(
        sync_service.client,
        "device_last_used",
        lambda: {
            **_device_upload(datetime(2026, 7, 4, 8, tzinfo=timezone.utc)),
            "lastUsedDeviceApplicationKey": "vivoactive5",
            "lastUsedDeviceName": "v\ufffdvoactive 5",
        },
    )
    monkeypatch.setattr(
        sync_service.client,
        "training_readiness",
        lambda _day: (_ for _ in ()).throw(AssertionError("unsupported endpoint called")),
    )
    monkeypatch.setattr(sync_service.client, "hrv", lambda _day: {"hrvSummary": {"lastNightAvg": 48}})
    monkeypatch.setattr(
        sync_service.client,
        "resting_hr",
        lambda _day: {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 55}]}}},
    )
    monkeypatch.setattr(sync_service.client, "stress", lambda _day: {"avgStressLevel": 22})

    result = sync_service.run_priority_sync()

    capability = session.get(DeviceCapability, freshness.TRAINING_READINESS)
    assert result["capability"] == "unsupported"
    assert capability.support_state == "unsupported"
    assert capability.evidence_source == "garmin_device_model:vivoactive_5"


def test_authoritative_unsupported_device_uses_individual_facts_without_score(session, monkeypatch):
    _wire_priority(monkeypatch, session)
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    session.commit()
    monkeypatch.setattr(sync_service.client, "training_readiness", lambda _day: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(sync_service.client, "hrv", lambda _day: {"hrvSummary": {"lastNightAvg": 48}})
    monkeypatch.setattr(
        sync_service.client,
        "resting_hr",
        lambda _day: {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 55}]}}},
    )
    monkeypatch.setattr(sync_service.client, "stress", lambda _day: {"avgStressLevel": 22})

    result = sync_service.run_priority_sync()

    assert result["ready"] is True
    assert result["capability"] == "unsupported"
    assert result["states"][freshness.TRAINING_READINESS] == freshness.UNSUPPORTED
    assert result["states"][freshness.HRV] == freshness.FRESH


def test_full_sync_records_today_individual_freshness_for_unsupported_watch(session):
    from db import DailyHealth, Sleep

    target = date(2026, 7, 4)
    freshness.note_capability_from_device(
        session,
        {"lastUsedDeviceApplicationKey": "vivoactive5"},
    )
    session.add(Sleep(day=target, total_s=8 * 3600, score=90))
    session.add(DailyHealth(day=target, hrv_overnight=52, resting_hr=50, stress_avg=20))
    session.commit()

    sync_service._record_full_sync_freshness(
        session,
        target,
        "2026-07-04T08:00:00+00:00",
    )
    session.commit()

    assert session.get(ObservationFreshness, (freshness.SLEEP, target)).state == freshness.FRESH
    assert session.get(ObservationFreshness, (freshness.SLEEP_SCORE, target)).state == freshness.FRESH
    assert session.get(ObservationFreshness, (freshness.TRAINING_READINESS, target)).state == freshness.UNSUPPORTED
    assert session.get(ObservationFreshness, (freshness.HRV, target)).state == freshness.FRESH
    assert session.get(ObservationFreshness, (freshness.RESTING_HR, target)).state == freshness.FRESH
    assert session.get(ObservationFreshness, (freshness.STRESS, target)).state == freshness.FRESH


def test_priority_endpoint_error_is_not_classified_as_missing(session, monkeypatch):
    _wire_priority(monkeypatch, session)
    monkeypatch.setattr(sync_service.client, "training_readiness", lambda _day: (_ for _ in ()).throw(TimeoutError()))

    result = sync_service.run_priority_sync()

    assert result["ready"] is False
    row = session.get(ObservationFreshness, (freshness.TRAINING_READINESS, date(2026, 7, 4)))
    assert row.state == freshness.ERROR
    assert row.error_code == "timeout"


def test_deadline_prompt_is_durable_and_not_duplicated(session, monkeypatch):
    import notify.morning as morning

    target = date(2026, 7, 4)
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(morning, "get_local_date", lambda: target)
    monkeypatch.setattr(morning, "get_local_now", lambda: datetime(2026, 7, 4, 11, 30))
    sent = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, reply_markup=None: sent.append((text, reply_markup)) or True,
    )
    freshness.record_signal(session, freshness.SLEEP, target, freshness.MISSING, "get_sleep_data")
    freshness.record_signal(
        session, freshness.TRAINING_READINESS, target, freshness.MISSING, "get_training_readiness"
    )
    session.commit()

    assert morning.morning_deadline() is True
    assert morning.morning_deadline() is False
    assert len(sent) == 1
    assert "Retry the Garmin fetch" in sent[0][0]
    assert sent[0][1]["inline_keyboard"][0][0]["text"] == "Retry Garmin fetch"
    assert session.get(MorningBriefState, target).status == "sync_required"


def test_deadline_reconciles_an_already_sent_morning_brief(session, monkeypatch):
    import notify.morning as morning

    target = date(2026, 7, 4)
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(morning, "get_local_date", lambda: target)
    monkeypatch.setattr(morning, "get_local_now", lambda: datetime(2026, 7, 4, 11, 30))
    session.add(CoachMessage(
        role="suggestion",
        content="Morning briefing already delivered.",
        created_at=datetime(2026, 7, 4, 10),
    ))
    session.commit()
    sent = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, reply_markup=None: sent.append((text, reply_markup)) or True,
    )

    assert morning.morning_deadline() is False
    state = session.get(MorningBriefState, target)
    assert state.status == "complete"
    assert state.briefing_sent_at == datetime(2026, 7, 4, 10)
    assert sent == []
