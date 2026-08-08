import pytest
from datetime import date
from types import SimpleNamespace

import app as app_module
import metrics.freshness as freshness_module
from metrics.slow_metric_history import (
    ScopedNumericHistory,
    SlowMetricHistoryReport,
    TrainingStatusHistory,
)


def test_recovery_time_hidden_for_unsupported_and_unverified_capability(monkeypatch):
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


def _render_dashboard(**kwargs):
    template = app_module.templates.get_template("dashboard.html")
    defaults = {
        "health_series": [],
        "sleep_series": [],
        "activities": [],
        "fitness_tiles": [],
        "readiness_tiles": [],
        "sync_running": False,
    }
    defaults.update(kwargs)
    return template.render(**defaults)


def test_training_status_rendered_only_for_supported_states():
    """Training Status card renders only for SUPPORTED_WITH_DATA and SUPPORTED_NO_DATA,
    and is hidden for UNSUPPORTED, NO_DEVICE_IDENTITY, and UNVERIFIED states.
    """
    def make_report(state, status="BUILDING"):
        return SlowMetricHistoryReport(
            as_of_day=date(2026, 8, 8),
            fitness_age=ScopedNumericHistory("fitness_age", "account", "account", None, None, (), "supported", False),
            target_fitness_age=ScopedNumericHistory("target_fitness_age", "account", "account", None, None, (), "supported", False),
            vo2_running=ScopedNumericHistory("vo2max", "activity", "running", None, None, (), "unsupported", False),
            vo2_cycling=ScopedNumericHistory("vo2max", "activity", "cycling", None, None, (), "unsupported", False),
            vo2_legacy=ScopedNumericHistory("vo2max", "device", "legacy", None, None, (), "unsupported", True),
            training_status=TrainingStatusHistory(
                state=state,
                device_scope_key="forerunner965" if state == "SUPPORTED_WITH_DATA" else None,
                device_display_name="Forerunner 965" if state == "SUPPORTED_WITH_DATA" else None,
                capability_state="supported" if "SUPPORTED" in state else "unsupported",
                current_status=status if state == "SUPPORTED_WITH_DATA" else None,
                current_day=date(2026, 8, 8) if state == "SUPPORTED_WITH_DATA" else None,
                changes=(),
            ),
        )

    # SUPPORTED_WITH_DATA -> rendered with status
    html = _render_dashboard(slow_metric_history=make_report("SUPPORTED_WITH_DATA"))
    assert "Training Status" in html
    assert "Garmin Training Status:" in html
    assert "BUILDING" in html

    # SUPPORTED_NO_DATA -> rendered with no data message
    html = _render_dashboard(slow_metric_history=make_report("SUPPORTED_NO_DATA"))
    assert "Training Status" in html
    assert "Training Status is supported for this device, but no current observation is stored." in html

    # UNSUPPORTED -> card completely hidden
    html = _render_dashboard(slow_metric_history=make_report("UNSUPPORTED"))
    assert "Training Status" not in html

    # NO_DEVICE_IDENTITY -> card completely hidden
    html = _render_dashboard(slow_metric_history=make_report("NO_DEVICE_IDENTITY"))
    assert "Training Status" not in html

    # UNVERIFIED -> card completely hidden
    html = _render_dashboard(slow_metric_history=make_report("UNVERIFIED"))
    assert "Training Status" not in html


def test_vo2_legacy_removed_and_empty_unsupported_cards_hidden():
    """Legacy VO2 max card is never rendered even with points;
    Running/Cycling VO2 cards render only when points exist or capability is supported.
    """
    report = SlowMetricHistoryReport(
        as_of_day=date(2026, 8, 8),
        fitness_age=ScopedNumericHistory("fitness_age", "account", "account", None, None, (), "unsupported", False),
        target_fitness_age=ScopedNumericHistory("target_fitness_age", "account", "account", None, None, (), "unsupported", False),
        vo2_running=ScopedNumericHistory("vo2max", "activity", "running", None, None, (), "unsupported", False),
        vo2_cycling=ScopedNumericHistory("vo2max", "activity", "cycling", None, None, (), "unsupported", False),
        vo2_legacy=ScopedNumericHistory(
            "vo2max", "device", "legacy", 48.5, None,
            (SimpleNamespace(observed_on=date(2026, 8, 1), value=48.5),),
            "unsupported", True,
        ),
        training_status=TrainingStatusHistory("UNSUPPORTED", None, None, "unsupported", None, None, ()),
    )

    html = _render_dashboard(slow_metric_history=report)
    assert "Legacy VO₂ max" not in html
    assert "activity type unverified" not in html
    assert "Running VO₂ max" not in html
    assert "Cycling VO₂ max" not in html
    # Entire section hidden because no slow cards meet display criteria
    assert "Long-term fitness history" not in html


def test_typed_vo2_and_supported_empty_cards_display():
    """Running/Cycling VO2 with valid typed points or supported capability display correctly."""
    report = SlowMetricHistoryReport(
        as_of_day=date(2026, 8, 8),
        fitness_age=ScopedNumericHistory("fitness_age", "account", "account", 30.0, None, (), "supported", False),
        target_fitness_age=ScopedNumericHistory("target_fitness_age", "account", "account", None, None, (), "supported", False),
        vo2_running=ScopedNumericHistory(
            "vo2max", "activity", "running", 52.0, None,
            (SimpleNamespace(observed_on=date(2026, 8, 1), value=52.0),),
            "supported", False,
        ),
        vo2_cycling=ScopedNumericHistory("vo2max", "activity", "cycling", None, None, (), "supported", False),
        vo2_legacy=ScopedNumericHistory("vo2max", "device", "legacy", None, None, (), "unsupported", True),
        training_status=TrainingStatusHistory("UNSUPPORTED", None, None, "unsupported", None, None, ()),
    )

    html = _render_dashboard(slow_metric_history=report)
    assert "Long-term fitness history" in html
    assert "Fitness Age" in html
    assert "Running VO₂ max" in html
    assert "52.0" in html
    assert "Cycling VO₂ max" in html
    assert "No local observation" in html
