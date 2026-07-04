from datetime import datetime, timedelta

from coach.onboarding import analyze_user_history, routine_sessions_for_setup
from db import Activity, Workout


def _activity(session, activity_id: int, activity_type: str, days_ago: int, name: str = "") -> None:
    session.add(
        Activity(
            id=activity_id,
            activity_type=activity_type,
            start_time=datetime.now() - timedelta(days=days_ago),
            name=name,
        )
    )


def _workout(session, workout_id: int, name: str, sport_type: str = "strength_training") -> None:
    session.add(
        Workout(
            workout_id=workout_id,
            name=name,
            sport_type=sport_type,
            steps_json="[]",
        )
    )


def test_no_history_defaults_to_low_history(session):
    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "low_history"
    assert analysis["defaults"]["primary_goal"] == "General fitness"
    assert analysis["defaults"]["days_per_week"] == 3
    assert analysis["total_activities"] == 0
    assert analysis["routine"]["detected"] is False


def test_thin_history_falls_back_to_preferred_activities(session):
    _activity(session, 1, "strength_training", 1, "Chest & Biceps")
    session.commit()

    analysis = analyze_user_history(session)
    sessions = routine_sessions_for_setup(analysis, ["Strength", "Running"])

    assert analysis["routine"]["detected"] is False
    assert [s["name"] for s in sessions] == ["Full body strength", "Running"]


def test_strength_history_selects_strength_defaults_without_templates(session):
    for idx in range(10):
        _activity(session, idx + 1, "strength_training", idx * 3)
    _activity(session, 20, "running", 8)
    _workout(session, 501, "Upper Strength")
    _workout(session, 502, "Lower Strength")
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "strength_focused"
    assert analysis["defaults"]["primary_goal"] == "Build strength"
    assert "Strength" in analysis["defaults"]["preferred_activities"]
    assert analysis["defaults"]["plan_mode"] == "schedule_my_routine"
    assert analysis["defaults"]["selected_templates"] == []


def test_repeated_strength_names_detect_ab_split(session):
    names = ["Chest & Biceps", "Back & Triceps", "Legs & Shoulders"]
    for idx, name in enumerate(names + names):
        _activity(session, idx + 1, "strength_training", 6 - idx, name)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["routine"]["detected"] is True
    assert [s["name"] for s in analysis["routine"]["sessions"]] == [
        "A - Chest & Biceps",
        "B - Back & Triceps",
        "C - Legs & Shoulders",
    ]


def test_generic_strength_name_is_not_a_split_slot(session):
    names = ["Strength", "Chest & Biceps", "Legs & Shoulders", "Back & Triceps"]
    for idx, name in enumerate(names + names):
        _activity(session, idx + 1, "strength_training", 8 - idx, name)
    session.commit()

    analysis = analyze_user_history(session)

    assert [s["name"] for s in analysis["routine"]["sessions"]] == [
        "A - Chest & Biceps",
        "B - Legs & Shoulders",
        "C - Back & Triceps",
    ]


def test_single_repeated_strength_name_detects_full_body(session):
    for idx in range(3):
        _activity(session, idx + 1, "strength_training", idx, "\U0001f3cb\ufe0f Strength @ 18:00")
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["routine"]["detected"] is True
    assert [s["name"] for s in analysis["routine"]["sessions"]] == ["Full body strength"]


def test_mixed_sport_history_detects_sport_sessions(session):
    for idx in range(3):
        _activity(session, idx + 1, "running", idx)
    for idx in range(2):
        _activity(session, idx + 20, "cycling", idx + 3)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["routine"]["detected"] is True
    assert [s["name"] for s in analysis["routine"]["sessions"]] == ["Running", "Cycling"]


def test_endurance_history_is_classified_from_all_history(session):
    for idx in range(8):
        _activity(session, idx + 1, "running", idx * 45)
    _activity(session, 20, "cycling", 140)
    _activity(session, 21, "strength_training", 1)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "endurance_focused"
    assert analysis["defaults"]["primary_goal"] == "Improve endurance"
    assert analysis["total_activities"] == 10
    assert len(analysis["activity_patterns"]) < analysis["total_activities"]


def test_mixed_strength_and_cardio_history(session):
    for idx in range(5):
        _activity(session, idx + 1, "strength_training", idx)
        _activity(session, idx + 20, "running", idx + 10)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "mixed_fitness"
    assert analysis["defaults"]["primary_goal"] == "Balanced fitness"


def test_sport_recreational_history(session):
    for idx in range(6):
        _activity(session, idx + 1, "soccer", idx)
    for idx in range(4):
        _activity(session, idx + 20, "running", idx + 20)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "sport_recreational"
    assert analysis["defaults"]["primary_goal"] == "Support sport performance"
