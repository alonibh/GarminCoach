"""Tests for the schedule_workout action validator (coach/actions.py)."""
import pytest
from pydantic import ValidationError

from coach.actions import parse_action


def test_valid_full_payload():
    a = parse_action({
        "action": "schedule_workout",
        "base_workout_id": 123,
        "suggested_time": "18:00",
        "modifications": [
            {"type": "keep_and_modify", "index": 0, "new_sets": 2},
            {"type": "add_new", "description": "Spiderman Pushups", "sets": 3, "reps": 10, "weight_kg": 0},
        ],
    })
    assert a.base_workout_id == 123
    assert a.suggested_time == "18:00"
    assert len(a.modifications) == 2


def test_minimal_payload_defaults():
    a = parse_action({"action": "schedule_workout", "base_workout_id": 1})
    assert a.suggested_time is None
    assert a.modifications == []


def test_empty_time_becomes_none():
    a = parse_action({"action": "schedule_workout", "base_workout_id": 1, "suggested_time": ""})
    assert a.suggested_time is None


@pytest.mark.parametrize("bad_time", ["18:00 PM", "6pm", "25:00", "18:60", "1800"])
def test_invalid_time_rejected(bad_time):
    with pytest.raises(ValidationError):
        parse_action({"action": "schedule_workout", "base_workout_id": 1, "suggested_time": bad_time})


def test_non_numeric_reps_rejected():
    with pytest.raises(ValidationError):
        parse_action({
            "action": "schedule_workout",
            "base_workout_id": 1,
            "modifications": [{"type": "keep_and_modify", "index": 0, "new_reps": "ten"}],
        })


def test_missing_base_workout_id_rejected():
    with pytest.raises(ValidationError):
        parse_action({"action": "schedule_workout"})


def test_wrong_action_rejected():
    with pytest.raises(ValidationError):
        parse_action({"action": "delete_everything", "base_workout_id": 1})


def test_unknown_modification_type_rejected():
    with pytest.raises(ValidationError):
        parse_action({
            "action": "schedule_workout",
            "base_workout_id": 1,
            "modifications": [{"type": "nuke", "index": 0}],
        })


def test_negative_weight_rejected():
    with pytest.raises(ValidationError):
        parse_action({
            "action": "schedule_workout",
            "base_workout_id": 1,
            "modifications": [{"type": "keep_and_modify", "index": 0, "new_weight_kg": -5}],
        })


def test_model_dump_fills_omitted_with_none():
    a = parse_action({
        "action": "schedule_workout",
        "base_workout_id": 1,
        "modifications": [{"type": "keep_and_modify", "index": 0}],
    })
    mod = a.model_dump()["modifications"][0]
    assert mod["new_sets"] is None
    assert mod["new_reps"] is None
    assert mod["new_weight_kg"] is None


def test_valid_schedule_session_calendar_only():
    a = parse_action({
        "action": "schedule_session",
        "title": "Easy Run",
        "activity_type": "running",
        "target_date": "2026-07-03",
        "suggested_time": "07:00",
        "duration_min": 45,
        "intensity": "light",
    })
    assert a.action == "schedule_session"
    assert a.base_workout_id is None
    assert a.duration_min == 45


def test_valid_schedule_session_with_garmin_template_and_modifications():
    a = parse_action({
        "action": "schedule_session",
        "program_session_id": 7,
        "title": "Upper Body",
        "activity_type": "strength_training",
        "base_workout_id": 123,
        "target_date": "2026-07-03",
        "suggested_time": "18:30",
        "duration_min": 60,
        "intensity": "normal",
        "modifications": [{"type": "keep_and_modify", "index": 0, "new_reps": 8}],
    })
    assert a.program_session_id == 7
    assert a.base_workout_id == 123
    assert len(a.modifications) == 1


@pytest.mark.parametrize("bad_date", ["2026/07/03", "tomorrow", "", "03-07-2026"])
def test_schedule_session_invalid_date_rejected(bad_date):
    with pytest.raises(ValidationError):
        parse_action({
            "action": "schedule_session",
            "title": "Run",
            "activity_type": "running",
            "target_date": bad_date,
        })


def test_schedule_session_invalid_intensity_rejected():
    with pytest.raises(ValidationError):
        parse_action({
            "action": "schedule_session",
            "title": "Run",
            "activity_type": "running",
            "target_date": "2026-07-03",
            "intensity": "destroy",
        })
