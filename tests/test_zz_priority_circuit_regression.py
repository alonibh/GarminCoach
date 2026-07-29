"""Isolation regression coverage for priority individual-signal circuit breaks."""
from datetime import date

import pytest
from garminconnect import GarminConnectTooManyRequestsError

from db import ObservationFreshness
from metrics import freshness
from sync import sync_service


def test_individual_rate_limit_preserves_unsupported_readiness(session, monkeypatch):
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    target = date(2026, 7, 4)
    freshness.record_signal(session, freshness.TRAINING_READINESS, target, freshness.UNSUPPORTED, "device_capability")
    calls = []
    monkeypatch.setattr(sync_service.client, "hrv", lambda _day: calls.append("hrv") or (_ for _ in ()).throw(GarminConnectTooManyRequestsError()))
    monkeypatch.setattr(sync_service.client, "resting_hr", lambda _day: calls.append("rhr"))
    monkeypatch.setattr(sync_service.client, "stress", lambda _day: calls.append("stress"))
    with pytest.raises(GarminConnectTooManyRequestsError):
        sync_service._priority_individual_health(session, target, None)
    assert calls == ["hrv"]
    assert session.get(ObservationFreshness, (freshness.TRAINING_READINESS, target)).state == freshness.UNSUPPORTED
    hrv = session.get(ObservationFreshness, (freshness.HRV, target))
    assert hrv.state == freshness.ERROR and hrv.error_code == "rate_limited"


def test_individual_rate_limit_preserves_unknown_readiness_and_completed_facts(session, monkeypatch):
    target = date(2026, 7, 4)
    freshness.record_signal(session, freshness.TRAINING_READINESS, target, freshness.MISSING, "get_training_readiness")
    calls = []
    monkeypatch.setattr(sync_service.client, "hrv", lambda _day: calls.append("hrv") or {"hrvSummary": {"lastNightAvg": 48}})
    monkeypatch.setattr(sync_service.client, "resting_hr", lambda _day: calls.append("rhr") or (_ for _ in ()).throw(GarminConnectTooManyRequestsError()))
    monkeypatch.setattr(sync_service.client, "stress", lambda _day: calls.append("stress"))
    with pytest.raises(GarminConnectTooManyRequestsError):
        sync_service._priority_individual_health(session, target, None)
    assert calls == ["hrv", "rhr"]
    assert session.get(ObservationFreshness, (freshness.TRAINING_READINESS, target)).state == freshness.MISSING
    assert session.get(ObservationFreshness, (freshness.HRV, target)).state == freshness.FRESH
    rhr = session.get(ObservationFreshness, (freshness.RESTING_HR, target))
    assert rhr.state == freshness.ERROR and rhr.error_code == "rate_limited"
