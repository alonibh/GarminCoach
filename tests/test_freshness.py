from datetime import date, datetime, timezone

from db import DailyHealth, Sleep, SyncState
from metrics import freshness


def _state(session, key, value):
    session.add(SyncState(key=key, value=value))


def test_freshness_requires_sleep(session, monkeypatch):
    monkeypatch.setattr(freshness, "get_local_date", lambda: date(2026, 7, 4))

    assert freshness.proactive_metrics_ready(session) is False


def test_freshness_rejects_watch_upload_before_sleep_end(session, monkeypatch):
    monkeypatch.setattr(freshness, "get_local_date", lambda: date(2026, 7, 4))
    session.add(
        Sleep(
            day=date(2026, 7, 4),
            total_s=7 * 3600,
            score=80,
            sleep_end_time=datetime(2026, 7, 4, 7, 0),
        )
    )
    session.add(DailyHealth(day=date(2026, 7, 4), hrv_overnight=50))
    _state(session, "device_last_upload", datetime(2026, 7, 4, 3, 0, tzinfo=timezone.utc).isoformat())
    _state(session, "last_sync_at", datetime(2026, 7, 4, 3, 5, tzinfo=timezone.utc).isoformat())
    session.commit()

    assert freshness.proactive_metrics_ready(session) is False


def test_freshness_allows_synced_sleep_and_recovery_signal(session, monkeypatch):
    monkeypatch.setattr(freshness, "get_local_date", lambda: date(2026, 7, 4))
    session.add(
        Sleep(
            day=date(2026, 7, 4),
            total_s=7 * 3600,
            score=80,
            sleep_end_time=datetime(2026, 7, 4, 7, 0),
        )
    )
    session.add(DailyHealth(day=date(2026, 7, 4), hrv_overnight=50))
    _state(session, "device_last_upload", datetime(2026, 7, 4, 7, 30, tzinfo=timezone.utc).isoformat())
    _state(session, "last_sync_at", datetime(2026, 7, 4, 7, 35, tzinfo=timezone.utc).isoformat())
    session.commit()

    assert freshness.proactive_metrics_ready(session) is True


def test_freshness_requires_sync_after_watch_upload(session, monkeypatch):
    monkeypatch.setattr(freshness, "get_local_date", lambda: date(2026, 7, 4))
    session.add(Sleep(day=date(2026, 7, 4), total_s=7 * 3600, score=80))
    _state(session, "device_last_upload", datetime(2026, 7, 4, 7, 30, tzinfo=timezone.utc).isoformat())
    _state(session, "last_sync_at", datetime(2026, 7, 4, 7, 0, tzinfo=timezone.utc).isoformat())
    session.commit()

    assert freshness.proactive_metrics_ready(session) is False
