from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from db import Activity, DailyHealth, Sleep
from sync.garmin_client import normalize_training_readiness
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
    assert health.body_battery_current is None
    assert health.steps == 6543
    assert health.step_goal == 10000
    assert health.total_kcal == 2180
    assert health.active_kcal == 480
    assert health.bmr_kcal == 1700


def test_daily_summary_parser_uses_key_presence_and_preserves_nulls():
    parsed = sync_service._parse_daily_summary({
        "restingHeartRate": None, "averageStressLevel": 0,
        "totalSteps": 0, "dailyStepGoal": None,
        "bodyBatteryHighestValue": 0, "bodyBatteryLowestValue": None,
    })
    assert parsed is not None
    values, families = parsed
    assert families == {"resting_hr", "stress", "steps", "body_battery"}
    assert values == {"stress_avg": 0, "steps": 0, "body_battery_high": 0}


def test_daily_summary_parser_rejects_non_finite_and_boolean_values():
    parsed = sync_service._parse_daily_summary({
        "restingHeartRate": True,
        "averageStressLevel": float("nan"),
        "totalSteps": float("inf"),
        "dailyStepGoal": float("-inf"),
        "bodyBatteryHighestValue": "bad",
    })
    assert parsed is not None
    values, families = parsed
    assert families == {"resting_hr", "stress", "steps", "body_battery"}
    assert values == {}


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


def test_037_sleep_respiration_alias_parses(session, monkeypatch):
    monkeypatch.setattr(
        sync_service,
        "client",
        ContractClient(_fixture("training_readiness_empty.json")),
    )
    sync_service._sync_sleep(session, TARGET)
    session.flush()
    assert session.get(Sleep, TARGET).respiration_avg == 14.2


def test_legacy_sleep_respiration_alias_still_parses(session, monkeypatch):
    payload = _fixture("sleep.json")
    dto = payload["dailySleepDTO"]
    dto["averageRespirationValue"] = dto.pop("avgRespirationValue")
    contract_client = ContractClient(_fixture("training_readiness_empty.json"))
    contract_client.sleep = lambda _day: payload
    monkeypatch.setattr(sync_service, "client", contract_client)

    sync_service._sync_sleep(session, TARGET)
    session.flush()

    assert session.get(Sleep, TARGET).respiration_avg == 14.2


def test_legacy_sleep_respiration_alias_still_parses(session, monkeypatch):
    payload = _fixture("sleep.json")
    dto = payload["dailySleepDTO"]
    dto["averageRespirationValue"] = dto.pop("avgRespirationValue")
    contract_client = ContractClient(_fixture("training_readiness_empty.json"))
    contract_client.sleep = lambda _day: payload
    monkeypatch.setattr(sync_service, "client", contract_client)

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


def test_037_training_readiness_snapshot_list_parses(
    session,
    monkeypatch,
):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_list.json",
    )
    assert health.training_readiness == 71


def test_multiple_same_day_training_readiness_snapshots_select_latest(
    session,
    monkeypatch,
):
    health = _sync_health(
        session,
        monkeypatch,
        "training_readiness_multiple_same_day.json",
    )
    assert health.training_readiness == 82


def test_training_readiness_normalizer_preserves_supporting_fields():
    normalized = normalize_training_readiness(
        _fixture("training_readiness_list.json"),
        TARGET,
    )

    assert normalized is not None
    assert normalized["trainingReadiness"] == 71
    assert normalized["recoveryTime"] == 120
    assert normalized["level"] == "MODERATE"


def test_training_readiness_normalizer_rejects_malformed_and_off_date_entries():
    normalized = normalize_training_readiness(
        [
            None,
            "not-a-snapshot",
            {"calendarDate": TARGET.isoformat(), "timestamp": "invalid", "score": 99},
            {
                "calendarDate": "2026-07-24",
                "timestamp": "2026-07-24T23:59:00Z",
                "score": 100,
            },
            {
                "calendarDate": TARGET.isoformat(),
                "timestamp": "2026-07-25T05:00:00Z",
                "score": "not-a-score",
            },
            {
                "calendarDate": TARGET.isoformat(),
                "timestamp": "2026-07-25T06:00:00Z",
                "score": 73,
                "recoveryTime": 180,
            },
        ],
        TARGET,
    )

    assert normalized is not None
    assert normalized["trainingReadiness"] == 73
    assert normalized["recoveryTime"] == 180


def test_training_readiness_normalizer_returns_missing_for_empty_or_invalid_response():
    assert normalize_training_readiness([], TARGET) is None
    assert normalize_training_readiness([{"score": 71}], TARGET) is None
    assert normalize_training_readiness({"calendarDate": "invalid", "value": 71}, TARGET) is None


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
