from coach.programs import PLAN_CHOICES, PROGRAMS, _exercise, _session, recommend_program
from coach.exercises import GARMIN_EXERCISES, exercise_metadata, muscle_group_for
from coach.program_policy import REJECTED_DEFAULT_ROUTINES, SOURCE_TRAINING_LEVELS


def test_catalog_contains_twenty_five_reviewed_routines_from_two_to_six_days():
    assert len(GARMIN_EXERCISES) > 1800
    assert len(PROGRAMS) == 25
    assert {len(program["sessions"]) for program in PROGRAMS.values()} == {2, 3, 4, 5, 6}
    assert {days: sum(len(program["sessions"]) == days for program in PROGRAMS.values()) for days in range(2, 7)} == {
        2: 1, 3: 7, 4: 9, 5: 3, 6: 5,
    }
    for program in PROGRAMS.values():
        assert program["source_url"].startswith("https://www.muscleandstrength.com/")
        assert all(count >= 2 for count in program["region_exposures"].values())
        assert program["weekly_sets"]


def test_get_ripped_adaptation_is_not_selectable():
    assert "upper_lower_full_3" not in PROGRAMS
    assert "upper_lower_full_3" not in {choice["key"] for choice in PLAN_CHOICES}


def test_plan_choices_include_clear_experience_badges():
    assert {choice["experience_label"] for choice in PLAN_CHOICES} == {"Beginner", "Intermediate", "Advanced"}
    assert {choice["experience_slug"] for choice in PLAN_CHOICES} == {"beginner", "intermediate", "advanced"}


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
        "upper_lower_4": "Beginner",
        "shul_4": "Intermediate",
        "split_full_4": "Advanced",
        "muscle_strength_5": "Intermediate",
        "ppl_6": "Beginner",
        "dumbbell_full_body_3": "Beginner",
        "planet_fitness_full_body_3": "Beginner",
        "long_cycle_full_body_3": "Beginner",
        "whole_body_toning_3": "Intermediate",
        "planet_fitness_upper_lower_4": "Beginner",
        "optimized_volume_4": "Beginner",
        "phul_4": "Intermediate",
        "dumbbell_upper_lower_4": "Beginner",
        "barbell_no_rack_4": "Intermediate",
        "barbell_upper_lower_4": "Beginner",
        "maul_5": "Beginner",
        "dumbbell_split_5": "Intermediate",
        "powerbuilding_ppl_6": "Intermediate",
        "low_volume_high_intensity_6": "Intermediate",
        "built_different_ppl_6": "Advanced",
        "muscle_mania_6": "Advanced",
    }
    assert labels == dict(SOURCE_TRAINING_LEVELS)


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
    assert {exercise["exercise_name"] for exercise in warmed} >= {"Trap Bar Deadlift", "Military Press", "Front Squat", "Dumbbell Bench Press", "Chin Up"}
    assert "Lat Pull Down" not in {exercise["exercise_name"] for exercise in warmed}
    assert "T Bar Row" not in {exercise["exercise_name"] for exercise in warmed}
    assert "Cable Row" not in {exercise["exercise_name"] for exercise in warmed}
    assert all(1 <= exercise["warmup_reps"] <= 8 for exercise in warmed)
    assert all(exercise["warmup_weight_kg"] is None for exercise in warmed)
    assert PROGRAMS["upper_lower_4"]["sessions"][0]["exercises"][0]["warmup_enabled"] is True


def test_templates_do_not_add_warmups_to_abdominal_exercises():
    abdominal_categories = {"CHOP", "CORE", "CRUNCH", "LEG_RAISE", "PLANK", "SIT_UP"}
    abdominal_exercises = [
        exercise
        for program in PROGRAMS.values()
        for session in program["sessions"]
        for exercise in session["exercises"]
        if (exercise_metadata(exercise["exercise_name"]) or {}).get("category") in abdominal_categories
    ]
    assert abdominal_exercises
    assert all(exercise["warmup_enabled"] is False for exercise in abdominal_exercises)


def test_templates_warm_the_first_isolation_for_a_new_major_region_only():
    day_two = PROGRAMS["total_package_3"]["sessions"][1]["exercises"]
    enabled = {exercise["exercise_name"] for exercise in day_two if exercise["warmup_enabled"]}
    assert enabled == {"Bench Press", "Leg Extension", "Pullup"}
    assert next(exercise for exercise in day_two if exercise["exercise_name"] == "Leg Extension")["warmup_enabled"]
    assert not next(exercise for exercise in day_two if exercise["exercise_name"] == "Leg Curl")["warmup_enabled"]
    assert not next(exercise for exercise in day_two if exercise["exercise_name"] == "Seated Lateral Raise")["warmup_enabled"]
    assert not next(exercise for exercise in day_two if exercise["exercise_name"] == "Dumbbell Hammer Curls")["warmup_enabled"]
    day_three = PROGRAMS["total_package_3"]["sessions"][2]["exercises"]
    assert not next(exercise for exercise in day_three if exercise["exercise_name"] == "Pulldown")["warmup_enabled"]


def test_total_package_warmups_match_the_final_day_by_day_rules():
    expected = [
        {"Squat", "Dumbbell Bench Press", "Dumbbell Row", "Seated Dumbbell Press", "Lunge"},
        {"Bench Press", "Leg Extension", "Pullup"},
        {"Deadlift", "Incline Dumbbell Press", "Leg Press"},
    ]
    actual = [
        {exercise["exercise_name"] for exercise in session["exercises"] if exercise["warmup_enabled"]}
        for session in PROGRAMS["total_package_3"]["sessions"]
    ]
    assert actual == expected


def test_first_direct_chest_press_warms_up_even_if_garmin_labels_dips_as_triceps():
    workout = PROGRAMS["ms_full_body_3"]["sessions"][1]["exercises"]
    assert next(exercise for exercise in workout if exercise["exercise_name"] == "Dips")["warmup_enabled"] is True


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
        plan_key="ms_full_body_3", limitations="",
        session_duration_min=60, history_summary="Running is common.",
    )
    assert all(s["sport_type"] == "strength_training" and s["session_role"] == "coach_strength" for s in proposal["sessions"])
    assert "does not assign dates or upload" in proposal["rationale"]


def _rests(program_key, session_name):
    routine = next(
        routine for routine in PROGRAMS[program_key]["sessions"]
        if routine["name"] == session_name
    )
    return {exercise["exercise_name"]: exercise["rest_seconds"] for exercise in routine["exercises"]}


def test_all_ten_templates_use_their_source_reviewed_between_set_rest_rules():
    assert {
        exercise["rest_seconds"]
        for routine in PROGRAMS["full_body_2"]["sessions"]
        for exercise in routine["exercises"]
    } == {120}

    beginner = {
        name: rest
        for routine in PROGRAMS["beginner_full_body_3"]["sessions"]
        for name, rest in _rests("beginner_full_body_3", routine["name"]).items()
    }
    assert {beginner[name] for name in {"Trap Bar Deadlift", "Front Squat", "Bench Press"}} == {300}
    assert beginner["Farmer's Carry"] == 45
    assert set(beginner.values()) == {45, 90, 300}

    ms_full_body = {
        name: rest
        for routine in PROGRAMS["ms_full_body_3"]["sessions"]
        for name, rest in _rests("ms_full_body_3", routine["name"]).items()
    }
    assert {ms_full_body[name] for name in {"Squat", "Bench Press", "Barbell Row", "Deadlift", "Seated Overhead Press"}} == {120}
    assert ms_full_body["Romanian Deadlift"] == 90
    assert set(ms_full_body.values()) == {90, 120}

    total_package = {
        name: rest
        for routine in PROGRAMS["total_package_3"]["sessions"]
        for name, rest in _rests("total_package_3", routine["name"]).items()
    }
    assert {total_package[name] for name in {"Squat", "Bench Press", "Deadlift"}} == {180}
    assert set(total_package.values()) == {60, 180}

    upper_lower_compounds = {
        "Bench Press", "Barbell Row", "Seated Overhead Dumbbell Press", "V-Bar Lat Pull Down",
        "Squat", "Stiff Leg Deadlifts", "Incline Dumbbell Bench Press", "Rack Deadlifts",
        "Military Press", "Machine Chest Press", "Machine Row", "Machine Shoulder Press",
        "Leg Press", "Dumbbell Stiff Leg Deadlift", "Hack Squat",
    }
    upper_lower = {
        name: rest
        for routine in PROGRAMS["upper_lower_4"]["sessions"]
        for name, rest in _rests("upper_lower_4", routine["name"]).items()
    }
    assert {upper_lower[name] for name in upper_lower_compounds} == {90}
    assert {rest for name, rest in upper_lower.items() if name not in upper_lower_compounds} == {60}

    assert set(_rests("shul_4", "Lower Strength").values()) == {120, 300}
    assert set(_rests("shul_4", "Upper Strength").values()) == {120, 300}
    shul_lower_hypertrophy = _rests("shul_4", "Lower Hypertrophy")
    assert {shul_lower_hypertrophy[name] for name in {"Leg Extension", "Standing Machine Calf Raise"}} == {45}
    assert {rest for name, rest in shul_lower_hypertrophy.items() if name not in {"Leg Extension", "Standing Machine Calf Raise"}} == {60}
    shul_upper_hypertrophy = _rests("shul_4", "Upper Hypertrophy")
    assert {shul_upper_hypertrophy[name] for name in {"Face Pull", "Lateral Raise", "Barbell Curl", "Incline Skullcrusher"}} == {45}
    assert {rest for name, rest in shul_upper_hypertrophy.items() if name not in {"Face Pull", "Lateral Raise", "Barbell Curl", "Incline Skullcrusher"}} == {60}

    assert {
        exercise["rest_seconds"]
        for routine in PROGRAMS["split_full_4"]["sessions"]
        for exercise in routine["exercises"]
    } == {45}
    assert set(_rests("muscle_strength_5", "Upper Strength").values()) == {180}
    assert set(_rests("muscle_strength_5", "Lower Strength").values()) == {180}
    for session_name in {"Back & Shoulders Size", "Chest & Arms Size", "Legs Size"}:
        assert set(_rests("muscle_strength_5", session_name).values()) == {90}
    assert {
        exercise["rest_seconds"]
        for routine in PROGRAMS["ppl_6"]["sessions"]
        for exercise in routine["exercises"]
    } == {45}
    assert {
        exercise["rest_seconds"]
        for routine in PROGRAMS["dumbbell_full_body_3"]["sessions"]
        for exercise in routine["exercises"]
    } == {60}


def test_major_region_gate_ignores_focus_labels_and_arm_isolation():
    from coach.programs import _program

    try:
        _program("Incomplete", "https://www.muscleandstrength.com/workouts/example", "new", [
            _session("A", "full body", [
                _exercise("Squat", 3, 8), _exercise("Bench Press", 3, 8), _exercise("Barbell Curl", 3, 8),
            ]),
            _session("B", "full body", [
                _exercise("Deadlift", 3, 8), _exercise("Military Press", 3, 8), _exercise("Hammer Curl", 3, 8),
            ]),
        ], "Deliberately incomplete")
    except ValueError as exc:
        assert "does not provide two weekly" in str(exc)
    else:
        raise AssertionError("Arm isolation must not satisfy the major-region pull gate")


def test_temporary_and_phase_specific_routines_are_not_selectable_defaults():
    assert set(PROGRAMS).isdisjoint(REJECTED_DEFAULT_ROUTINES)


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


def test_deadlift_maps_to_garmins_barbell_deadlift_not_the_banded_variant():
    metadata = exercise_metadata("Deadlift")
    assert metadata is not None
    assert metadata["key"] == "DEADLIFT:BARBELL_DEADLIFT"


def test_primary_muscle_groups_cover_close_and_cross_category_alternatives():
    assert muscle_group_for("BENCH_PRESS:BARBELL_BENCH_PRESS") == "chest"
    assert muscle_group_for("FLYE:DUMBBELL_FLYE") == "chest"
    assert muscle_group_for("ROW:SEATED_CABLE_ROW") == "back"
    assert muscle_group_for("Seated Dumbbell Press") == "shoulders"
    assert muscle_group_for("Seated Leg Curl") == "hamstrings_glutes"
