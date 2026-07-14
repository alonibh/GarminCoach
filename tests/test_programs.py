from coach.programs import PROGRAMS, recommend_program
from coach.exercises import GARMIN_EXERCISES


def test_catalog_contains_ten_reviewed_routines_from_two_to_six_days():
    assert len(GARMIN_EXERCISES) > 1800
    assert len(PROGRAMS) == 10
    assert {len(program["sessions"]) for program in PROGRAMS.values()} == {2, 3, 4, 5, 6}
    for program in PROGRAMS.values():
        assert program["source_url"].startswith("https://www.muscleandstrength.com/")
        assert all(count >= 2 for count in program["region_exposures"].values())
        assert program["weekly_sets"]


def test_source_program_is_not_trimmed_by_free_text_duration_limit():
    proposal = recommend_program(
        goal="Feel fitter & more consistent", plan_key="upper_lower_4",
        limitations="max 30 minutes", session_duration_min=30,
        history_summary="There are few synced activities.",
    )
    assert len(proposal["sessions"][0]["exercises"]) == len(PROGRAMS["upper_lower_4"]["sessions"][0]["exercises"])
    assert "not silently trimmed" in proposal["rationale"]


def test_warmup_is_once_per_movement_pattern_with_matching_reps():
    proposal = recommend_program(
        goal="Build strength & muscle", plan_key="full_body_2", limitations="",
        session_duration_min=90, history_summary="Recent history is sparse.",
    )
    for routine in proposal["sessions"]:
        warmed = [ex for ex in routine["exercises"] if ex["warmup_enabled"]]
        assert len({ex["movement_pattern"] for ex in warmed}) == len(warmed)
        assert all(ex["warmup_reps"] == ex["reps"] for ex in warmed)
        assert all(ex["warmup_weight_kg"] is None for ex in warmed)


def test_all_program_sessions_are_gym_only_and_undated():
    proposal = recommend_program(
        goal="Improve a sport/activity", plan_key="upper_lower_full_3", limitations="",
        session_duration_min=60, history_summary="Running is common.",
    )
    assert all(s["sport_type"] == "strength_training" and s["session_role"] == "coach_strength" for s in proposal["sessions"])
    assert "does not assign dates or upload" in proposal["rationale"]
