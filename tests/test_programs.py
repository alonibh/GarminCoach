from coach.programs import recommend_program


def test_sport_goal_selects_conservative_two_day_gym_program():
    proposal = recommend_program(
        goal="Improve a sport/activity",
        limitations="",
        days_per_week=2,
        session_duration_min=60,
        history_summary="Strength is the most common activity.",
    )

    assert proposal["key"] == "sport_support_2"
    assert len(proposal["sessions"]) == 2
    assert all(session["session_role"] == "coach_strength" for session in proposal["sessions"])
    assert "without assigning dates" in proposal["rationale"]


def test_short_session_trims_gym_program():
    proposal = recommend_program(
        goal="Stay consistent",
        limitations="",
        days_per_week=4,
        session_duration_min=30,
        history_summary="There are few synced activities.",
    )

    assert proposal["key"] == "upper_lower_4"
    assert len(proposal["sessions"][0]["exercises"]) == 3


def test_overhead_limitation_removes_overhead_press_from_template():
    proposal = recommend_program(
        goal="Build strength",
        limitations="No heavy overhead press",
        days_per_week=3,
        session_duration_min=60,
        history_summary="Strength is the most common activity.",
    )

    exercises = [
        exercise["exercise_name"]
        for session in proposal["sessions"]
        for exercise in session["exercises"]
    ]
    assert "OVERHEAD_PRESS" not in exercises
    assert "Overhead pressing was removed" in proposal["rationale"]
