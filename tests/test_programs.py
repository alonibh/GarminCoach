from coach.programs import recommend_program


def test_sport_commitment_selects_conservative_two_day_strength_program():
    proposal = recommend_program(
        goal="Build strength",
        preferred_activities=["Strength", "Soccer"],
        equipment=["gym"],
        sport_commitments="Soccer every Saturday",
        limitations="",
        days_per_week=3,
        history_summary="Strength is the most common activity.",
    )

    assert proposal["key"] == "sport_support_2"
    assert len(proposal["sessions"]) == 2
    assert "without assigning dates" in proposal["rationale"]


def test_no_gym_access_selects_minimal_equipment_program():
    proposal = recommend_program(
        goal="Stay consistent",
        preferred_activities=["Walking"],
        equipment=["bodyweight"],
        sport_commitments="",
        limitations="",
        days_per_week=4,
        history_summary="There are few synced activities.",
    )

    assert proposal["key"] == "minimal_equipment_2"
    assert proposal["sessions"][0]["exercises"][0]["exercise_name"] == "BODYWEIGHT_SQUAT"


def test_overhead_limitation_removes_overhead_press_from_template():
    proposal = recommend_program(
        goal="Build strength",
        preferred_activities=["Strength"],
        equipment=["gym"],
        sport_commitments="",
        limitations="No heavy overhead press",
        days_per_week=3,
        history_summary="Strength is the most common activity.",
    )

    exercises = [
        exercise["exercise_name"]
        for session in proposal["sessions"]
        for exercise in session["exercises"]
    ]
    assert "OVERHEAD_PRESS" not in exercises
    assert "Overhead pressing was removed" in proposal["rationale"]
