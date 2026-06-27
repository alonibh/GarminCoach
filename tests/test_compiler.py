import pytest
from coach.garmin_compiler import _get_step_weight, build_generic_step

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

