from __future__ import annotations

import inspect
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

import pytest


EXPECTED_VERSION = os.getenv("GARMINCONNECT_COMPAT_VERSION")
pytestmark = pytest.mark.skipif(
    EXPECTED_VERSION is None,
    reason="requires the isolated garminconnect 0.3.7 compatibility environment",
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "garmin"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_compatibility_environment_uses_exact_distribution():
    assert EXPECTED_VERSION == "0.3.7"
    assert version("garminconnect") == EXPECTED_VERSION


def test_typed_models_needed_by_garmincoach_import_and_validate_fixtures():
    from garminconnect.typed import (
        Activity,
        BodyBatteryEntry,
        DailyStats,
        HrvData,
        SleepData,
        TrainingReadiness,
        TypedGarmin,
    )

    assert TypedGarmin is not None
    assert DailyStats.model_validate(_fixture("daily_stats.json")).total_steps == 6543
    assert SleepData.model_validate(_fixture("sleep.json")).daily_sleep_dto.sleep_time_seconds == 26100
    assert HrvData.model_validate(_fixture("hrv.json")).hrv_summary.last_night_avg == 46
    assert [
        BodyBatteryEntry.model_validate(item).charged
        for item in _fixture("body_battery.json")
    ] == [48]
    assert [
        TrainingReadiness.model_validate(item).score
        for item in _fixture("training_readiness_list.json")
    ] == [71]
    assert [
        Activity.model_validate(item).activity_id
        for item in _fixture("activities.json")
    ] == [987654321]


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("get_activities_by_date", ("2026-07-01", "2026-07-02")),
        ("get_activities", (0, 1)),
        ("count_activities", ()),
        ("get_activity_exercise_sets", (1,)),
        ("get_activity_hr_in_timezones", (1,)),
        ("get_sleep_data", ("2026-07-01",)),
        ("get_hrv_data", ("2026-07-01",)),
        ("get_body_battery", ("2026-07-01", "2026-07-01")),
        ("get_all_day_stress", ("2026-07-01",)),
        ("get_rhr_day", ("2026-07-01",)),
        ("get_daily_steps", ("2026-07-01", "2026-07-01")),
        ("get_stats", ("2026-07-01",)),
        ("get_device_last_used", ()),
        ("get_training_readiness", ("2026-07-01",)),
        ("get_training_status", ("2026-07-01",)),
        ("get_full_name", ()),
        ("get_activity", (1,)),
        ("get_workouts", ()),
        ("get_workout_by_id", (1,)),
        ("login", ()),
        ("login", ("token-store",)),
        ("resume_login", ({}, "123456")),
    ],
)
def test_methods_called_by_current_garmin_adapter_accept_current_call_shape(
    method_name,
    args,
):
    from garminconnect import Garmin

    method = getattr(Garmin, method_name)
    inspect.signature(method).bind(None, *args)


def test_constructor_and_token_serialization_members_used_by_adapter_exist():
    from garminconnect import Garmin

    inspect.signature(Garmin).bind()
    inspect.signature(Garmin).bind(
        email="athlete.invalid",
        password="not-a-real-password",
        prompt_mfa=lambda: "",
        return_on_mfa=True,
    )
    api = Garmin()
    assert callable(api.client.loads)
    assert callable(api.client.dumps)


def test_wrapper_declares_both_supported_training_readiness_return_shapes():
    from garminconnect import Garmin
    from sync.garmin_client import GarminClient

    upstream_return = inspect.signature(Garmin.get_training_readiness).return_annotation
    wrapper_return = get_type_hints(GarminClient.training_readiness)["return"]
    assert get_origin(upstream_return) is list
    assert set(get_args(wrapper_return)) == {dict, list[dict]}
