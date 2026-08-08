import pytest
from datetime import date
from fastapi.testclient import TestClient

import app as app_module
import metrics.freshness as freshness_module


@pytest.fixture
def client(monkeypatch):
    import config
    monkeypatch.setattr(config, "APP_USERNAME", "", raising=False)
    import sync.scheduler as scheduler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: None)
    from control_db import User
    monkeypatch.setattr("app.resolve_web_session", lambda s, token: User(id="00000000-0000-0000-0000-000000000001", email="test@example.com", status="active", role="owner", onboarding_step="complete"))
    c = TestClient(app_module.app)
    c.cookies.set("gc_session", "testuser")
    return c, app_module.app


def test_removed_sections_do_not_appear_on_dashboard(client, monkeypatch):
    """Proves that neither section heading appears on the dashboard,
    the surrounding dashboard sections still render, and the dashboard route
    no longer builds either removed report.
    """
    c, _ = client

    # Spy on report builder functions to verify the dashboard route does NOT call them.
    trend_report_called = False
    slow_report_called = False

    def spy_trend_report(*args, **kwargs):
        nonlocal trend_report_called
        trend_report_called = True

    def spy_slow_report(*args, **kwargs):
        nonlocal slow_report_called
        slow_report_called = True

    import metrics.recovery_trends as rt_mod
    import metrics.slow_metric_history as sm_mod
    monkeypatch.setattr(rt_mod, "build_recovery_health_trend_report", spy_trend_report)
    monkeypatch.setattr(sm_mod, "build_slow_metric_history_report", spy_slow_report)

    response = c.get("/")
    assert response.status_code == 200

    # 1. Neither section heading appears
    assert "28-day recovery and health trends" not in response.text
    assert "Long-term fitness history" not in response.text

    # 2. Surrounding dashboard sections still render
    assert "Today" in response.text
    assert "Your charts" in response.text
    assert "Garmin insights" in response.text or "Body metrics" in response.text

    # 3. Dashboard route no longer builds either removed report
    assert not trend_report_called, "Dashboard route should not call build_recovery_health_trend_report"
    assert not slow_report_called, "Dashboard route should not call build_slow_metric_history_report"


def test_recovery_time_signal_row_logic(monkeypatch):
    """Recovery Time row is hidden when Connect capability is unsupported/unverified,
    shown when stored value exists, and shows 'Not available today' when supported without data.
    """
    class MockQuery:
        def filter(self, *args, **kwargs):
            return self
        def order_by(self, *args, **kwargs):
            return self
        def first(self):
            return None
        def all(self):
            return []

    class MockSession:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def get(self, model, key):
            return None
        def query(self, model):
            return MockQuery()

    monkeypatch.setattr(app_module, "get_session", lambda: MockSession())
    monkeypatch.setattr(app_module, "get_local_date", lambda: date(2026, 8, 8))

    # Case 1: recovery_time_connect capability is unsupported -> Recovery Time row hidden.
    monkeypatch.setattr(freshness_module, "capability_state", lambda session, metric=None: "unsupported")
    tiles = app_module._readiness_tiles()
    signals_tile = next((t for t in tiles if t.get("key") == "recovery_signals"), None)
    assert signals_tile is not None
    labels = [r["label"] for r in signals_tile["signal_rows"]]
    assert "Recovery Time" not in labels

    # Case 2: recovery_time_connect capability is supported -> Recovery Time row present with 'Not available today'.
    monkeypatch.setattr(
        freshness_module,
        "capability_state",
        lambda session, metric=None: "supported" if metric == "recovery_time_connect" else "unsupported",
    )
    tiles = app_module._readiness_tiles()
    signals_tile = next((t for t in tiles if t.get("key") == "recovery_signals"), None)
    labels = [r["label"] for r in signals_tile["signal_rows"]]
    assert "Recovery Time" in labels
    rec_row = next(r for r in signals_tile["signal_rows"] if r["label"] == "Recovery Time")
    assert rec_row["value"] == "Not available today"
    assert rec_row["indicator"] == "No data"


def test_stress_and_body_battery_graphs_removed(client):
    """Proves Stress and Body Battery canvas/charts are absent, remaining charts render,
    and health_series no longer exposes the removed chart-only fields."""
    c, _ = client
    response = c.get("/")
    assert response.status_code == 200

    # Removed chart canvases and JS are absent
    assert 'id="stressChart"' not in response.text
    assert 'id="bodyBatteryChart"' not in response.text
    assert "lineChart('stressChart'" not in response.text
    assert "bodyBatteryChart(" not in response.text

    # Remaining charts still render
    assert 'id="rhrChart"' in response.text
    assert 'id="hrvChart"' in response.text
    assert 'id="sleepChart"' in response.text
    assert 'id="stepsChart"' in response.text
    assert 'id="caloriesChart"' in response.text
    assert "lineChart('rhrChart'" in response.text
    assert "lineChart('hrvChart'" in response.text

    # Verify removed fields absent and retained fields present by calling the function directly.
    from datetime import date as _date
    from db import DailyHealth
    row = DailyHealth(day=_date(2026, 8, 1))
    series = app_module._dashboard_health_series([row], overnight_ready=True, as_of_day=_date(2026, 8, 8))
    assert len(series) == 1
    entry = series[0]
    # Removed chart-only fields must not appear
    assert "stress" not in entry
    assert "bb_high" not in entry
    assert "bb_low" not in entry
    assert "bb_charged" not in entry
    assert "bb_drained" not in entry
    # Retained series fields still present
    assert "rhr" in entry
    assert "hrv" in entry
    assert "steps" in entry
    assert "active_kcal" in entry
    assert "bmr_kcal" in entry
