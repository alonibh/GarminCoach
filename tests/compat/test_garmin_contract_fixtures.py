from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from db import Activity, DailyHealth, Sleep
from sync import sync_service


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "garmin"
TARGET = date(2026, 7, 25)


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ContractClient:
    def __init__(self, training_readiness):
        self._training_readiness = training_readiness
        self.api = SimpleNamespace(
            get_activity=lambda _activity_id: {},
        )

    def sleep(self, _day):
        return _fixture("sleep.json")

    def hrv(self, _day):
        return _fixture("hrv.json")

    def resting_hr(self, _day):
        return {
            "allMetrics": {
                "metricsMap": {
                    "WELLNESS_RESTING_HEART_RATE": [{"value": 54}]
                }
            }
        }

    def stress(self, _day):
        return {"avgStressLevel": 24}

    def body_battery(self, _start, _end):
        return _fixture("body_battery.json")

    def daily_steps(self, _start, _end):
        stats = _fixture("daily_stats.json")
        return [{"totalSteps": stats["totalSteps"], "stepGoal": stats["dailyStepGoal"]}]

    def user_summary(self, _day):
        return _fixture("daily_stats.json")

    def training_readiness(self, _day):
        return self._training_readiness

    def training_status(self, _day):
        return {"mostRecentTrainingStatus": "PRODUCTIVE"}

    def hr_zones(self, _activity_id):
        return []


def _sync_health(session, monkeypatch, readiness_fixture: str) -> DailyHealth:
    monkeypatch.setattr(
        sync_service,
        "client",
        ContractClient(_fixture(readiness_fixture)),
    )
    sync_service._sync_daily_health(session, TARGET)
    session.flush()
    return session.get(DailyHealth, TARGET)


def test_daily_stats_hrv_and_body_battery_contracts_parse(session, monkeypatch):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_legacy_dict.json",
    )

    assert health.hrv_overnight == 46
    assert health.hrv_baseline_low == 38
    assert health.hrv_baseline_high == 58
    assert health.resting_hr == 54
    assert health.stress_avg == 24
    assert health.body_battery_low == 36
    assert health.body_battery_high == 74
    assert health.body_battery_current == 61
    assert health.steps == 6543
    assert health.step_goal == 10000
    assert health.total_kcal == 2180
    assert health.active_kcal == 480
    assert health.bmr_kcal == 1700


def test_sleep_contract_parses(session, monkeypatch):
    monkeypatch.setattr(
        sync_service,
        "client",
        ContractClient(_fixture("training_readiness_empty.json")),
    )

    assert sync_service._sync_sleep(session, TARGET) is True
    session.flush()
    sleep = session.get(Sleep, TARGET)
    assert sleep.total_s == 26100
    assert sleep.deep_s == 5400
    assert sleep.light_s == 13200
    assert sleep.rem_s == 6000
    assert sleep.awake_s == 1500
    assert sleep.score == 82


@pytest.mark.xfail(
    strict=True,
    reason="0.3.7 typed sleep uses avgRespirationValue; current parser expects averageRespirationValue",
)
def test_037_sleep_respiration_alias_requires_adapter_change(session, monkeypatch):
    monkeypatch.setattr(
        sync_service,
        "client",
        ContractClient(_fixture("training_readiness_empty.json")),
    )
    sync_service._sync_sleep(session, TARGET)
    session.flush()
    assert session.get(Sleep, TARGET).respiration_avg == 14.2


def test_activity_contract_parses(session, monkeypatch):
    monkeypatch.setattr(
        sync_service,
        "client",
        ContractClient(_fixture("training_readiness_empty.json")),
    )
    raw = _fixture("activities.json")[0]

    activity_id = sync_service._upsert_activity(session, raw)
    session.flush()
    activity = session.get(Activity, activity_id)

    assert activity.id == 987654321
    assert activity.activity_type == "strength_training"
    assert activity.name == "Synthetic Strength Session"
    assert activity.duration_s == 2700.0
    assert activity.avg_hr == 118
    assert activity.max_hr == 156
    assert activity.rpe == 6
    assert activity.feel == 3
    assert activity.source_workout_id == 24680


def test_legacy_training_readiness_dictionary_still_parses(session, monkeypatch):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_legacy_dict.json",
    )
    assert health.training_readiness == 64


def test_empty_training_readiness_response_remains_missing(session, monkeypatch):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_empty.json",
    )
    assert health.training_readiness is None


@pytest.mark.xfail(
    strict=True,
    reason="current production parser accepts dictionaries only; 0.3.7 returns snapshot lists",
)
def test_037_training_readiness_snapshot_list_requires_adapter_change(
    session,
    monkeypatch,
):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_list.json",
    )
    assert health.training_readiness == 71


@pytest.mark.xfail(
    strict=True,
    reason="current production parser cannot select the latest valid same-day snapshot",
)
def test_multiple_same_day_training_readiness_snapshots_require_selection_adapter(
    session,
    monkeypatch,
):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_multiple_same_day.json",
    )
    assert health.training_readiness == 82


def test_contract_fixtures_are_synthetic_and_contain_no_auth_material():
    forbidden_keys = {"email", "password", "token", "access_token", "refresh_token"}

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key.lower() not in forbidden_keys
                yield from walk(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from walk(nested)
        elif isinstance(value, str):
            assert "@" not in value

    for fixture in FIXTURES.glob("*.json"):
        list(walk(_fixture(fixture.name)))
