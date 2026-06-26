"""Tests for build_snapshot (coach/snapshot.py).

Verifies the payload trims correctly: running workouts excluded, all-null
metrics blocks dropped, freshness labels added, and days_since_last_trained
populated. No network — get_upcoming_schedule is monkeypatched.
"""
import json
from datetime import date, datetime, timedelta

import pytest

from db import Activity, DailyMetrics, ExerciseSet, Workout


def _strength_steps(exercise_name: str, category: str) -> str:
    """Minimal Garmin workoutSegments JSON with one executable strength step."""
    return json.dumps([
        {
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepType": {"stepTypeKey": "interval"},
                    "exerciseName": exercise_name,
                    "category": category,
                    "endConditionValue": 10,
                    "weightValue": 20.0,
                    "endCondition": {"conditionTypeKey": "reps"},
                }
            ]
        }
    ])


@pytest.fixture(autouse=True)
def _no_calendar(monkeypatch):
    # build_snapshot does a late import of coach.calendar.get_upcoming_schedule.
    import coach.calendar as cal
    monkeypatch.setattr(cal, "get_upcoming_schedule", lambda days=7: [])


def _seed_workouts(session):
    session.add(Workout(
        workout_id=1, name="Chest & Biceps", sport_type="strength_training",
        steps_json=_strength_steps("BENCH_PRESS", "BENCH_PRESS"),
    ))
    session.add(Workout(
        workout_id=2, name="Legs & Shoulders", sport_type="strength_training",
        steps_json=_strength_steps("SQUAT", "SQUAT"),
    ))
    # A running template that must NOT appear in the coach payload.
    session.add(Workout(
        workout_id=99, name="חזרות על ריצה מהירה", sport_type="running",
        steps_json=json.dumps([{"workoutSteps": []}]),
    ))
    session.commit()


def test_running_workouts_excluded(session):
    _seed_workouts(session)
    from coach.snapshot import build_snapshot
    snap = json.loads(build_snapshot(session))
    names = [w["name"] for w in snap.get("user_saved_workouts", [])]
    assert "Chest & Biceps" in names
    assert "Legs & Shoulders" in names
    assert all("ריצה" not in n for n in names)  # no running templates
    assert all(w["sport"] == "strength_training" for w in snap["user_saved_workouts"])


def test_all_null_metrics_block_dropped(session):
    _seed_workouts(session)
    # A metrics row with no real signal at all.
    session.add(DailyMetrics(day=date.today(), readiness=None, acute_load=0.0,
                             chronic_load=0.0, acwr=None, sleep_debt_h=0.0))
    session.commit()
    from coach.snapshot import build_snapshot
    snap = json.loads(build_snapshot(session))
    # Should not emit an all-null daily_metrics block; flags availability instead.
    assert "daily_metrics" not in snap
    assert snap.get("metrics_available") is False


def test_metrics_block_kept_when_real(session):
    _seed_workouts(session)
    session.add(DailyMetrics(day=date.today(), readiness=78.0, acute_load=120.0,
                             chronic_load=100.0, acwr=1.2, sleep_debt_h=0.0))
    session.commit()
    from coach.snapshot import build_snapshot
    snap = json.loads(build_snapshot(session))
    assert snap["daily_metrics"]["readiness_score_0_to_100"] == 78.0
    assert snap["daily_metrics"]["acwr_ratio"] == 1.2
    assert snap["daily_metrics"]["acwr_status"]  # label present
    # sleep_debt_h was 0.0 -> pruned as empty
    assert "sleep_debt_hours" not in snap["daily_metrics"]


def test_days_since_last_trained(session):
    _seed_workouts(session)
    three_days_ago = datetime.now() - timedelta(days=3)
    act = Activity(id=5001, activity_type="strength_training", start_time=three_days_ago)
    session.add(act)
    session.add(ExerciseSet(activity_id=5001, set_index=0, exercise_category="BENCH_PRESS",
                            exercise_name="BENCH_PRESS", reps=10, weight_kg=22.5))
    session.commit()
    from coach.snapshot import build_snapshot
    snap = json.loads(build_snapshot(session))
    dsl = snap.get("days_since_last_trained", {})
    assert dsl.get("Chest & Biceps") == 3
    # Legs never trained -> omitted (None values filtered out).
    assert "Legs & Shoulders" not in dsl
