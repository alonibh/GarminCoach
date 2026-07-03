import pytest
from coach.garmin_compiler import _get_step_weight, build_generic_step
from db import PlannedSession, SyncState

def test_get_step_weight_with_none_weight():
    """Test that a step with weightValue = None (like bodyweight exercises) does not crash."""
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {
            "stepTypeKey": "interval"
        },
        "weightValue": None
    }
    # Should not throw TypeError and should return 0.0
    weight = _get_step_weight(step)
    assert weight == 0.0

def test_get_step_weight_with_valid_weight():
    """Test that a step with valid weightValue is parsed correctly."""
    step = {
        "type": "ExecutableStepDTO",
        "weightValue": 50.5
    }
    weight = _get_step_weight(step)
    assert weight == 50.5

def test_get_step_weight_in_repeat_group_with_none_weight():
    """Test RepeatGroupDTO with null weightValue does not crash."""
    step = {
        "type": "RepeatGroupDTO",
        "workoutSteps": [
            {
                "stepType": {"stepTypeKey": "interval"},
                "weightValue": None
            }
        ]
    }
    weight = _get_step_weight(step)
    assert weight == 0.0


def test_schedule_session_calendar_only_creates_planned_session(session):
    from coach.garmin_compiler import compile_and_schedule

    ok = compile_and_schedule(session, {
        "action": "schedule_session",
        "title": "Easy Run",
        "activity_type": "running",
        "target_date": "2026-07-03",
        "suggested_time": "07:00",
        "duration_min": 45,
        "intensity": "light",
    })

    assert ok is True
    planned = session.query(PlannedSession).one()
    assert planned.title == "Easy Run"
    assert planned.status == "approved"
    row = session.get(SyncState, "coach_calendar_events")
    assert row is not None
    assert "Easy Run" in row.value
