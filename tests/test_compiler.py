import pytest
from coach.garmin_compiler import _get_step_weight, build_generic_step, _build_rampup_steps

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

def test_build_rampup_steps():
    """Test that rampup step calculation handles weights correctly."""
    rampups = _build_rampup_steps(45.0, "Bench Press", exercise_name="Bench Press", category="CHEST")
    assert len(rampups) == 1
    
    group = rampups[0]
    assert group["type"] == "RepeatGroupDTO"
    
    interval = group["workoutSteps"][0]
    assert interval["weightValue"] == 22.5  # 45.0 * 0.5 rounded to nearest 2.5
    assert interval["endConditionValue"] == 8  # 8 reps
