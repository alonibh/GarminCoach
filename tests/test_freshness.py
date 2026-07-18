from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager

from db import DailyHealth, DailyMetrics, Sleep, SyncState
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


def test_recompute_suppresses_today_readiness_until_fresh(session, monkeypatch):
    import metrics.engine as engine

    today = date(2026, 7, 4)
    monkeypatch.setattr(engine, "get_local_date", lambda: today)
    monkeypatch.setattr(engine, "compute_readiness", lambda *args, **kwargs: 18)
    monkeypatch.setattr("metrics.freshness.proactive_metrics_ready", lambda session: False)
    session.add(DailyHealth(day=today, resting_hr=70))
    session.add(Sleep(day=today, total_s=7 * 3600, score=80))
    session.commit()

    engine.recompute_daily_metrics(session)

    assert session.get(DailyMetrics, today).readiness is None


@contextmanager
def _bound_session(session):
    yield session


def test_dashboard_hides_readiness_when_today_is_unready(session, monkeypatch):
    import app as app_module

    today = date.today()
    yesterday = today - timedelta(days=1)
    session.add(DailyMetrics(day=yesterday, readiness=72, acute_load=10, chronic_load=10, acwr=1.0))
    session.add(DailyMetrics(day=today, readiness=18, acute_load=5, chronic_load=10, acwr=0.5))
    session.commit()
    monkeypatch.setattr(app_module, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(app_module, "_overnight_metrics_ready", lambda session: False)

    readiness, acwr = app_module._readiness_tiles()

    assert readiness["value"] is None
    assert readiness["age"] is None
    assert readiness["desc"] == "Waiting for today's Garmin Training Readiness."
    assert acwr["value"] == 0.5


def test_dashboard_replaces_unsupported_readiness_with_separate_recovery_signals(session, monkeypatch):
    import app as app_module

    today = date.today()
    freshness.note_capability_from_device(
        session,
        {
            "lastUsedDeviceApplicationKey": "vivoactive5",
            "lastUsedDeviceName": "v\ufffdvoactive 5",
        },
    )
    session.add(Sleep(day=today, total_s=8 * 3600 + 15 * 60, score=91, sleep_stress_avg=18))
    session.add(DailyHealth(
        day=today,
        hrv_overnight=61,
        hrv_baseline_low=57,
        hrv_baseline_high=66,
        resting_hr=49,
        stress_avg=18,
    ))
    for offset in range(1, 9):
        session.add(DailyHealth(day=today - timedelta(days=offset), resting_hr=52))
    for signal in (
        freshness.SLEEP,
        freshness.SLEEP_SCORE,
        freshness.HRV,
        freshness.RESTING_HR,
        freshness.STRESS,
    ):
        freshness.record_signal(session, signal, today, freshness.FRESH, "test")
    session.commit()
    monkeypatch.setattr(app_module, "get_session", lambda: _bound_session(session))

    readiness, _ = app_module._readiness_tiles()
    rendered = app_module.templates.get_template("dashboard.html").render(
        needs_login=False,
        last_sync_at=None,
        device_last_upload=None,
        sync_running=False,
        sync_summary=None,
        fitness_tiles=[],
        readiness_tiles=[readiness],
        health_series=[],
        sleep_series=[],
        activities=[],
    )

    assert readiness["key"] == "recovery_signals"
    assert readiness["signal_rows"] == [
        {"label": "Sleep", "value": "8h 15m · score 91 (Excellent)", "indicator": "Excellent", "tone": "positive"},
        {"label": "HRV", "value": "61 ms · within 57–66 baseline", "indicator": "Within baseline", "tone": "positive"},
        {"label": "Resting HR", "value": "49 bpm · 3 bpm below 28-day median", "indicator": "Below median", "tone": "comparison"},
        {"label": "Sleep stress", "value": "18 · Garmin resting range", "indicator": "Resting Range", "tone": "positive"},
    ]
    assert "Recovery signals" in rendered
    assert "Separate observations; not a combined readiness score." not in rendered
    assert "61 ms · within 57–66 baseline" in rendered
    assert 'class="recovery-signal-indicator positive"' in rendered
    assert "Within baseline" in rendered
    assert 'class="recovery-signal-indicator comparison"' in rendered
    assert "No fallback readiness score is invented." not in rendered
    assert "No data yet" not in rendered
    assert "Waiting for today" not in rendered


def test_dashboard_uses_proven_synced_raw_recovery_facts_without_freshness_rows(session, monkeypatch):
    import app as app_module

    today = date.today()
    freshness.note_capability_from_device(
        session,
        {"lastUsedDeviceApplicationKey": "vivoactive5"},
    )
    session.add(Sleep(day=today, total_s=7.5 * 3600, score=86, sleep_stress_avg=20))
    session.add(DailyHealth(
        day=today,
        hrv_overnight=55,
        hrv_baseline_low=50,
        hrv_baseline_high=60,
        resting_hr=51,
        stress_avg=22,
    ))
    synced_at = datetime.now(timezone.utc).replace(microsecond=0)
    _state(session, "device_last_upload", synced_at.isoformat())
    _state(session, "last_sync_at", (synced_at + timedelta(minutes=1)).isoformat())
    session.commit()
    monkeypatch.setattr(app_module, "get_session", lambda: _bound_session(session))

    recovery, _ = app_module._readiness_tiles()

    assert recovery["key"] == "recovery_signals"
    assert recovery["signal_rows"][0] == {
        "label": "Sleep", "value": "7h 30m · score 86 (Good)",
        "indicator": "Good", "tone": "positive",
    }
    assert recovery["signal_rows"][1] == {
        "label": "HRV", "value": "55 ms · within 50–60 baseline",
        "indicator": "Within baseline", "tone": "positive",
    }
    assert all(row["label"] != "Recovery time" for row in recovery["signal_rows"])


def test_dashboard_sleep_chart_does_not_turn_missing_today_into_zero(monkeypatch):
    import app as app_module

    today = date.today()
    rows = [
        Sleep(day=today - timedelta(days=1), total_s=7 * 3600, score=80),
        Sleep(day=today, total_s=None, score=None),
    ]

    series = app_module._dashboard_sleep_series(rows, overnight_ready=False)

    assert series[-2]["hours"] == 7.0
    assert series[-1]["hours"] is None


def test_dashboard_sleep_and_hrv_hide_today_until_overnight_ready(monkeypatch):
    import app as app_module

    today = date.today()
    sleep_rows = [Sleep(day=today, total_s=6 * 3600, score=75)]
    health_rows = [
        DailyHealth(
            day=today,
            hrv_overnight=42,
            hrv_baseline_low=40,
            hrv_baseline_high=60,
            steps=1200,
        )
    ]

    sleep_series = app_module._dashboard_sleep_series(sleep_rows, overnight_ready=False)
    health_series = app_module._dashboard_health_series(health_rows, overnight_ready=False)

    assert sleep_series[0]["hours"] is None
    assert health_series[0]["hrv"] is None
    assert health_series[0]["hrv_baseline_low"] is None
    assert health_series[0]["steps"] == 1200
