import pytest

from coach.programs import PLAN_CHOICES, PROGRAMS, _exercise, _session, recommend_program
from coach.exercises import GARMIN_EXERCISES, exercise_metadata, muscle_group_for, catalog_for_ui
from coach.program_policy import REJECTED_DEFAULT_ROUTINES, SOURCE_TRAINING_LEVELS


def test_catalog_contains_twenty_one_reviewed_routines_from_two_to_six_days():
    assert len(GARMIN_EXERCISES) > 1800
    assert len(PROGRAMS) == 21
    assert {len(program["sessions"]) for program in PROGRAMS.values()} == {2, 3, 4, 5, 6}
    assert {days: sum(len(program["sessions"]) == days for program in PROGRAMS.values()) for days in range(2, 7)} == {
        2: 1, 3: 6, 4: 9, 5: 2, 6: 3,
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
        "ppl_6": "Beginner",
        "dumbbell_full_body_3": "Beginner",
        "planet_fitness_full_body_3": "Beginner",
        "whole_body_toning_3": "Intermediate",
        "planet_fitness_upper_lower_4": "Beginner",
        "optimized_volume_4": "Beginner",
        "phul_4": "Intermediate",
        "dumbbell_upper_lower_4": "Beginner",
        "barbell_no_rack_4": "Intermediate",
        "barbell_upper_lower_4": "Beginner",
        "maul_5": "Beginner",
        "dumbbell_split_5": "Intermediate",
        "built_different_ppl_6": "Advanced",
        "muscle_mania_6": "Advanced",
    }
    assert labels == dict(SOURCE_TRAINING_LEVELS)


def test_source_program_is_not_trimmed_by_free_text_duration_limit():
    proposal = recommend_program(
        plan_key="upper_lower_4",
        history_summary="There are few synced activities.",
    )
    assert len(proposal["sessions"][0]["exercises"]) == len(PROGRAMS["upper_lower_4"]["sessions"][0]["exercises"])
    assert "weekly workout windows and rest days" in proposal["rationale"]


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
        "Leg Press", "Dumbbell Stiff Leg Deadlift",
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
    assert {
        exercise["rest_seconds"]
        for routine in PROGRAMS["ppl_6"]["sessions"]
        for exercise in routine["exercises"]
    } == {90}
    assert {
        exercise["rest_seconds"]
        for routine in PROGRAMS["dumbbell_full_body_3"]["sessions"]
        for exercise in routine["exercises"]
    } == {60}


def test_ppl_6_all_exercises_have_90s_rest():
    """ppl_6 uses a single 90 s rest value (between sets and after the final set); no transition field."""
    for session in PROGRAMS["ppl_6"]["sessions"]:
        for item in session["exercises"]:
            assert item["rest_seconds"] == 90
            assert "transition_rest_seconds" not in item


def test_muscle_strength_5_is_absent_from_catalog():
    assert "muscle_strength_5" not in PROGRAMS
    assert "muscle_strength_5" not in {choice["key"] for choice in PLAN_CHOICES}
    assert len(PLAN_CHOICES) == 21


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


# ---------------------------------------------------------------------------
# Gate C regression tests — expansion routines audited 2026-07-22
# ---------------------------------------------------------------------------

def test_expansion_routine_session_and_exercise_counts():
    """Each expansion routine has its source-reviewed session and exercise structure."""
    expected = {
        "planet_fitness_full_body_3": (3, [7, 8, 8]),
        "whole_body_toning_3": (3, [7, 7, 6]),
        "planet_fitness_upper_lower_4": (4, [8, 6, 8, 6]),
        "optimized_volume_4": (4, [7, 5, 7, 5]),
        "phul_4": (4, [7, 5, 7, 6]),
        "dumbbell_upper_lower_4": (4, [7, 6, 7, 6]),
        "barbell_no_rack_4": (4, [6, 6, 6, 7]),
        "barbell_upper_lower_4": (4, [5, 5, 5, 5]),
        "maul_5": (5, [4, 4, 4, 4, 4]),
        "dumbbell_split_5": (5, [6, 7, 6, 7, 7]),
        "built_different_ppl_6": (6, [4, 4, 4, 4, 4, 4]),
        "muscle_mania_6": (6, [6, 6, 6, 6, 6, 6]),
    }
    for key, (num_sessions, exercise_counts) in expected.items():
        program = PROGRAMS[key]
        assert len(program["sessions"]) == num_sessions, f"{key}: session count"
        for idx, count in enumerate(exercise_counts):
            actual = len(program["sessions"][idx]["exercises"])
            assert actual == count, f"{key} session[{idx}]: expected {count} exercises, got {actual}"


def test_phul_uses_source_reviewed_rest_values():
    """PHUL uses 180 s for main power-set compounds and 60 s for accessories/hypertrophy."""
    # Power sessions: main 3-rep lifts get 180 s; 6-rep accessories use 60 s default.
    # Hypertrophy sessions: all exercises use 60 s default.
    for session in PROGRAMS["phul_4"]["sessions"]:
        if session["name"] in ("Upper Power", "Lower Power"):
            for exercise in session["exercises"]:
                if exercise["reps"] == 3:
                    assert exercise["rest_seconds"] == 180, (
                        f"PHUL {session['name']} main lift {exercise['exercise_name']} should be 180s"
                    )
                else:
                    assert exercise["rest_seconds"] == 60, (
                        f"PHUL {session['name']} accessory {exercise['exercise_name']} should be 60s"
                    )
        else:
            rests = {e["rest_seconds"] for e in session["exercises"]}
            assert rests == {60}, f"PHUL {session['name']} should be all 60s, got {rests}"


def test_expansion_routines_use_garmincoach_default_rest_where_source_is_silent():
    """Expansion routines where source specifies no rest use 60 s (GarminCoach product default)."""
    # Routines where source is silent on rest — 60 s is the GarminCoach default, not attributed to ACSM.
    # Routines where source is silent on rest — 60 s GarminCoach default, not ACSM-attributed.
    garmincoach_default_keys = {
        "planet_fitness_full_body_3",
        "planet_fitness_upper_lower_4",
        "dumbbell_upper_lower_4", "barbell_no_rack_4", "barbell_upper_lower_4",
        "maul_5", "dumbbell_split_5",
        "built_different_ppl_6",
    }
    for key in garmincoach_default_keys:
        rests = {e["rest_seconds"] for s in PROGRAMS[key]["sessions"] for e in s["exercises"]}
        assert rests == {60}, f"{key}: expected only 60s rest (GarminCoach default), got {rests}"

    # whole_body_toning_3: source prescribes 30-45 s rest; upper bound (45 s) applied.
    wbt_rests = {e["rest_seconds"] for s in PROGRAMS["whole_body_toning_3"]["sessions"] for e in s["exercises"]}
    assert wbt_rests == {45}, f"whole_body_toning_3: expected 45s rest (source-defined upper bound), got {wbt_rests}"

    # optimized_volume_4: source is silent on rest; 60 s GarminCoach default applied.
    ovw_rests = {e["rest_seconds"] for s in PROGRAMS["optimized_volume_4"]["sessions"] for e in s["exercises"]}
    assert ovw_rests == {60}, f"optimized_volume_4: expected only 60s rest (GarminCoach default), got {ovw_rests}"

    # muscle_mania_6: source specifies 60 s compound, 45 s isolation (source-defined, not GarminCoach default).
    mm_rests = {e["rest_seconds"] for s in PROGRAMS["muscle_mania_6"]["sessions"] for e in s["exercises"]}
    assert mm_rests == {60, 45}, f"muscle_mania_6: expected 60s (compound) and 45s (isolation) rest, got {mm_rests}"


def test_ppl_6_all_exercises_have_single_90s_rest_no_transition():
    """ppl_6 has rest_seconds == 90 on every exercise and no transition_rest_seconds field."""
    for session in PROGRAMS["ppl_6"]["sessions"]:
        for exercise in session["exercises"]:
            assert exercise["rest_seconds"] == 90
            assert "transition_rest_seconds" not in exercise
            assert "superset_group" not in exercise




def test_expansion_routines_have_two_weekly_lower_push_pull_exposures():
    """All expansion routines satisfy the catalog's mandatory two-exposure gate."""
    expansion_keys = [
        "planet_fitness_full_body_3", "whole_body_toning_3",
        "planet_fitness_upper_lower_4", "optimized_volume_4", "phul_4",
        "dumbbell_upper_lower_4", "barbell_no_rack_4", "barbell_upper_lower_4",
        "maul_5", "dumbbell_split_5", "built_different_ppl_6", "muscle_mania_6",
    ]
    for key in expansion_keys:
        program = PROGRAMS[key]
        assert all(count >= 2 for count in program["region_exposures"].values()), (
            f"{key}: region_exposures gate failed: {program['region_exposures']}"
        )


def test_every_expansion_routine_is_garmin_representable():
    """Every exercise in every expansion routine maps to the Garmin exercise catalog."""
    expansion_keys = [
        "planet_fitness_full_body_3", "whole_body_toning_3",
        "planet_fitness_upper_lower_4", "optimized_volume_4", "phul_4",
        "dumbbell_upper_lower_4", "barbell_no_rack_4", "barbell_upper_lower_4",
        "maul_5", "dumbbell_split_5", "built_different_ppl_6", "muscle_mania_6",
    ]
    missing = [
        (key, session["name"], exercise["exercise_name"])
        for key in expansion_keys
        for session in PROGRAMS[key]["sessions"]
        for exercise in session["exercises"]
        if exercise_metadata(exercise["exercise_name"]) is None
    ]
    assert missing == [], f"Unmapped exercises: {missing}"


def test_expansion_routine_warmup_anchors():
    """First exercise of each expansion-routine session that is a qualifying compound gets a warm-up."""
    for key in [
        "phul_4", "muscle_mania_6",
        "barbell_upper_lower_4", "barbell_no_rack_4",
    ]:
        for session in PROGRAMS[key]["sessions"]:
            first = session["exercises"][0]
            if first["garmin_category"] in {
                "SQUAT", "DEADLIFT", "BENCH_PRESS", "SHOULDER_PRESS", "ROW", "PULL_UP",
            }:
                assert first["warmup_enabled"] is True, (
                    f"{key}/{session['name']}: first compound {first['exercise_name']} should have warm-up"
                )


# ---------------------------------------------------------------------------
# Exercise-name exactness audit (catalog_for_ui comparison)
# ---------------------------------------------------------------------------

def audit_exercise_name_exactness(programs=None):
    """Return a dict with audit results comparing routine names to exact catalog_for_ui() labels.

    Keys:
        exact        – list of (prog_key, session_name, exercise_name) that exactly match a label
        non_exact    – list of (prog_key, session_name, exercise_name, resolved_label) that differ
        unmapped     – list of (prog_key, session_name, exercise_name) with no catalog resolution
    """
    if programs is None:
        programs = PROGRAMS
    exact_labels = {item["label"] for item in catalog_for_ui()}
    exact, non_exact, unmapped = [], [], []
    for prog_key, program in programs.items():
        for session in program["sessions"]:
            for exercise in session["exercises"]:
                name = exercise["exercise_name"]
                if name in exact_labels:
                    exact.append((prog_key, session["name"], name))
                else:
                    meta = exercise_metadata(name)
                    if meta is None:
                        unmapped.append((prog_key, session["name"], name))
                    else:
                        non_exact.append((prog_key, session["name"], name, meta["label"]))
    return {"exact": exact, "non_exact": non_exact, "unmapped": unmapped}


def test_no_routine_exercise_is_unmapped_in_catalog():
    """Every exercise in every routine must resolve via exercise_metadata() (no None)."""
    result = audit_exercise_name_exactness()
    assert result["unmapped"] == [], (
        f"Exercises that do not resolve to the Garmin catalog: {result['unmapped']}"
    )


def test_routine_exercise_name_exactness_audit():
    """Report non-exact routine exercise names; fail only if any are completely unmapped.

    Non-exact-but-safe-equivalent names are expected and accepted; this test
    records the current counts so regressions (new unmapped names) are detected.
    The full list of non-exact names must be reviewed manually when this count
    changes.
    """
    result = audit_exercise_name_exactness()
    total = len(result["exact"]) + len(result["non_exact"]) + len(result["unmapped"])
    assert result["unmapped"] == [], (
        f"New unmapped exercises detected: {result['unmapped']}"
    )
    non_exact_names = sorted({name for _, _, name, _ in result["non_exact"]})
    assert len(result["exact"]) == 261, (
        f"Exact-label count changed: expected 261, got {len(result['exact'])}. "
        "Review the audit before updating this number."
    )
    assert len(non_exact_names) == 131, (
        f"Unique non-exact names changed: expected 131, got {len(non_exact_names)}. "
        f"Names: {non_exact_names}"
    )


# ---------------------------------------------------------------------------
# Regression tests: alias corrections (Part A) and source substitutions (Part B)
# ---------------------------------------------------------------------------

def test_corrected_alias_dumbbell_frog_squat():
    meta = exercise_metadata("Dumbbell Frog Squat")
    assert meta and meta["key"] == "SQUAT:WIDE_STANCE_GOBLET_SQUAT", (
        f"Dumbbell Frog Squat must map to SQUAT:WIDE_STANCE_GOBLET_SQUAT, got {meta and meta['key']!r}"
    )


def test_corrected_alias_dumbbell_hip_thrust():
    meta = exercise_metadata("Dumbbell Hip Thrust")
    assert meta and meta["key"] == "HIP_RAISE:BARBELL_HIP_THRUST_WITH_BENCH", (
        f"Dumbbell Hip Thrust must map to HIP_RAISE:BARBELL_HIP_THRUST_WITH_BENCH, got {meta and meta['key']!r}"
    )


def test_corrected_alias_glute_kick_backs():
    meta = exercise_metadata("Glute Kick Backs")
    assert meta and meta["key"] == "HIP_STABILITY:QUADRUPED_HIP_EXTENSION", (
        f"Glute Kick Backs must map to HIP_STABILITY:QUADRUPED_HIP_EXTENSION, got {meta and meta['key']!r}"
    )


def test_corrected_alias_landmine_squat():
    meta = exercise_metadata("Landmine Squat")
    assert meta and meta["key"] == "SQUAT:GOBLET_SQUAT", (
        f"Landmine Squat must map to SQUAT:GOBLET_SQUAT, got {meta and meta['key']!r}"
    )


def test_corrected_alias_leg_press_calf_raise():
    meta = exercise_metadata("Leg Press Calf Raise")
    assert meta and meta["key"] == "CALF_RAISE:STANDING_CALF_RAISE", (
        f"Leg Press Calf Raise must map to CALF_RAISE:STANDING_CALF_RAISE (not seated), got {meta and meta['key']!r}"
    )


def test_corrected_alias_single_leg_good_morning():
    meta = exercise_metadata("Single Leg Good Morning")
    assert meta and meta["key"] == "LEG_CURL:SINGLE_LEG_BARBELL_GOOD_MORNING", (
        f"Single Leg Good Morning must map to LEG_CURL:SINGLE_LEG_BARBELL_GOOD_MORNING, got {meta and meta['key']!r}"
    )


def test_corrected_alias_smith_machine_row():
    meta = exercise_metadata("Smith Machine Row")
    assert meta and meta["key"] == "ROW:BENT_OVER_ROW_WITH_BARBELL", (
        f"Smith Machine Row must map to ROW:BENT_OVER_ROW_WITH_BARBELL (not seated cable), got {meta and meta['key']!r}"
    )


def _session_exercises(prog_key: str, session_name: str) -> list[dict]:
    prog = PROGRAMS[prog_key]
    for session in prog["sessions"]:
        if session["name"] == session_name:
            return session["exercises"]
    raise KeyError(f"Session {session_name!r} not found in {prog_key!r}")


def test_source_substitution_hack_squat_upper_lower_4():
    """upper_lower_4 / Lower B: Hack Squat replaced by Leg Press with source provenance note."""
    exercises = _session_exercises("upper_lower_4", "Lower B")
    names = [e["exercise_name"] for e in exercises]
    assert "Hack Squat" not in names, "Hack Squat must not remain in upper_lower_4/Lower B"
    substituted = [e for e in exercises if e["exercise_name"] == "Leg Press" and "machine hack squat" in e.get("notes", "")]
    assert substituted, "A Leg Press with note 'machine hack squat' must appear in upper_lower_4/Lower B"
    e = substituted[0]
    assert e["sets"] == 2 and e["reps"] == 12 and e["rest_seconds"] == 90


def test_source_substitution_hack_squat_and_glute_ham_raise_shul_4():
    """shul_4 / Lower Strength: Hack Squat → Leg Press and Glute Ham Raise → Swiss Ball Hip Raise And Leg Curl."""
    exercises = _session_exercises("shul_4", "Lower Strength")
    names = [e["exercise_name"] for e in exercises]
    assert "Hack Squat" not in names, "Hack Squat must not remain in shul_4/Lower Strength"
    assert "Glute Ham Raise" not in names, "Glute Ham Raise must not remain in shul_4/Lower Strength"
    lp = [e for e in exercises if e["exercise_name"] == "Leg Press" and "machine hack squat" in e.get("notes", "")]
    assert lp, "Leg Press (source: machine hack squat) must appear in shul_4/Lower Strength"
    assert lp[0]["sets"] == 3 and lp[0]["reps"] == 15 and lp[0]["rest_seconds"] == 120
    gh = [e for e in exercises if e["exercise_name"] == "Swiss Ball Hip Raise And Leg Curl"]
    assert gh, "Swiss Ball Hip Raise And Leg Curl must appear in shul_4/Lower Strength"
    assert gh[0]["sets"] == 3 and gh[0]["reps"] == 10 and gh[0]["rest_seconds"] == 120


def test_source_substitution_hack_squat_muscle_mania_6():
    """muscle_mania_6 / Lower 3: Hack Squat replaced by Leg Press with source provenance note."""
    exercises = _session_exercises("muscle_mania_6", "Lower 3")
    names = [e["exercise_name"] for e in exercises]
    assert "Hack Squat" not in names, "Hack Squat must not remain in muscle_mania_6/Lower 3"
    substituted = [e for e in exercises if e["exercise_name"] == "Leg Press" and "machine hack squat" in e.get("notes", "")]
    assert substituted, "A Leg Press with note 'machine hack squat' must appear in muscle_mania_6/Lower 3"
    e = substituted[0]
    assert e["sets"] == 4 and e["reps"] == 12


def test_source_substitution_single_arm_landmine_press_barbell_no_rack_4():
    """barbell_no_rack_4 / Upper B: Single Arm Landmine Press → Single Arm Dumbbell Shoulder Press."""
    exercises = _session_exercises("barbell_no_rack_4", "Upper B")
    names = [e["exercise_name"] for e in exercises]
    assert "Single Arm Landmine Press" not in names, (
        "Single Arm Landmine Press must not remain in barbell_no_rack_4/Upper B"
    )
    substituted = [e for e in exercises if e["exercise_name"] == "Single Arm Dumbbell Shoulder Press"]
    assert substituted, "Single Arm Dumbbell Shoulder Press must appear in barbell_no_rack_4/Upper B"
    e = substituted[0]
    assert e["sets"] == 4 and e["reps"] == 8
    assert "landmine press" in e.get("notes", "").lower(), (
        "Source provenance note must reference 'landmine press'"
    )


def test_garmin_adapted_routines_are_in_audit():
    """The four garmin_adapted routines must be classified as such in the audit doc."""
    sections = _parse_audit_sections()
    for key in ("upper_lower_4", "shul_4", "barbell_no_rack_4", "muscle_mania_6"):
        assert sections.get(key) == "garmin_adapted", (
            f"'{key}' must be classified as garmin_adapted in the audit doc, got {sections.get(key)!r}"
        )


# ---------------------------------------------------------------------------
# Audit-enforcement tests (Gate B)
# ---------------------------------------------------------------------------

import os as _os

_AUDIT_PATH = _os.path.join(_os.path.dirname(__file__), "..", "docs", "CURATED_ROUTINE_AUDIT.md")


def _audit_text() -> str:
    with open(_AUDIT_PATH, encoding="utf-8") as f:
        return f.read()


def test_audit_doc_contains_no_acsm_derived_default_label():
    """No routine in the audit may label rest as 'ACSM-derived'; use 'GarminCoach product default'."""
    assert "ACSM-derived default" not in _audit_text(), (
        "CURATED_ROUTINE_AUDIT.md still contains 'ACSM-derived default' language; "
        "correct to 'GarminCoach product default' for routines where source is silent on rest."
    )


def test_audit_doc_source_mismatch_count_is_zero():
    """The audit summary must report zero source_mismatch routines."""
    text = _audit_text()
    # The summary table must contain 'source_mismatch | 0'
    assert "| `source_mismatch` | 0 |" in text, (
        "CURATED_ROUTINE_AUDIT.md summary must show source_mismatch = 0."
    )


def test_audit_doc_source_unverified_count_is_zero():
    """The audit summary must report zero source_unverified routines."""
    text = _audit_text()
    assert "| `source_unverified` | 0 |" in text, (
        "CURATED_ROUTINE_AUDIT.md summary must show source_unverified = 0."
    )


def test_audit_doc_garmin_adapted_four_routines():
    """garmin_adapted covers exactly the four routines with no adequate Garmin catalog entry."""
    text = _audit_text()
    assert "Long Cycle Full Body" not in text, (
        "long_cycle_full_body_3 was removed; its display name must not appear in the audit."
    )
    for key in ("upper_lower_4", "shul_4", "barbell_no_rack_4", "muscle_mania_6"):
        assert key in text, f"Expected garmin_adapted routine '{key}' missing from audit."


def test_audit_doc_garmincoach_default_rest_not_attributed_to_source():
    """Routes with GarminCoach default rest must not claim it is source-defined."""
    text = _audit_text()
    # Any line that says "source-defined" must not immediately follow a GarminCoach-default-rest note
    # Pragmatic check: ensure the text does not attribute 60 s default to ACSM
    assert "60 s (ACSM" not in text, (
        "CURATED_ROUTINE_AUDIT.md must not attribute the 60 s GarminCoach default to ACSM."
    )


def test_removed_routines_are_absent_from_programs_and_plan_choices():
    """The three removed non-ACSM routines must not appear anywhere in the catalog."""
    removed = {"long_cycle_full_body_3", "powerbuilding_ppl_6", "low_volume_high_intensity_6"}
    assert not (removed & set(PROGRAMS)), f"Removed keys still in PROGRAMS: {removed & set(PROGRAMS)}"
    plan_keys = {choice["key"] for choice in PLAN_CHOICES}
    assert not (removed & plan_keys), f"Removed keys still in PLAN_CHOICES: {removed & plan_keys}"


def test_source_silent_routines_use_garmincoach_default_rest_not_higher():
    """Source-silent routines must use exactly 60 s between-set rest (GarminCoach default)."""
    source_silent = {
        "planet_fitness_full_body_3",
        "planet_fitness_upper_lower_4",
        "dumbbell_upper_lower_4", "barbell_no_rack_4", "barbell_upper_lower_4",
        "maul_5", "dumbbell_split_5",
        "built_different_ppl_6",
        "optimized_volume_4",
    }
    for key in source_silent:
        rests = {e["rest_seconds"] for s in PROGRAMS[key]["sessions"] for e in s["exercises"]}
        assert rests == {60}, (
            f"{key}: source is silent on rest; expected only 60 s (GarminCoach default), got {rests}"
        )


def test_whole_body_toning_uses_source_defined_rest():
    """whole_body_toning_3 must use 45 s rest (source-defined upper bound of 30-45 s range)."""
    rests = {
        e["rest_seconds"]
        for s in PROGRAMS["whole_body_toning_3"]["sessions"]
        for e in s["exercises"]
    }
    assert rests == {45}, f"whole_body_toning_3: expected 45 s source rest, got {rests}"


def test_muscle_mania_uses_source_defined_compound_and_isolation_rest():
    """muscle_mania_6 must use 60 s for compound and 45 s for isolation (source-defined)."""
    rests = {
        e["rest_seconds"]
        for s in PROGRAMS["muscle_mania_6"]["sessions"]
        for e in s["exercises"]
    }
    assert rests == {60, 45}, f"muscle_mania_6: expected {{60, 45}} source rest, got {rests}"


# ---------------------------------------------------------------------------
# Audit-accounting tests
# Parse CURATED_ROUTINE_AUDIT.md line-by-line using string operations only
# (no regex). Derives a list of section records then validates structural
# invariants before producing the {key: classification} mapping used by all
# accounting tests.
# ---------------------------------------------------------------------------

_VALID_CLASSIFICATIONS = {
    "source_exact",
    "source_exact_with_equivalent_names",
    "source_permitted_optional_omission",
    "garmin_adapted",
    "source_mismatch",
    "source_unverified",
}

_EXPECTED_COUNTS = {
    "source_exact": 7,
    "source_exact_with_equivalent_names": 9,
    "source_permitted_optional_omission": 1,
    "garmin_adapted": 4,
    "source_mismatch": 0,
    "source_unverified": 0,
}


# ---------------------------------------------------------------------------
# Core parser — no regex, accepts any iterable of lines
# ---------------------------------------------------------------------------

def _parse_audit_records_from_lines(lines):
    """Parse an iterable of Markdown lines into a list of section records.

    Each record:
        {
            "heading": str,           # text after "### "
            "internal_keys": list,    # every Internal key value found in this section
            "classifications": list,  # every Classification value found in this section
        }

    Rules:
    - A new section begins at a line starting with "### ".
    - Parsing of sections stops when a line whose strip equals "## Summary" is reached.
    - A table row is matched when the pipe-split second cell is **exactly** one
      backtick-delimited token (starts with "`", ends with "`", contains exactly
      two backtick characters).  This excludes descriptive rows such as:
          | **Internal key** | `PROGRAMS` dict key in `coach/programs.py` |
      which have four backtick characters in the value cell.
    - All occurrences of Internal key and Classification rows are preserved so
      that duplicates within a section are detectable.
    """
    records = []
    current = None
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        # Stop when the Summary section begins.
        if line.strip() == "## Summary":
            break
        # A new ### heading begins a new section.
        if line.startswith("### "):
            if current is not None:
                records.append(current)
            current = {
                "heading": line[4:].strip(),
                "internal_keys": [],
                "classifications": [],
            }
            continue
        # Only parse table rows inside a section.
        if current is None or not line.startswith("|"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        label_cell = parts[1].strip()
        value_cell = parts[2].strip()
        # Label cell must be **Label Name**.
        if not (label_cell.startswith("**") and label_cell.endswith("**")):
            continue
        label = label_cell[2:-2]
        # Value cell must be exactly one backtick-enclosed token.
        if not (
            value_cell.startswith("`")
            and value_cell.endswith("`")
            and value_cell.count("`") == 2
        ):
            continue
        value = value_cell[1:-1]
        if label == "Internal key":
            current["internal_keys"].append(value)
        elif label == "Classification":
            current["classifications"].append(value)
    # Flush the final section.
    if current is not None:
        records.append(current)
    return records


def _parse_audit_records():
    """Parse CURATED_ROUTINE_AUDIT.md and return the list of section records."""
    with open(_AUDIT_PATH, encoding="utf-8") as fh:
        return _parse_audit_records_from_lines(fh)


def _validate_records(records):
    """Assert all structural invariants on parsed records; return {key: classification}.

    Raises AssertionError with a descriptive message on the first violation:
    - Each section must have exactly one Internal key row.
    - Each section must have exactly one Classification row.
    - The section heading must equal the Internal key.
    - No Internal key may appear in more than one section.
    """
    seen_keys: dict[str, str] = {}  # key -> heading where it first appeared
    result: dict[str, str] = {}
    for rec in records:
        heading = rec["heading"]
        keys = rec["internal_keys"]
        clss = rec["classifications"]
        assert len(keys) == 1, (
            f"Section '{heading}': expected exactly 1 Internal key row, "
            f"got {len(keys)}: {keys}"
        )
        assert len(clss) == 1, (
            f"Section '{heading}': expected exactly 1 Classification row, "
            f"got {len(clss)}: {clss}"
        )
        key = keys[0]
        assert heading == key, (
            f"Section heading '{heading}' disagrees with Internal key '{key}'"
        )
        assert key not in seen_keys, (
            f"Internal key '{key}' appears in both section "
            f"'{seen_keys[key]}' and '{heading}'"
        )
        seen_keys[key] = heading
        result[key] = clss[0]
    return result


def _parse_audit_sections() -> dict[str, str]:
    """Return {key: classification} after all structural invariants have been asserted."""
    return _validate_records(_parse_audit_records())


# ---------------------------------------------------------------------------
# Summary parsers
# ---------------------------------------------------------------------------

def _parse_summary_counts() -> dict[str, int]:
    """Return {classification: count} from the Summary table."""
    counts: dict[str, int] = {}
    in_summary = False
    with open(_AUDIT_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.strip() == "## Summary":
                in_summary = True
                continue
            if in_summary and line.startswith("## ") and "Summary" not in line:
                break
            if in_summary and line.startswith("|") and "`" in line:
                parts = line.split("`")
                if len(parts) >= 3:
                    cls = parts[1].strip()
                    if cls in _VALID_CLASSIFICATIONS:
                        after = parts[2].lstrip(" |").split()[0].rstrip(",")
                        try:
                            counts[cls] = int(after)
                        except ValueError:
                            pass
    return counts


def _parse_summary_key_lists() -> dict[str, set[str]]:
    """Return {classification: set_of_keys} from the parenthesised lists in the Summary table."""
    key_lists: dict[str, set[str]] = {}
    in_summary = False
    with open(_AUDIT_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.strip() == "## Summary":
                in_summary = True
                continue
            if in_summary and line.startswith("## ") and "Summary" not in line:
                break
            if in_summary and line.startswith("|") and "`" in line:
                parts = line.split("`")
                if len(parts) < 3:
                    continue
                cls = parts[1].strip()
                if cls not in _VALID_CLASSIFICATIONS:
                    continue
                paren_start = line.find("(")
                paren_end = line.rfind(")")
                if paren_start == -1 or paren_end == -1:
                    key_lists[cls] = set()
                    continue
                raw = line[paren_start + 1:paren_end]
                keys: set[str] = set()
                for token in raw.split(","):
                    token = token.strip().split(" ")[0].split("\u2014")[0].strip()
                    if token:
                        keys.add(token)
                key_lists[cls] = keys
    return key_lists


def _check_summary_key_lists_match(
    sections: dict[str, str],
    summary_lists: dict[str, set[str]],
) -> list[str]:
    """Return a list of mismatch error messages (empty when everything matches)."""
    errors = []
    for cls, summary_keys in summary_lists.items():
        if not summary_keys:
            continue
        section_keys = {k for k, v in sections.items() if v == cls}
        missing = sorted(section_keys - summary_keys)
        extra = sorted(summary_keys - section_keys)
        if missing:
            errors.append(f"'{cls}' summary list missing keys present in sections: {missing}")
        if extra:
            errors.append(f"'{cls}' summary list has keys not in sections: {extra}")
    return errors


# ---------------------------------------------------------------------------
# Structural invariant tests (operate on raw records from the real audit doc)
# ---------------------------------------------------------------------------

def test_audit_contains_exactly_21_routine_sections():
    """The audit must have exactly 21 routine sections."""
    records = _parse_audit_records()
    assert len(records) == 21, (
        f"Expected 21 sections; found {len(records)}: {[r['heading'] for r in records]}"
    )


def test_audit_each_section_has_exactly_one_internal_key_row():
    """Every section must contain exactly one Internal key row."""
    records = _parse_audit_records()
    bad = [r for r in records if len(r["internal_keys"]) != 1]
    assert not bad, (
        "Sections with wrong number of Internal key rows: "
        + str({r["heading"]: r["internal_keys"] for r in bad})
    )


def test_audit_each_section_has_exactly_one_classification_row():
    """Every section must contain exactly one Classification row."""
    records = _parse_audit_records()
    bad = [r for r in records if len(r["classifications"]) != 1]
    assert not bad, (
        "Sections with wrong number of Classification rows: "
        + str({r["heading"]: r["classifications"] for r in bad})
    )


def test_audit_section_heading_matches_internal_key():
    """Each section heading must equal its Internal key value."""
    records = _parse_audit_records()
    mismatches = [
        (r["heading"], r["internal_keys"])
        for r in records
        if len(r["internal_keys"]) == 1 and r["heading"] != r["internal_keys"][0]
    ]
    assert not mismatches, (
        f"Heading/key mismatches: {mismatches}"
    )


# ---------------------------------------------------------------------------
# Semantic accounting tests (use validated {key: classification} mapping)
# ---------------------------------------------------------------------------

def test_every_programs_key_appears_exactly_once_in_audit():
    """Every key in PROGRAMS must appear exactly once in the audit."""
    sections = _parse_audit_sections()
    missing = sorted(set(PROGRAMS) - set(sections))
    extra = sorted(set(sections) - set(PROGRAMS))
    assert not missing, f"Keys in PROGRAMS missing from audit: {missing}"
    assert not extra, f"Keys in audit not in PROGRAMS: {extra}"


def test_every_audit_section_has_a_valid_classification():
    """Every routine section must carry a valid classification."""
    sections = _parse_audit_sections()
    invalid = {k: v for k, v in sections.items() if v not in _VALID_CLASSIFICATIONS}
    assert not invalid, (
        f"Audit sections with invalid classification: {invalid}"
    )


def test_audit_section_classification_counts_match_expected():
    """Counts derived from individual sections must match the required totals."""
    sections = _parse_audit_sections()
    from collections import Counter
    derived = Counter(sections.values())
    for cls, expected_count in _EXPECTED_COUNTS.items():
        actual = derived.get(cls, 0)
        assert actual == expected_count, (
            f"Classification '{cls}': expected {expected_count}, got {actual}. "
            f"Keys: {sorted(k for k, v in sections.items() if v == cls)}"
        )


def test_audit_section_counts_sum_to_21():
    """All classification counts must sum to len(PROGRAMS) == 21."""
    sections = _parse_audit_sections()
    assert len(sections) == len(PROGRAMS) == 21, (
        f"Section count {len(sections)} != PROGRAMS count {len(PROGRAMS)}"
    )


def test_audit_summary_counts_match_section_derived_counts():
    """Summary-table counts must equal counts derived from individual sections."""
    sections = _parse_audit_sections()
    from collections import Counter
    derived = Counter(sections.values())
    summary = _parse_summary_counts()
    for cls in _VALID_CLASSIFICATIONS:
        derived_count = derived.get(cls, 0)
        summary_count = summary.get(cls, 0)
        assert derived_count == summary_count, (
            f"Classification '{cls}': sections say {derived_count}, "
            f"summary says {summary_count}. "
            "Fix either the individual section or the summary table."
        )


def test_audit_summary_key_lists_match_section_derived_classifications():
    """Parenthesised key lists in the summary must match individual section classifications."""
    sections = _parse_audit_sections()
    summary_lists = _parse_summary_key_lists()
    errors = _check_summary_key_lists_match(sections, summary_lists)
    assert not errors, "\n".join(errors)


def test_maul_5_and_dumbbell_split_5_in_source_exact_summary():
    """maul_5 and dumbbell_split_5 must appear in the source_exact summary list."""
    summary_lists = _parse_summary_key_lists()
    source_exact_keys = summary_lists.get("source_exact", set())
    assert "maul_5" in source_exact_keys, (
        f"'maul_5' missing from source_exact summary list; found: {sorted(source_exact_keys)}"
    )
    assert "dumbbell_split_5" in source_exact_keys, (
        f"'dumbbell_split_5' missing from source_exact summary list; found: {sorted(source_exact_keys)}"
    )


# ---------------------------------------------------------------------------
# Parser mutation tests — inline Markdown, no file I/O
# These prove that malformed documents are rejected rather than silently
# producing wrong results.
# ---------------------------------------------------------------------------

def _lines(text):
    """Split a multi-line string into a list of lines, each ending with \\n."""
    return [ln + "\n" for ln in text.splitlines()]


def _mk_section(heading, key=None, cls="source_exact"):
    """Minimal well-formed routine section Markdown."""
    if key is None:
        key = heading
    return (
        f"### {heading}\n"
        f"| **Internal key** | `{key}` |\n"
        f"| **Classification** | `{cls}` |\n"
    )


def test_mutation_duplicate_key_across_sections():
    """The same Internal key in two different sections must be rejected.

    Both sections have matching heading and key so the heading/key check passes,
    but the cross-section duplicate check must fire.
    """
    # Two structurally valid sections that share the same key.
    text = _mk_section("routine_a") + _mk_section("routine_a")
    records = _parse_audit_records_from_lines(_lines(text))
    with pytest.raises(AssertionError, match="appears in both section"):
        _validate_records(records)


def test_mutation_duplicate_internal_key_rows_in_section():
    """Two Internal key rows inside one section must be rejected."""
    text = (
        "### routine_a\n"
        "| **Internal key** | `routine_a` |\n"
        "| **Internal key** | `routine_a` |\n"
        "| **Classification** | `source_exact` |\n"
    )
    records = _parse_audit_records_from_lines(_lines(text))
    with pytest.raises(AssertionError, match="exactly 1 Internal key row"):
        _validate_records(records)


def test_mutation_duplicate_classification_rows_in_section():
    """Two Classification rows inside one section must be rejected."""
    text = (
        "### routine_a\n"
        "| **Internal key** | `routine_a` |\n"
        "| **Classification** | `source_exact` |\n"
        "| **Classification** | `garmin_adapted` |\n"
    )
    records = _parse_audit_records_from_lines(_lines(text))
    with pytest.raises(AssertionError, match="exactly 1 Classification row"):
        _validate_records(records)


def test_mutation_missing_classification_row():
    """A section with no Classification row must be rejected."""
    text = (
        "### routine_a\n"
        "| **Internal key** | `routine_a` |\n"
    )
    records = _parse_audit_records_from_lines(_lines(text))
    with pytest.raises(AssertionError, match="exactly 1 Classification row"):
        _validate_records(records)


def test_mutation_missing_internal_key_row():
    """A section with no Internal key row must be rejected."""
    text = (
        "### routine_a\n"
        "| **Classification** | `source_exact` |\n"
    )
    records = _parse_audit_records_from_lines(_lines(text))
    with pytest.raises(AssertionError, match="exactly 1 Internal key row"):
        _validate_records(records)


def test_mutation_heading_disagrees_with_key():
    """A section heading that does not match its Internal key must be rejected."""
    text = (
        "### routine_a\n"
        "| **Internal key** | `routine_b` |\n"
        "| **Classification** | `source_exact` |\n"
    )
    records = _parse_audit_records_from_lines(_lines(text))
    with pytest.raises(AssertionError, match="disagrees with Internal key"):
        _validate_records(records)


def test_mutation_unknown_classification():
    """A section with an unknown classification must be caught by the validity check."""
    text = _mk_section("routine_a", cls="totally_wrong")
    records = _parse_audit_records_from_lines(_lines(text))
    sections = _validate_records(records)  # structural validation passes
    invalid = {k: v for k, v in sections.items() if v not in _VALID_CLASSIFICATIONS}
    assert invalid, (
        "Expected 'totally_wrong' to be flagged as an invalid classification"
    )


def test_mutation_summary_missing_key():
    """A summary list that omits a key present in sections must be detected."""
    sections = {"key_a": "source_exact", "key_b": "source_exact"}
    summary_lists = {"source_exact": {"key_a"}}  # key_b deliberately omitted
    errors = _check_summary_key_lists_match(sections, summary_lists)
    assert errors, "Expected an error for key_b missing from summary"
    assert any("key_b" in e for e in errors), f"key_b not mentioned in errors: {errors}"


def test_mutation_summary_extra_key():
    """A summary list that contains a key absent from sections must be detected."""
    sections = {"key_a": "source_exact"}
    summary_lists = {"source_exact": {"key_a", "key_c"}}  # key_c is extra
    errors = _check_summary_key_lists_match(sections, summary_lists)
    assert errors, "Expected an error for key_c extra in summary"
    assert any("key_c" in e for e in errors), f"key_c not mentioned in errors: {errors}"
