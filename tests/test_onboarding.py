from datetime import datetime, timedelta

from coach.onboarding import analyze_user_history
from db import Activity, Workout


def _activity(session, activity_id: int, activity_type: str, days_ago: int) -> None:
    session.add(
        Activity(
            id=activity_id,
            activity_type=activity_type,
            start_time=datetime.now() - timedelta(days=days_ago),
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


def test_strength_history_selects_strength_defaults_and_templates(session):
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
    assert analysis["defaults"]["plan_mode"] == "existing_templates"
    assert analysis["defaults"]["selected_templates"] == [502, 501]


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
