"""Tests for build_snapshot (coach/snapshot.py).

Verifies the payload trims correctly: Garmin templates are included, all-null
metrics blocks dropped, freshness labels added, and days_since_last_trained
populated. No network — get_upcoming_schedule is monkeypatched.
"""
import json
import yaml
from datetime import date, datetime, timedelta

import pytest

from db import (
    Activity,
    AthleteProfile,
    DailyMetrics,
    ExerciseSet,
    PlannedSession,
    ProgramSession,
    SessionExercise,
    TrainingProgram,
    Workout,
)


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
        workout_id=1, name="Upper Strength", sport_type="strength_training",
        steps_json=_strength_steps("BENCH_PRESS", "BENCH_PRESS"),
    ))
    session.add(Workout(
        workout_id=2, name="Lower Strength", sport_type="strength_training",
        steps_json=_strength_steps("SQUAT", "SQUAT"),
    ))
    # A running template should appear in the generic Garmin template list.
    session.add(Workout(
        workout_id=99, name="חזרות על ריצה מהירה", sport_type="running",
        steps_json=json.dumps([{"workoutSteps": []}]),
    ))
    session.commit()


def test_all_null_metrics_block_dropped(session):
    _seed_workouts(session)
    # A metrics row with no real signal at all.
    session.add(DailyMetrics(day=date.today(), readiness=None, acute_load=0.0,
                             chronic_load=0.0, acwr=None, sleep_debt_h=0.0))
    session.commit()
    from coach.snapshot import build_snapshot
    snap = yaml.safe_load(build_snapshot(session))
    # Composite readiness and ACWR are never exposed to the coach snapshot.
    assert "daily_metrics" not in snap


def test_custom_readiness_and_acwr_are_excluded_from_coach_snapshot(session):
    _seed_workouts(session)
    session.add(DailyMetrics(day=date.today(), readiness=78.0, acute_load=120.0,
                             chronic_load=100.0, acwr=1.2, sleep_debt_h=0.0))
    session.commit()
    from coach.snapshot import build_snapshot
    snap = yaml.safe_load(build_snapshot(session))
    assert "daily_metrics" not in snap


def test_days_since_last_trained(session):
    _seed_workouts(session)
    three_days_ago = datetime.now() - timedelta(days=3)
    act = Activity(id=5001, activity_type="strength_training", start_time=three_days_ago)
    session.add(act)
    session.add(ExerciseSet(activity_id=5001, set_index=0, exercise_category="BENCH_PRESS",
                            exercise_name="BENCH_PRESS", reps=10, weight_kg=22.5))
    session.commit()
    from coach.snapshot import build_snapshot
    snap = yaml.safe_load(build_snapshot(session))
    dsl = snap.get("workout_history_log", [])
    assert "'Upper Strength' was trained 3 days ago." in dsl
    # Legs never trained -> None values now formatted as string.
    assert "'Lower Strength' has never been trained in recorded history." in dsl


def test_profile_program_and_rolling_plan_included(session):
    from time_utils import get_local_date

    session.add(AthleteProfile(
        id=1,
        experience_level="beginner",
        primary_goal="general fitness",
        equipment_access='["gym"]',
        onboarding_complete=True,
    ))
    program = TrainingProgram(name="My routine", mode="schedule_my_routine", active=True)
    session.add(program)
    session.flush()
    ps = ProgramSession(program_id=program.id, name="Full body A", sport_type="strength_training", sequence_order=1)
    session.add(ps)
    session.flush()
    session.add(SessionExercise(
        program_session_id=ps.id,
        exercise_name="Goblet Squat",
        exercise_key="SQUAT:GOBLET_SQUAT",
        garmin_category="SQUAT",
        garmin_name="GOBLET_SQUAT",
        movement_pattern="knee_dominant",
        sets=3,
        reps=10,
        rest_seconds=60,
    ))
    empty = ProgramSession(
        program_id=program.id,
        name="Unfinished accessories",
        sport_type="strength_training",
        sequence_order=2,
    )
    session.add(empty)
    session.add(PlannedSession(
        program_session_id=ps.id,
        activity_type="strength_training",
        title="Full body A",
        target_date=get_local_date(),
        suggested_time="07:00",
        duration_min=45,
        intensity="light",
        status="approved",
    ))
    session.commit()

    from coach.snapshot import build_snapshot
    snap = yaml.safe_load(build_snapshot(session))

    assert snap["athlete_profile"]["primary_goal"] == "general fitness"
    assert snap["active_program"]["name"] == "My routine"
    assert snap["active_program"]["sessions"][0]["name"] == "Full body A"
    assert [item["name"] for item in snap["active_program"]["sessions"]] == ["Full body A"]
    assert snap["rolling_plan_14_days"][0]["title"] == "Full body A"


def test_calendar_titles_keep_unicode_in_snapshot(session, monkeypatch):
    import coach.calendar as cal
    from coach.snapshot import build_snapshot

    hebrew_title = "\u05e2\u05e8\u05d1 \u05e7\u05d9\u05e6\u05d5\u05df"
    monkeypatch.setattr(cal, "get_upcoming_schedule", lambda days=7: [{
        "title": hebrew_title,
        "start": "2026-07-08 18:00",
        "end": "19:00",
    }])

    snapshot = build_snapshot(session)

    assert hebrew_title in snapshot
    assert "\\u05" not in snapshot


def test_morning_snapshot_rewrite_keeps_unicode(session):
    from coach.coach import _verbalize_morning_snapshot

    hebrew_title = "\u05e2\u05e8\u05d1 \u05e7\u05d9\u05e6\u05d5\u05df"
    snapshot = _verbalize_morning_snapshot(
        f"upcoming_schedule_7_days:\n- title: {hebrew_title}\n",
        session,
    )

    assert hebrew_title in snapshot
    assert "\\u05" not in snapshot
