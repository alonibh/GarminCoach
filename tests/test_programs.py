from coach.programs import PLAN_CHOICES, PROGRAMS, _exercise, _session, recommend_program
from coach.exercises import GARMIN_EXERCISES, exercise_metadata, muscle_group_for


def test_catalog_contains_ten_reviewed_routines_from_two_to_six_days():
    assert len(GARMIN_EXERCISES) > 1800
    assert len(PROGRAMS) == 10
    assert {len(program["sessions"]) for program in PROGRAMS.values()} == {2, 3, 4, 5, 6}
    for program in PROGRAMS.values():
        assert program["source_url"].startswith("https://www.muscleandstrength.com/")
        assert all(count >= 2 for count in program["region_exposures"].values())
        assert program["weekly_sets"]


def test_plan_choices_include_clear_experience_badges():
    assert {choice["experience_label"] for choice in PLAN_CHOICES} == {"Beginner", "Intermediate", "Expert"}
    assert {choice["experience_slug"] for choice in PLAN_CHOICES} == {"beginner", "intermediate", "expert"}


def test_plan_choices_include_original_routine_detail_badges():
    expected_labels = {"Main Goal", "Workout Type", "Training Level", "Days Per Week", "Time Per Workout"}
    for choice in PLAN_CHOICES:
        details = dict(choice["source_details"])
        assert set(details) == expected_labels
        assert details["Training Level"] == choice["experience_label"]
        assert details["Days Per Week"] == f"{choice['days']} days"
        assert details["Time Per Workout"] == f"{choice['duration_min']} min"


def test_source_training_levels_are_reflected_in_catalog_badges():
    labels = {choice["key"]: choice["experience_label"] for choice in PLAN_CHOICES}
    assert labels == {
        "full_body_2": "Beginner",
        "beginner_full_body_3": "Beginner",
        "ms_full_body_3": "Beginner",
        "total_package_3": "Intermediate",
        "upper_lower_full_3": "Intermediate",
        "upper_lower_4": "Beginner",
        "shul_4": "Intermediate",
        "split_full_4": "Expert",
        "muscle_strength_5": "Intermediate",
        "ppl_6": "Beginner",
    }


def test_source_program_is_not_trimmed_by_free_text_duration_limit():
    proposal = recommend_program(
        plan_key="upper_lower_4",
        limitations="max 30 minutes", session_duration_min=30,
        history_summary="There are few synced activities.",
    )
    assert len(proposal["sessions"][0]["exercises"]) == len(PROGRAMS["upper_lower_4"]["sessions"][0]["exercises"])
    assert "not silently trimmed" in proposal["rationale"]


def test_templates_follow_daily_anchor_and_cold_joint_warmup_rules():
    proposal = recommend_program(
        plan_key="full_body_2", limitations="",
        session_duration_min=90, history_summary="Recent history is sparse.",
    )
    exercises = [exercise for routine in proposal["sessions"] for exercise in routine["exercises"]]
    warmed = [exercise for exercise in exercises if exercise["warmup_enabled"]]
    assert {exercise["exercise_name"] for exercise in warmed} >= {"Trap Bar Deadlift", "Military Press", "Lat Pull Down", "Front Squat", "Dumbbell Bench Press", "Chin Up"}
    assert "T Bar Row" not in {exercise["exercise_name"] for exercise in warmed}
    assert "Cable Row" not in {exercise["exercise_name"] for exercise in warmed}
    assert all(1 <= exercise["warmup_reps"] <= 8 for exercise in warmed)
    assert all(exercise["warmup_weight_kg"] is None for exercise in warmed)
    assert PROGRAMS["upper_lower_full_3"]["sessions"][0]["exercises"][0]["warmup_enabled"] is True


def test_templates_warm_the_first_isolation_for_a_new_major_region_only():
    day_two = PROGRAMS["total_package_3"]["sessions"][1]["exercises"]
    enabled = {exercise["exercise_name"] for exercise in day_two if exercise["warmup_enabled"]}
    assert enabled == {"Bench Press", "Leg Extension", "Pullup", "Seated Lateral Raise"}
    assert next(exercise for exercise in day_two if exercise["exercise_name"] == "Leg Extension")["warmup_enabled"]
    assert not next(exercise for exercise in day_two if exercise["exercise_name"] == "Leg Curl")["warmup_enabled"]
    assert not next(exercise for exercise in day_two if exercise["exercise_name"] == "Dumbbell Hammer Curls")["warmup_enabled"]


def test_long_break_return_to_a_heavy_compound_gets_one_warmup_set():
    session = _session("Test", "full body", [
        _exercise("Squat", 5, 5),
        _exercise("Bench Press", 5, 5),
        _exercise("Dumbbell Row", 5, 5),
        _exercise("Seated Dumbbell Press", 5, 5),
        _exercise("Lunge", 5, 5),
    ])
    assert session["exercises"][-1]["warmup_enabled"] is True


def test_all_program_sessions_are_gym_only_and_undated():
    proposal = recommend_program(
        plan_key="upper_lower_full_3", limitations="",
        session_duration_min=60, history_summary="Running is common.",
    )
    assert all(s["sport_type"] == "strength_training" and s["session_role"] == "coach_strength" for s in proposal["sessions"])
    assert "does not assign dates or upload" in proposal["rationale"]


def test_total_package_uses_sixty_second_default_rest():
    exercises = [
        exercise
        for routine in PROGRAMS["total_package_3"]["sessions"]
        for exercise in routine["exercises"]
    ]
    assert exercises
    assert {exercise["rest_seconds"] for exercise in exercises} == {60}


def test_every_curated_exercise_has_a_primary_muscle_group():
    missing = [
        exercise["exercise_name"]
        for program in PROGRAMS.values()
        for routine in program["sessions"]
        for exercise in routine["exercises"]
        if not muscle_group_for(exercise["exercise_key"] or exercise["exercise_name"], exercise["movement_pattern"])
    ]
    assert missing == []


def test_every_curated_exercise_maps_to_the_garmin_catalog():
    missing = [
        exercise["exercise_name"]
        for program in PROGRAMS.values()
        for routine in program["sessions"]
        for exercise in routine["exercises"]
        if exercise_metadata(exercise["exercise_name"]) is None
    ]
    assert missing == []


def test_seated_dumbbell_press_maps_to_garmins_seated_shoulder_press():
    metadata = exercise_metadata("Seated Dumbbell Press")
    assert metadata is not None
    assert metadata["key"] == "SHOULDER_PRESS:SEATED_DUMBBELL_SHOULDER_PRESS"


def test_primary_muscle_groups_cover_close_and_cross_category_alternatives():
    assert muscle_group_for("BENCH_PRESS:BARBELL_BENCH_PRESS") == "chest"
    assert muscle_group_for("FLYE:DUMBBELL_FLYE") == "chest"
    assert muscle_group_for("ROW:SEATED_CABLE_ROW") == "back"
    assert muscle_group_for("Seated Dumbbell Press") == "shoulders"
    assert muscle_group_for("Seated Leg Curl") == "hamstrings_glutes"
