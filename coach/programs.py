"""Curated, source-audited gym routines used by onboarding."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from coach.exercises import CATALOG_VERSION, exercise_key, exercise_metadata


ACSM_SOURCE_URL = "https://acsm.org/resistance-training-guidelines-update-2026/"
ACSM_ATTRIBUTION = "Muscle & Strength routine reviewed against ACSM 2026 resistance-training guidance."
ROUTINE_CATALOG_VERSION = f"{CATALOG_VERSION}+default-routines-2026-07-22-25"

# These are direct translations of Muscle & Strength's published Training
# Level. Never infer or relabel a routine from its exercises or frequency.
EXPERIENCE_BADGES = {
    "new": {"label": "Beginner", "slug": "beginner"},
    "six_to_twenty_four_months": {"label": "Intermediate", "slug": "intermediate"},
    "two_plus_years": {"label": "Advanced", "slug": "advanced"},
    "returning": {"label": "Beginner", "slug": "beginner"},
}


# Only these compound categories qualify for the 20-minute return rule.
_WARMUP_COMPOUND_CATEGORIES = {
    "SQUAT", "LUNGE", "DEADLIFT", "HIP_RAISE", "BENCH_PRESS",
    "SHOULDER_PRESS", "PULL_UP", "ROW",
}
# Abdominal work is deliberately performed after the session's compounds and
# accessories.  It therefore has no separate rehearsal set: the trunk and
# overall body temperature have already been prepared by the preceding work.
# Keep CARRY out of this set, since loaded carries are not abdominal isolation.
_ABDOMINAL_CATEGORIES = {"CHOP", "CORE", "CRUNCH", "LEG_RAISE", "PLANK", "SIT_UP"}
_MAJOR_REGION_BY_MUSCLE_GROUP = {
    "quads": "lower_body",
    "hamstrings_glutes": "lower_body",
    "chest": "chest",
    "back": "back",
    "shoulders": "shoulders",
}


def warmup_defaults(
    name: str,
    meta: dict[str, Any] | None,
    reps: int | None,
    duration_seconds: int | None = None,
    weight_kg: float | None = None,
) -> dict[str, Any]:
    """Return a rep-based warm-up's editable defaults, including bodyweight work."""
    if (
        reps is None
        or duration_seconds is not None
        or (meta or {}).get("category") in _ABDOMINAL_CATEGORIES
    ):
        return {
            "warmup_enabled": False,
            "warmup_reps": None,
            "warmup_duration_seconds": None,
            "warmup_weight_kg": None,
        }
    return {
        "warmup_enabled": True,
        "warmup_reps": 8,
        "warmup_duration_seconds": None,
        "warmup_weight_kg": round(weight_kg * 0.5, 1) if weight_kg else None,
    }


def _without_warmup(exercise: dict[str, Any]) -> None:
    exercise.update({
        "warmup_enabled": False,
        "warmup_reps": None,
        "warmup_duration_seconds": None,
        "warmup_weight_kg": None,
    })


def _estimated_exercise_seconds(exercise: dict[str, Any]) -> int:
    """Conservative planning estimate used only for the 20-minute re-entry rule."""
    if exercise["duration_seconds"]:
        return exercise["sets"] * exercise["duration_seconds"] + max(exercise["sets"] - 1, 0) * exercise["rest_seconds"]
    return exercise["sets"] * 60 + max(exercise["sets"] - 1, 0) * exercise["rest_seconds"]


def _is_heavy_compound(exercise: dict[str, Any]) -> bool:
    meta = exercise_metadata(exercise["exercise_name"])
    return bool(
        meta
        and meta.get("category") in _WARMUP_COMPOUND_CATEGORIES
        and exercise["sets"] >= 4
        and (exercise["reps"] or 0) <= 10
    )


def _major_region(meta: dict[str, Any] | None, exercise_name: str) -> str | None:
    # Garmin's catalog currently represents plain "Leg Extension" as a banded
    # exercise. Preserve the source routine's actual lower-body joint context
    # for warm-up planning rather than inheriting that catalog category.
    normalized_name = exercise_name.upper()
    if "LEG EXTENSION" in normalized_name or "LEG CURL" in normalized_name:
        return "lower_body"
    return _MAJOR_REGION_BY_MUSCLE_GROUP.get((meta or {}).get("muscle_group"))


def _joint_systems(meta: dict[str, Any] | None, region: str | None) -> set[str]:
    """Joint systems substantially loaded by an exercise, including stabilizers."""
    systems = {
        "lower_body": {"lower_body"},
        "chest": {"shoulder_complex"},
        "shoulders": {"shoulder_complex"},
        "back": {"back_grip"},
    }.get(region, set()).copy()
    # A deadlift is primarily lower body, but its lats and grip are also loaded
    # hard enough to count as back/grip preparation for a later pulldown or row.
    if (meta or {}).get("category") == "DEADLIFT":
        systems.add("back_grip")
    return systems


def _primary_joint_system(region: str | None) -> str | None:
    return {
        "lower_body": "lower_body",
        "chest": "shoulder_complex",
        "shoulders": "shoulder_complex",
        "back": "back_grip",
    }.get(region)


def _direct_press_region(meta: dict[str, Any] | None, exercise_name: str) -> str | None:
    """Return the primary region for direct presses, distinct from stabilization."""
    category = (meta or {}).get("category")
    if category == "SHOULDER_PRESS":
        return "shoulders"
    if category in {"BENCH_PRESS", "PUSH_UP"} or "DIP" in exercise_name.upper():
        return "chest"
    return None


def _apply_session_warmups(exercises: list[dict[str, Any]]) -> None:
    """Apply the daily-anchor, cold-joint, flow, and 20-minute return rules."""
    touched_regions: set[str] = set()
    joint_touched_at: dict[str, int] = {}
    elapsed_seconds = 0
    previous_joint_systems: set[str] = set()
    direct_press_regions_seen: set[str] = set()
    for index, exercise in enumerate(exercises):
        meta = exercise_metadata(exercise["exercise_name"])
        defaults = warmup_defaults(
            exercise["exercise_name"], meta, exercise["reps"], exercise["duration_seconds"], exercise["weight_kg"],
        )
        _without_warmup(exercise)
        region = _major_region(meta, exercise["exercise_name"])
        joint_systems = _joint_systems(meta, region)
        primary_joint_system = _primary_joint_system(region)
        can_warm_up = defaults["warmup_enabled"]
        is_anchor = index == 0
        is_new_major_region = region is not None and region not in touched_regions
        has_cold_joint = not (joint_systems & joint_touched_at.keys())
        is_back_to_back_flow = bool(joint_systems & previous_joint_systems)
        direct_press_region = _direct_press_region(meta, exercise["exercise_name"])
        is_first_direct_press = direct_press_region is not None and direct_press_region not in direct_press_regions_seen
        last_touched = joint_touched_at.get(primary_joint_system) if primary_joint_system else None
        is_long_break_return = bool(
            _is_heavy_compound(exercise)
            and last_touched is not None
            and elapsed_seconds - last_touched >= 20 * 60
        )
        if can_warm_up and not is_back_to_back_flow and (
            is_anchor or (is_new_major_region and has_cold_joint) or is_first_direct_press or is_long_break_return
        ):
            exercise.update(defaults)
        exercise_end = elapsed_seconds + _estimated_exercise_seconds(exercise)
        touched_regions.update({region} if region else set())
        for system in joint_systems:
            joint_touched_at[system] = exercise_end
        if direct_press_region:
            direct_press_regions_seen.add(direct_press_region)
        previous_joint_systems = joint_systems
        elapsed_seconds = exercise_end


def _exercise(
    name: str,
    sets: int,
    reps: int | None,
    movement: str | None = None,
    rest: int = 60,
    notes: str = "",
    duration: int | None = None,
) -> dict[str, Any]:
    meta = exercise_metadata(name)
    exercise = {
        "exercise_name": name,
        "exercise_key": exercise_key(name),
        "sets": sets,
        "reps": reps,
        "weight_kg": None,
        "duration_seconds": duration,
        "rest_seconds": rest,
        "movement_pattern": movement or (meta or {}).get("movement_pattern", "other"),
        "garmin_category": (meta or {}).get("category"),
        "garmin_name": (meta or {}).get("garmin_name"),
        "is_generic": meta is None,
        "notes": notes,
    }
    exercise.update(warmup_defaults(name, meta, reps, duration))
    return exercise


def _session(name: str, focus: str, exercises: list[dict], duration: int = 60) -> dict[str, Any]:
    _apply_session_warmups(exercises)
    return {
        "name": name,
        "sport_type": "strength_training",
        "duration_min": duration,
        "focus_tags": [focus, "strength"],
        "exercises": exercises,
    }


def _program(name: str, source: str, level: str, sessions: list[dict], review: str) -> dict:
    weekly_sets: dict[str, int] = {}
    exposures: dict[str, int] = {}
    for session in sessions:
        seen: set[str] = set()
        for exercise in session["exercises"]:
            pattern = exercise["movement_pattern"]
            weekly_sets[pattern] = weekly_sets.get(pattern, 0) + exercise["sets"]
            seen.add(pattern)
        for pattern in seen:
            exposures[pattern] = exposures.get(pattern, 0) + 1
    def exposes(session: dict, region: str) -> bool:
        patterns = {exercise["movement_pattern"] for exercise in session["exercises"]}
        # ACSM's major-muscle-group frequency guidance must be demonstrated by
        # the exercises themselves. A focus label or arm isolation must not let
        # an otherwise incomplete day pass the catalog gate.
        relevant = {
            "lower": {"knee_dominant", "hip_hinge"},
            "push": {"horizontal_push", "vertical_push"},
            "pull": {"horizontal_pull", "vertical_pull"},
        }
        return bool(patterns & relevant[region])
    regions = {region: sum(exposes(session, region) for session in sessions) for region in ("lower", "push", "pull")}
    if any(count < 2 for count in regions.values()):
        raise ValueError(f"{name} does not provide two weekly lower, push, and pull exposures")
    return {
        "name": name,
        "source_url": source,
        "experience": level,
        "sessions": sessions,
        "volume_review": review,
        "catalog_version": ROUTINE_CATALOG_VERSION,
        "weekly_sets": weekly_sets,
        "weekly_exposures": exposures,
        "region_exposures": regions,
    }


PROGRAMS: dict[str, dict[str, Any]] = {
    "full_body_2": _program(
        "A/B Full Body · 2 days",
        "https://www.muscleandstrength.com/workouts/a-b-2-day-workout-for-busy-people",
        "new",
        [
            _session("Full Body 1", "full body", [
                _exercise("Trap Bar Deadlift", 5, 6, rest=120), _exercise("Military Press", 5, 6, rest=120),
                _exercise("Lat Pull Down", 4, 12, rest=120), _exercise("T Bar Row", 4, 12, rest=120),
                _exercise("Push Up", 3, None, rest=120, notes="AMRAP; leave one rep in reserve"),
            ], 90),
            _session("Full Body 2", "full body", [
                _exercise("Front Squat", 5, 6, rest=120), _exercise("Dumbbell Bench Press", 5, 8, rest=120),
                _exercise("Chin Up", 4, 8, rest=120), _exercise("Cable Row", 4, 12, rest=120),
                _exercise("Bodyweight Hip Thrust", 3, 12, rest=120),
            ], 90),
        ],
        "Two full-body exposures; 13+ direct/indirect weekly sets for the major regions.",
    ),
    "beginner_full_body_3": _program(
        "Beginner Full Body · 3 days",
        "https://www.muscleandstrength.com/workouts/3-day-workout-routine-and-diet-for-beginners",
        "new",
        [
            _session("Full Body 1", "full body", [
                _exercise("Trap Bar Deadlift", 3, 5, rest=300), _exercise("Bent Over Row", 3, 8, rest=90),
                _exercise("Dumbbell Overhead Press", 3, 10, rest=90), _exercise("Lat Pull Down", 3, 10, rest=90),
                _exercise("Dumbbell Bicep Curl", 2, 12, rest=45), _exercise("Rope Pressdown", 2, 12, rest=45),
                _exercise("Plank", 2, None, rest=45, notes="Hold to technical failure"),
            ], 75),
            _session("Full Body 2", "full body", [
                _exercise("Front Squat", 3, 5, rest=300), _exercise("Romanian Deadlift", 3, 10, rest=90),
                _exercise("Farmer's Carry", 3, None, movement="core", rest=45, duration=45),
                _exercise("Dumbbell Row", 3, 15, rest=90), _exercise("Incline Dumbbell Bench Press", 3, 12, rest=90),
                _exercise("Calf Raise", 3, 20, rest=45), _exercise("Dead Bugs", 2, 12, rest=45, notes="Each side"),
            ], 75),
            _session("Full Body 3", "full body", [
                _exercise("Bench Press", 3, 5, rest=300), _exercise("Leg Press", 3, 10, rest=90),
                _exercise("Leg Curl", 3, 12, rest=90), _exercise("Lateral Raise", 3, 15, rest=45),
                _exercise("Cable Row", 3, 12, rest=90), _exercise("EZ Bar Curl", 2, 12, rest=45),
                _exercise("Skullcrusher", 2, 12, rest=45), _exercise("Pallof Press", 2, 12, rest=45, notes="Each side"),
            ], 75),
        ],
        "Three full-body sessions with at least two weekly exposures per major region.",
    ),
    "ms_full_body_3": _program(
        "M&S Full Body · 3 days",
        "https://www.muscleandstrength.com/workouts/muscle-strength-full-body-workout-routine",
        "new",
        [
            _session("Full Body 1", "full body", [
                _exercise("Squat", 3, 5, rest=120), _exercise("Bench Press", 3, 5, rest=120),
                _exercise("Barbell Row", 3, 5, rest=120), _exercise("Upright Row", 3, 10, rest=90),
                _exercise("Skullcrushers", 3, 10, rest=90), _exercise("Dumbbell Curls", 3, 10, rest=90),
                _exercise("Leg Curls", 3, 15, rest=90), _exercise("Ab Wheel Roll Out", 3, 15, rest=90),
            ], 90),
            _session("Full Body 2", "full body", [
                _exercise("Deadlift", 1, 5, rest=120), _exercise("Romanian Deadlift", 2, 12, rest=90),
                _exercise("Seated Overhead Press", 3, 10, rest=120), _exercise("Pull Ups", 3, 15, rest=90),
                _exercise("Dips", 3, 20, movement="vertical_push", rest=90), _exercise("Barbell Shrugs", 3, 10, rest=90),
                _exercise("Seated Calf Raise", 3, 15, rest=90), _exercise("Plank", 3, None, rest=90, duration=60),
            ], 90),
            _session("Full Body 3", "full body", [
                _exercise("Squat", 1, 5, rest=120), _exercise("Squat", 1, 20, rest=120, notes="Back-off set"),
                _exercise("Incline Dumbbell Bench Press", 3, 10, rest=90), _exercise("One Arm Dumbbell Row", 3, 15, rest=90),
                _exercise("Seated Arnold Press", 3, 15, rest=90), _exercise("Cable Tricep Extensions", 3, 10, rest=90),
                _exercise("Barbell Curls", 3, 10, rest=90), _exercise("Leg Curls", 3, 15, rest=90),
                _exercise("Ab Wheel Roll Out", 3, 15, rest=90),
            ], 90),
        ],
        "Three full-body sessions; source ramp-up sets are replaced by the app's single movement warm-up.",
    ),
    "total_package_3": _program(
        "Total Package · 3 days",
        "https://www.muscleandstrength.com/workouts/total-package-workout",
        "six_to_twenty_four_months",
        [
            _session("Full Body 1", "full body", [
                _exercise("Squat", 5, 5, rest=180), _exercise("Dumbbell Bench Press", 4, 10),
                _exercise("Dumbbell Row", 4, 10), _exercise("Seated Dumbbell Press", 4, 10),
                _exercise("Lunge", 4, 10), _exercise("Dumbbell Curl", 3, 10),
                _exercise("Standing Barbell Tricep Extension", 3, 10), _exercise("Calf Raise", 3, 12),
                _exercise("Plank", 5, None, duration=20),
            ], 90),
            _session("Full Body 2", "full body", [
                _exercise("Bench Press", 5, 5, rest=180), _exercise("Machine Pec Deck", 3, 12),
                _exercise("Leg Extension", 4, 10), _exercise("Leg Curl", 4, 10),
                _exercise("Pullup", 4, 10), _exercise("Seated Lateral Raise", 4, 10),
                _exercise("Dumbbell Hammer Curls", 3, 10), _exercise("Rope Extension", 3, 10),
                _exercise("Plank", 5, None, duration=20),
            ], 90),
            _session("Full Body 3", "full body", [
                _exercise("Deadlift", 5, 5, rest=180), _exercise("Incline Dumbbell Press", 4, 10),
                _exercise("Lateral Raise", 4, 10), _exercise("Pulldown", 4, 10),
                _exercise("Leg Press", 4, 10), _exercise("EZ Bar Curl", 3, 10),
                _exercise("Skullcrushers", 3, 10), _exercise("Dumbbell Shrugs", 3, 12),
                _exercise("Plank", 5, None, duration=20),
            ], 90),
        ],
        "High-volume full-body routine with three weekly exposures; recommend only to established trainees.",
    ),
    "upper_lower_4": _program(
        "Upper / Lower Bodybuilding · 4 days",
        "https://www.muscleandstrength.com/workouts/upper-lower-4-day-gym-bodybuilding-workout",
        "new",
        [
            _session("Upper A", "upper body", [
                _exercise("Bench Press", 3, 12, rest=90), _exercise("Barbell Row", 3, 12, rest=90), _exercise("Seated Overhead Dumbbell Press", 3, 12, rest=90),
                _exercise("Pec Deck", 2, 12), _exercise("V-Bar Lat Pull Down", 2, 12, rest=90), _exercise("Side Lateral Raise", 2, 15),
                _exercise("Cable Tricep Extensions", 3, 12), _exercise("Cable Curls", 3, 12),
            ]),
            _session("Lower A", "lower body", [
                _exercise("Squat", 3, 12, rest=90), _exercise("Stiff Leg Deadlifts", 3, 12, rest=90), _exercise("Standing Calf Raise", 3, 15),
                _exercise("Leg Extension", 2, 12), _exercise("Leg Curl", 2, 12), _exercise("Seated Calf Raise", 2, 12),
                _exercise("Cable Crunch", 3, 12), _exercise("Cable Pull Through", 3, 12),
            ]),
            _session("Upper B", "upper body", [
                _exercise("Incline Dumbbell Bench Press", 3, 12, rest=90), _exercise("Rack Deadlifts", 3, 8, rest=90), _exercise("Military Press", 3, 12, rest=90),
                _exercise("Machine Chest Press", 2, 12, rest=90), _exercise("Machine Row", 2, 12, rest=90), _exercise("Machine Shoulder Press", 2, 12, rest=90),
                _exercise("Dumbbell Curls", 3, 12), _exercise("Machine Tricep Dip", 3, 12),
            ]),
            _session("Lower B", "lower body", [
                _exercise("Leg Press", 3, 20, rest=90), _exercise("Dumbbell Stiff Leg Deadlift", 3, 12, rest=90), _exercise("Leg Press Calf Raise", 3, 15),
                _exercise("Hack Squat", 2, 12, rest=90), _exercise("Seated Leg Curl", 2, 12), _exercise("Seated Calf Raise", 2, 12),
                _exercise("Plank", 3, None, duration=60), _exercise("Hyperextension", 3, 12),
            ]),
        ],
        "Two upper and two lower exposures with source-prescribed volume.",
    ),
    "shul_4": _program(
        "SHUL Strength / Hypertrophy · 4 days",
        "https://www.muscleandstrength.com/workouts/shul-workout",
        "six_to_twenty_four_months",
        [
            _session("Lower Strength", "lower body", [
                _exercise("Front Squat", 3, 5, rest=300), _exercise("Trap Bar Deadlift", 3, 5, rest=300),
                _exercise("Hack Squat", 3, 15, rest=120), _exercise("Glute Ham Raise", 3, 10, movement="hip_hinge", rest=120),
                _exercise("Seated Calf Raise", 4, 10, rest=120),
            ]),
            _session("Upper Strength", "upper body", [
                _exercise("Dumbbell Bench Press", 3, 5, rest=300), _exercise("One Arm Dumbbell Row", 3, 5, rest=300),
                _exercise("Overhead Press", 3, 5, rest=300), _exercise("Pull Up", 3, 10, rest=120),
                _exercise("Incline Bench Press", 3, 10, rest=120), _exercise("Tricep Dip", 2, 10, movement="vertical_push", rest=120),
                _exercise("Farmer's Carry", 2, None, movement="core", rest=120, duration=40),
            ]),
            _session("Lower Hypertrophy", "lower body", [
                _exercise("Front Squat", 3, 12), _exercise("Dumbbell Reverse Lunge", 3, 12), _exercise("Barbell Hip Thrust", 3, 12),
                _exercise("Leg Extension", 3, 15, rest=45), _exercise("Romanian Deadlift", 3, 15), _exercise("Standing Machine Calf Raise", 3, 12, rest=45),
            ]),
            _session("Upper Hypertrophy", "upper body", [
                _exercise("Incline Dumbbell Bench Press", 3, 12), _exercise("Decline Bench Press", 3, 12),
                _exercise("Lat Pull Down", 3, 12), _exercise("Inverted Row", 3, 12), _exercise("Face Pull", 3, 12, rest=45),
                _exercise("Lateral Raise", 3, 12, rest=45), _exercise("Barbell Curl", 3, 12, rest=45), _exercise("Incline Skullcrusher", 3, 12, rest=45),
            ]),
        ],
        "Strength and hypertrophy days each expose upper and lower regions twice weekly; advanced volume.",
    ),
    "split_full_4": _program(
        "Three-Way Split + Full Body · 4 days",
        "https://www.muscleandstrength.com/workouts/4-day-workout-to-build-muscle",
        "two_plus_years",
        [
            _session("Back & Biceps", "pull", [
                _exercise("Chin Up", 3, 10, rest=45), _exercise("T-bar Machine Row", 3, 10, rest=45),
                _exercise("Close Grip Pull Down", 3, 10, rest=45), _exercise("One Arm Dumbbell Row", 3, 10, rest=45),
                _exercise("Barbell Curl", 3, 10, rest=45), _exercise("Hammer Curl", 3, 10, rest=45),
            ], 45),
            _session("Legs", "lower body", [
                _exercise("Seated Leg Curl", 4, 10, rest=45), _exercise("Barbell Squat", 3, 10, rest=45),
                _exercise("Trap Bar Deadlift", 3, 10, rest=45), _exercise("Dumbbell Stiff Legged Deadlift", 3, 10, rest=45),
                _exercise("Leg Extension", 3, 10, rest=45), _exercise("Seated Calf Raise", 2, 20, rest=45),
                _exercise("Standing Machine Calf Raise", 2, 20, rest=45),
            ], 45),
            _session("Chest, Shoulders & Triceps", "push", [
                _exercise("Standing Military Press", 3, 10, rest=45), _exercise("Side Lateral Raise", 3, 10, rest=45),
                _exercise("Face Pull", 3, 10, rest=45), _exercise("Incline Bench Press", 3, 10, rest=45),
                _exercise("Dumbbell Bench Press", 3, 10, rest=45), _exercise("Incline Skullcrusher", 3, 10, rest=45),
                _exercise("Tricep Pushdown", 3, 10, rest=45),
            ], 45),
            _session("Full Body", "full body", [
                _exercise("Deadlift", 4, 10, rest=45), _exercise("Front Squat", 3, 10, rest=45),
                _exercise("Barbell Hip Thrust", 3, 10, rest=45), _exercise("Dips", 3, 10, movement="vertical_push", rest=45),
                _exercise("Inverted Row", 3, 10, rest=45), _exercise("Push Ups", 3, 10, rest=45),
            ], 45),
        ],
        "The full-body fourth day gives every major region a second weekly exposure; advanced volume.",
    ),
    "muscle_strength_5": _program(
        "Muscle & Strength Building Split · 5 days",
        "https://www.muscleandstrength.com/workouts/5-day-muscle-and-strength-building-workout-split",
        "six_to_twenty_four_months",
        [
            _session("Upper Strength", "upper body", [
                _exercise("Weighted Wide Grip Pull Ups", 2, 6, rest=180), _exercise("Bent Over Barbell Row", 4, 6, rest=180),
                _exercise("Narrow Grip T-Bar Row", 2, 6, rest=180), _exercise("Standing Overhead Barbell Press", 4, 6, rest=180),
                _exercise("Incline Dumbbell Bench Press", 4, 6, rest=180), _exercise("Weighted Dips", 2, 6, movement="vertical_push", rest=180),
                _exercise("EZ Bar Skullcrusher", 2, 6, rest=180), _exercise("EZ Bar Bicep Curls", 2, 6, rest=180),
            ], 90),
            _session("Lower Strength", "lower body", [
                _exercise("Squats", 4, 6, rest=180), _exercise("Hack Squats", 2, 6, rest=180),
                _exercise("Deadlifts", 4, 6, rest=180), _exercise("Lying Leg Curls", 2, 6, rest=180),
                _exercise("Standing Calf Raise", 4, 6, rest=180), _exercise("Seated Calf Raise", 2, 6, rest=180),
            ], 90),
            _session("Back & Shoulders Size", "pull", [
                _exercise("Wide Grip Pull Down", 4, 12, rest=90), _exercise("Narrow Grip Pull Down", 4, 12, rest=90),
                _exercise("Chest Supported Machine Row", 4, 12, rest=90), _exercise("Narrow Grip Low Pulley Cable Row", 2, 12, rest=90),
                _exercise("Straight Arm Rope Pull Down", 2, 12, rest=90), _exercise("Lower Back Hyperextensions", 2, 12, rest=90),
                _exercise("Dumbbell Shoulder Press", 4, 12, rest=90), _exercise("Standing Dumbbell Side Lateral Raise", 2, 12, rest=90),
                _exercise("Standing EZ Bar Front Raise", 2, 12, rest=90), _exercise("Dumbbell Rear Delt Lateral Raise", 2, 12, rest=90),
                _exercise("Cable EZ Bar Upright Row", 2, 12, rest=90), _exercise("Rope Face Pull", 2, 12, rest=90),
            ], 90),
            _session("Chest & Arms Size", "push", [
                _exercise("Incline Barbell Bench Press", 4, 12, rest=90), _exercise("Flat Machine Chest Press", 2, 12, rest=90),
                _exercise("Incline Dumbbell Fly", 2, 12, rest=90), _exercise("Cable Crossover", 2, 12, rest=90),
                _exercise("Narrow Grip Bench Press", 2, 12, rest=90), _exercise("Seated Overhead EZ Bar Tricep Extension", 2, 12, rest=90),
                _exercise("Single Arm Cable Press Down", 2, 12, rest=90), _exercise("EZ Bar Preacher Curl", 2, 12, rest=90),
                _exercise("Standing Alternating Dumbbell Hammer Curl", 2, 12, rest=90), _exercise("High Pulley Single Arm Bicep Curl", 2, 12, rest=90),
            ], 90),
            _session("Legs Size", "lower body", [
                _exercise("Seated Hamstring Curl", 4, 12, rest=90), _exercise("Leg Extension", 4, 12, rest=90), _exercise("Front Squat", 4, 12, rest=90),
                _exercise("Leg Press", 4, 12, rest=90), _exercise("Barbell Walking Lunge", 4, 12, rest=90, notes="Each side"),
                _exercise("Abductor Machine", 2, 12, movement="hip_hinge", rest=90), _exercise("Adductor Machine", 2, 12, movement="hip_hinge", rest=90),
                _exercise("Glute Kick Backs", 2, 12, rest=90, notes="Each side"), _exercise("Donkey Calf Raise", 4, 12, rest=90),
                _exercise("Seated Calf Raise", 4, 12, rest=90), _exercise("Single Leg Calf Press", 4, 12, rest=90, notes="Each side"),
            ], 90),
        ],
        "Each major region has strength and size exposures; original lower-bound set ranges retained.",
    ),
    "ppl_6": _program(
        "Push / Pull / Legs A/B · 6 days",
        "https://www.muscleandstrength.com/workouts/6-day-push-pull-legs-planet-fitness-workout",
        "new",
        [
            _session("Push A", "push", [
                _exercise("Dumbbell Bench Press", 4, 12, rest=45), _exercise("Incline Smith Machine Bench Press", 3, 10, movement="horizontal_push", rest=45),
                _exercise("Dips", 3, 15, movement="vertical_push", rest=45), _exercise("Seated Arnold Press", 4, 12, rest=45),
                _exercise("Lateral Raise", 3, 15, rest=45), _exercise("Cable Overhead Tricep Extension", 4, 15, movement="elbow_extension", rest=45),
            ]),
            _session("Pull A", "pull", [
                _exercise("Dumbbell Row", 4, 12, rest=45), _exercise("Seated Cable Row", 4, 12, rest=45),
                _exercise("Pull Up", 3, 12, rest=45), _exercise("Inverted Row", 3, 12, rest=45),
                _exercise("Dumbbell Curl", 4, 15, rest=45),
            ]),
            _session("Legs A", "lower body", [
                _exercise("Leg Press", 4, 10, rest=45), _exercise("Smith Machine Front Squat", 4, 10, rest=45),
                _exercise("Dumbbell Stiff Leg Deadlift", 4, 12, rest=45), _exercise("Lying Leg Curl", 3, 12, rest=45),
                _exercise("Bodyweight Hip Thrust", 3, 15, rest=45), _exercise("Standing Calf Raise", 4, 15, rest=45),
            ]),
            _session("Push B", "push", [
                _exercise("Standing Dumbbell Press", 4, 12, movement="vertical_push", rest=45), _exercise("Seated Lateral Raise", 3, 15, rest=45),
                _exercise("Lateral Raise Machine", 3, 15, movement="vertical_push", rest=45), _exercise("Incline Dumbbell Bench Press", 4, 12, rest=45),
                _exercise("Push Ups", 4, 15, rest=45), _exercise("Lying Dumbbell Tricep Extensions", 4, 15, movement="elbow_extension", rest=45),
            ]),
            _session("Pull B", "pull", [
                _exercise("Lat Pull Down", 4, 12, rest=45), _exercise("Cable Face Pull", 4, 15, movement="horizontal_pull", rest=45),
                _exercise("Smith Machine Row", 4, 10, movement="horizontal_pull", rest=45), _exercise("Straight Arm Lat Pull Down", 4, 15, movement="vertical_pull", rest=45),
                _exercise("Cable Curl", 4, 15, rest=45),
            ]),
            _session("Legs B", "lower body", [
                _exercise("Dumbbell Rear Lunge", 4, 12, rest=45, notes="Each side"), _exercise("Goblet Squat", 4, 15, rest=45),
                _exercise("Seated Leg Curl", 3, 15, rest=45), _exercise("Dumbbell Deadlift", 3, 12, movement="hip_hinge", rest=45),
                _exercise("Glute Hyperextension", 3, 15, movement="hip_hinge", rest=45), _exercise("Leg Press Calf Raise", 4, 15, rest=45),
            ]),
        ],
        "Push, pull, and legs are each trained twice; high-frequency option for established trainees only.",
    ),
    "dumbbell_full_body_3": _program(
        "Dumbbell Full Body · 3 days",
        "https://www.muscleandstrength.com/workouts/3-day-full-body-dumbbell-workout",
        "new",
        [
            _session("Full Body 1", "full body", [
                _exercise("Dumbbell Squat", 3, 10), _exercise("Dumbbell Stiff Leg Deadlift", 3, 10),
                _exercise("Bent Over Dumbbell Row", 3, 10, movement="horizontal_pull"), _exercise("Dumbbell Bench Press", 3, 10),
                _exercise("Dumbbell Lateral Raise", 2, 8), _exercise("Standing Dumbbell Curl", 2, 8),
                _exercise("Lying Dumbbell Tricep Extension", 2, 8),
            ], 45),
            _session("Full Body 2", "full body", [
                _exercise("Dumbbell Lunge", 3, 10), _exercise("Dumbbell Hamstring Curl", 3, 10, movement="hip_hinge"),
                _exercise("Dumbbell Deadlift", 3, 10), _exercise("Dumbbell Military Press", 3, 10, movement="vertical_push"),
                _exercise("Dumbbell Fly", 2, 8, movement="horizontal_push"), _exercise("Hammer Curl", 2, 8),
                _exercise("Seated Dumbbell Tricep Extension", 2, 8),
            ], 45),
            _session("Full Body 3", "full body", [
                _exercise("Dumbbell Step Up", 3, 10), _exercise("Dumbbell Stiff Leg Deadlift", 3, 10),
                _exercise("One Arm Dumbbell Row", 3, 10), _exercise("Reverse Grip Dumbbell Press", 3, 10, movement="horizontal_push"),
                _exercise("Dumbbell Rear Delt Fly", 2, 8, movement="horizontal_pull"), _exercise("Zottman Curl", 2, 8),
                _exercise("Close Grip Dumbbell Press", 2, 8, movement="horizontal_push"),
            ], 45),
        ],
        "Repeatable dumbbell-only full-body base with ordinary load and repetition progression.",
    ),
}


# Source-reviewed expansion selected to broaden both training age and weekly
# frequency.  Optional cardio mentioned by a source is deliberately not a
# session: this catalog contains only the source's resistance-training work.
PROGRAMS.update({
    "planet_fitness_full_body_3": _program(
        "Planet Fitness Full Body · 3 days",
        "https://www.muscleandstrength.com/workouts/3-day-full-body-planet-fitness-workout", "new", [
            _session("Full Body A", "full body", [
                _exercise("Goblet Squat", 4, 8), _exercise("Lying Leg Curl", 3, 10),
                _exercise("Dumbbell Row", 4, 8), _exercise("Lat Pull Down", 3, 10),
                _exercise("Incline Dumbbell Bench Press", 4, 8), _exercise("Lateral Raise", 3, 10),
            ], 70),
            _session("Full Body B", "full body", [
                _exercise("Dumbbell Stiff Leg Deadlift", 4, 8), _exercise("Leg Extension", 3, 10),
                _exercise("Pull Up", 4, 8, notes="Use the assisted pull-up machine"), _exercise("Seated Cable Row", 3, 10),
                _exercise("Seated Dumbbell Press", 4, 8), _exercise("Dumbbell Bench Press", 3, 10),
            ], 70),
            _session("Full Body C", "full body", [
                _exercise("Leg Press", 4, 8), _exercise("Walking Lunge", 3, 10),
                _exercise("Smith Machine Row", 4, 8, movement="horizontal_pull"), _exercise("Cable Face Pull", 3, 10, movement="horizontal_pull"),
                _exercise("Push Up", 3, 10), _exercise("Push Ups", 3, 8, notes="Use a close grip"),
            ], 70),
        ], "Three machine-and-dumbbell full-body exposures; source lower-bound rep targets retained.",
    ),
    "long_cycle_full_body_3": _program(
        "Long Cycle Full Body · 3 days",
        "https://www.muscleandstrength.com/workouts/beginner-long-cycle-muscle-strength-building-workout", "new", [
            _session("Workout 1", "full body", [_exercise("Squat", 3, 8), _exercise("Bench Press", 3, 8), _exercise("Barbell Row", 3, 8), _exercise("Barbell Curl", 2, 10)], 60),
            _session("Workout 2", "full body", [_exercise("Deadlift", 3, 5), _exercise("Military Press", 3, 8), _exercise("Pull Up", 3, 8), _exercise("Standing Calf Raise", 3, 12)], 60),
            _session("Workout 3", "full body", [_exercise("Front Squat", 3, 8), _exercise("Incline Bench Press", 3, 8), _exercise("One Arm Dumbbell Row", 3, 10), _exercise("Skullcrusher", 2, 10)], 60),
        ], "Alternating full-body compounds provide three weekly lower, press, and pull exposures.",
    ),
    "whole_body_toning_3": _program(
        "Whole Body Toning · 3 days",
        "https://www.muscleandstrength.com/workouts/3-day-whole-body-toning-workout.html", "six_to_twenty_four_months", [
            _session("Series 1", "full body", [_exercise("Leg Press", 3, 20), _exercise("Seated Cable Row", 3, 20), _exercise("Machine Chest Press", 3, 20), _exercise("Seated Calf Raise", 2, 25)], 30),
            _session("Series 2", "full body", [_exercise("Smith Machine Front Squat", 3, 20), _exercise("Lat Pull Down", 3, 20), _exercise("Cable Fly", 3, 20, movement="horizontal_push", notes="Source movement: dumbbell fly"), _exercise("Sit Up", 2, None, notes="Use a decline bench")], 30),
            _session("Series 3", "full body", [_exercise("Dumbbell Lunge", 4, 10), _exercise("Wide Grip Pull Up", 3, None), _exercise("Barbell Bench Press", 3, 15), _exercise("Hanging Leg Raise", 2, None, notes="Source movement: horizontal leg raise")], 30),
        ], "Three short, high-repetition full-body sessions using commercial-gym equipment.",
    ),
    "planet_fitness_upper_lower_4": _program(
        "Planet Fitness Upper / Lower · 4 days",
        "https://www.muscleandstrength.com/workouts/4-day-upper-lower-planet-fitness-workout", "new", [
            _session("Upper A", "upper body", [_exercise("Dumbbell Bench Press", 4, 8), _exercise("Seated Cable Row", 4, 8), _exercise("Seated Dumbbell Press", 3, 10), _exercise("Lat Pull Down", 3, 10)], 70),
            _session("Lower A", "lower body", [_exercise("Leg Press", 4, 8), _exercise("Dumbbell Stiff Leg Deadlift", 4, 8), _exercise("Walking Lunge", 3, 10), _exercise("Lying Leg Curl", 3, 10)], 70),
            _session("Upper B", "upper body", [_exercise("Incline Dumbbell Bench Press", 4, 8), _exercise("Smith Machine Row", 4, 8, movement="horizontal_pull"), _exercise("Machine Shoulder Press", 3, 10), _exercise("Pull Up", 3, 10, notes="Use the assisted pull-up machine")], 70),
            _session("Lower B", "lower body", [_exercise("Goblet Squat", 4, 10), _exercise("Dumbbell Deadlift", 4, 8), _exercise("Leg Extension", 3, 12), _exercise("Seated Leg Curl", 3, 12)], 70),
        ], "Two machine-and-dumbbell upper and lower exposures for newer gym members.",
    ),
    "optimized_volume_4": _program(
        "Optimized Volume · 4 days", "https://www.muscleandstrength.com/workouts/ovw-workout", "new", [
            _session("Upper 1", "upper body", [_exercise("Bench Press", 4, 6), _exercise("Barbell Row", 4, 6), _exercise("Incline Dumbbell Press", 3, 8), _exercise("Lat Pull Down", 3, 8)], 45),
            _session("Lower 1", "lower body", [_exercise("Squat", 4, 6), _exercise("Lunge", 3, 8), _exercise("Leg Curl", 3, 10), _exercise("Calf Raise", 4, 10)], 45),
            _session("Upper 2", "upper body", [_exercise("Overhead Press", 4, 6), _exercise("Pullup", 4, 6), _exercise("Incline Dumbbell Bench Press", 3, 8), _exercise("Seated Cable Row", 3, 8)], 45),
            _session("Lower 2", "lower body", [_exercise("Romanian Deadlift", 4, 6), _exercise("Leg Press", 3, 8), _exercise("Leg Extension", 3, 10), _exercise("Seated Calf Raise", 4, 10)], 45),
        ], "Source upper/lower structure retains its prescribed optimal-volume compound work.",
    ),
    "phul_4": _program(
        "PHUL Power / Hypertrophy · 4 days", "https://www.muscleandstrength.com/workouts/phul-workout", "six_to_twenty_four_months", [
            _session("Upper Power", "upper body", [_exercise("Barbell Bench Press", 4, 3, rest=180), _exercise("Bent Over Row", 4, 3, rest=180), _exercise("Incline Dumbbell Bench Press", 3, 6), _exercise("Lat Pull Down", 3, 6)], 60),
            _session("Lower Power", "lower body", [_exercise("Squat", 4, 3, rest=180), _exercise("Deadlift", 4, 3, rest=180), _exercise("Leg Press", 3, 10), _exercise("Leg Curl", 3, 6)], 60),
            _session("Upper Hypertrophy", "upper body", [_exercise("Incline Barbell Bench Press", 4, 8), _exercise("Seated Cable Row", 4, 8), _exercise("One Arm Dumbbell Row", 3, 8), _exercise("Dumbbell Lateral Raise", 3, 10)], 60),
            _session("Lower Hypertrophy", "lower body", [_exercise("Front Squat", 4, 8), _exercise("Barbell Lunge", 4, 8), _exercise("Leg Extension", 3, 10), _exercise("Leg Curl", 3, 10)], 60),
        ], "Power and hypertrophy sessions each train the upper and lower body once.",
    ),
    "dumbbell_upper_lower_4": _program(
        "Dumbbell Upper / Lower · 4 days", "https://www.muscleandstrength.com/workouts/dumbbell-only-upper-lower-workout-routine", "new", [
            _session("Upper A", "upper body", [
                _exercise("Bent Over Dumbbell Row", 4, 8), _exercise("Dumbbell Bench Press", 4, 8),
                _exercise("Dumbbell Lateral Raise", 3, 8), _exercise("Dumbbell Pullover", 3, 8, movement="vertical_pull"),
                _exercise("Dumbbell Bicep Curl", 2, 8), _exercise("Dumbbell Tricep Extension", 2, 8), _exercise("Dumbbell Shrug", 2, 12),
            ], 60),
            _session("Lower A", "lower body", [
                _exercise("Goblet Squat", 4, 8), _exercise("Dumbbell Stiff Leg Deadlift", 4, 8),
                _exercise("Dumbbell Plie Squat", 3, 8, movement="knee_dominant"), _exercise("Dumbbell Hamstring Curl", 3, 8, movement="hip_hinge"),
                _exercise("Standing Dumbbell Calf Raise", 3, 8), _exercise("Plank", 3, None, duration=20),
            ], 60),
            _session("Upper B", "upper body", [
                _exercise("One Arm Dumbbell Row", 4, 8), _exercise("Dumbbell Shoulder Press", 4, 8),
                _exercise("Incline Dumbbell Bench Press", 3, 8), _exercise("Chest Supported Dumbbell Row", 3, 8, movement="horizontal_pull"),
                _exercise("Dumbbell Hammer Curl", 2, 8), _exercise("Dumbbell Floor Press", 2, 8), _exercise("Seated Dumbbell Shrug", 2, 12),
            ], 60),
            _session("Lower B", "lower body", [
                _exercise("Dumbbell Stiff Leg Deadlift", 4, 8), _exercise("Dumbbell Rear Lunge", 4, 8),
                _exercise("Dumbbell Hip Thrust", 4, 8, movement="hip_hinge"), _exercise("Dumbbell Split Squat", 3, 8),
                _exercise("Seated Dumbbell Calf Raise", 3, 8), _exercise("Plank", 3, None, duration=20),
            ], 60),
        ], "Repeatable dumbbell-only upper/lower base with two weekly exposures per major region.",
    ),
    "barbell_no_rack_4": _program(
        "Barbell Only (No Rack) · 4 days", "https://www.muscleandstrength.com/workouts/4-day-barbell-only-workout", "six_to_twenty_four_months", [
            _session("Upper A", "upper body", [
                _exercise("Overhead Press", 4, 4), _exercise("Bent Over Row", 4, 4),
                _exercise("Weighted Push Up", 4, 4, movement="horizontal_push"), _exercise("Pull Up", 4, 4),
                _exercise("Landmine Lateral Raise", 3, 6, movement="vertical_push"), _exercise("Skullcrusher", 3, 6),
            ], 90),
            _session("Lower A", "lower body", [
                _exercise("Sumo Deadlift", 4, 4), _exercise("Bulgarian Split Squat", 4, 6),
                _exercise("Barbell Glute Bridge", 5, 8, movement="hip_hinge"), _exercise("Single Leg Good Morning", 3, 8, movement="hip_hinge", notes="Use a resistance band"),
                _exercise("Side Plank", 3, None, duration=30), _exercise("Barbell Calf Raise", 4, 8),
            ], 90),
            _session("Upper B", "upper body", [
                _exercise("Band Pull Apart", 3, 20, movement="horizontal_pull"), _exercise("Floor Press", 4, 8),
                _exercise("Meadows Row", 4, 8, movement="horizontal_pull"), _exercise("Single Arm Landmine Press", 4, 8, movement="vertical_push"),
                _exercise("Inverted Row", 4, 8), _exercise("Banded Tricep Extension", 3, 20),
            ], 90),
            _session("Lower B", "lower body", [
                _exercise("Banded Hamstring Curl", 3, 15, movement="hip_hinge"), _exercise("Landmine Squat", 4, 8, movement="knee_dominant", notes="Use the source 1½-rep variation"),
                _exercise("Barbell Reverse Lunge", 4, 8), _exercise("Stiff Leg Deadlift", 4, 8),
                _exercise("Pallof Press", 2, 8), _exercise("Single Leg Calf Raise", 3, None, notes="AMRAP each side"),
            ], 90),
        ], "Repeatable limited-equipment upper/lower base with ordinary double progression.",
    ),
    "barbell_upper_lower_4": _program(
        "Home / Gym Barbell · 4 days", "https://www.muscleandstrength.com/workouts/home-gym-barbell-workout-routine", "new", [
            _session("Lower A", "lower body", [
                _exercise("Power Clean", 3, 5, movement="hip_hinge"), _exercise("Squat", 3, 6),
                _exercise("Stiff Leg Deadlift", 3, 6), _exercise("Barbell Calf Raise", 3, 12),
                _exercise("Barbell Ab Rollout", 3, 10),
            ], 45),
            _session("Upper A", "upper body", [
                _exercise("Bench Press", 3, 6), _exercise("Yates Row", 3, 6, movement="horizontal_pull"),
                _exercise("Push Press", 3, 5), _exercise("Seated French Press", 3, 8), _exercise("Barbell Curl", 3, 8),
            ], 45),
            _session("Lower B", "lower body", [
                _exercise("Deadlift", 3, 5), _exercise("Front Squat", 3, 6),
                _exercise("Good Morning", 3, 6, movement="hip_hinge", notes="Use a wide stance"), _exercise("Barbell Calf Raise", 3, 12),
                _exercise("Weighted Sit Up", 3, 10, notes="Cradle a barbell in the elbows as prescribed by the source"),
            ], 45),
            _session("Upper B", "upper body", [
                _exercise("Seated Overhead Press", 3, 6), _exercise("Barbell Row", 3, 6),
                _exercise("Incline Bench Press", 3, 6), _exercise("Close Grip Bench Press", 3, 6),
                _exercise("Reverse Grip Barbell Curl", 3, 8),
            ], 45),
        ], "Repeatable barbell upper/lower base whose source progression is to add weight when possible.",
    ),
    "maul_5": _program(
        "MAUL · 5 days", "https://www.muscleandstrength.com/workouts/maul-workout", "new", [
            _session("Upper Mechanical", "upper body", [_exercise("Bench Press", 4, 6), _exercise("Barbell Row", 4, 6), _exercise("Overhead Press", 3, 8), _exercise("Pull Up", 3, 8)], 60),
            _session("Lower Mechanical", "lower body", [_exercise("Squat", 4, 6), _exercise("Romanian Deadlift", 4, 8), _exercise("Leg Press", 3, 10), _exercise("Leg Curl", 3, 10)], 60),
            _session("Upper Full", "upper body", [_exercise("Deadlift", 3, 5), _exercise("Incline Dumbbell Bench Press", 4, 10), _exercise("Seated Cable Row", 4, 10), _exercise("Lat Pull Down", 3, 12)], 60),
            _session("Shoulders & Arms", "upper body", [_exercise("Military Press", 4, 8), _exercise("Face Pull", 4, 12, movement="horizontal_pull"), _exercise("Narrow Grip Bench Press", 3, 10), _exercise("Barbell Curl", 3, 10)], 60),
            _session("Lower Full", "lower body", [_exercise("Front Squat", 4, 10), _exercise("Barbell Hip Thrust", 4, 10), _exercise("Dumbbell Lunge", 3, 12), _exercise("Lying Leg Curl", 3, 12)], 60),
        ], "Five-day adaptation split with repeated upper and lower compound exposure.",
    ),
    "dumbbell_split_5": _program(
        "Dumbbell Split · 5 days", "https://www.muscleandstrength.com/workouts/5-day-dumbbell-only-workout-split", "six_to_twenty_four_months", [
            _session("Chest, Shoulders & Triceps", "push", [
                _exercise("Dumbbell Bench Press", 5, 8), _exercise("Incline Dumbbell Bench Press", 4, 8),
                _exercise("Dumbbell Floor Press", 3, 8), _exercise("Standing Dumbbell Press", 4, 8),
                _exercise("Dumbbell Lateral Raise", 3, 8), _exercise("Dumbbell Tricep Kickback", 3, 8),
            ], 60),
            _session("Legs & Core A", "lower body", [
                _exercise("Goblet Squat", 4, 8), _exercise("Dumbbell Stiff Leg Deadlift", 4, 8),
                _exercise("Dumbbell Rear Lunge", 4, 8), _exercise("Dumbbell Frog Squat", 3, 8, movement="knee_dominant"),
                _exercise("Dumbbell Calf Raise", 4, 20), _exercise("Weighted Crunch", 3, 20), _exercise("Side Plank", 3, None, duration=20),
            ], 60),
            _session("Back & Biceps", "pull", [
                _exercise("Bent Over Dumbbell Row", 4, 8), _exercise("Tripod Dumbbell Row", 4, 8, movement="horizontal_pull"),
                _exercise("Dumbbell Pullover", 3, 8, movement="vertical_pull"), _exercise("Reverse Grip Dumbbell Row", 4, 8, movement="horizontal_pull"),
                _exercise("Dumbbell Bicep Curl", 3, 10), _exercise("Dumbbell Hammer Curl", 3, 10),
            ], 60),
            _session("Legs & Core B", "lower body", [
                _exercise("Dumbbell Squat", 4, 8), _exercise("Dumbbell Deadlift", 4, 8),
                _exercise("Dumbbell Split Squat", 3, 8), _exercise("Dumbbell Hip Thrust", 4, 10, movement="hip_hinge"),
                _exercise("Dumbbell Calf Raise", 4, 20), _exercise("Dumbbell Side Bend", 3, 15), _exercise("Plank", 3, None, duration=20),
            ], 60),
            _session("Complete Upper Body", "upper body", [
                _exercise("One Arm Dumbbell Row", 4, 8), _exercise("Arnold Press", 4, 8),
                _exercise("Incline Dumbbell Bench Press", 4, 8), _exercise("Chest Supported Dumbbell Row", 3, 8, movement="horizontal_pull"),
                _exercise("Pinwheel Curl", 2, 8), _exercise("Overhead Dumbbell Tricep Extension", 3, 8), _exercise("Dumbbell Shrug", 3, 12),
            ], 60),
        ], "Repeatable dumbbell split with two lower and two complete upper-region exposures.",
    ),
    "powerbuilding_ppl_6": _program(
        "Powerbuilding PPL · 6 days", "https://www.muscleandstrength.com/workouts/6-day-powerbuilding-split-meal-plan", "six_to_twenty_four_months", [
            _session("Push A", "push", [_exercise("Barbell Bench Press", 5, 3, rest=120), _exercise("Seated Overhead Press", 3, 8), _exercise("Weighted Dips", 3, 10, movement="vertical_push")], 60),
            _session("Pull A", "pull", [_exercise("Deadlift", 5, 3, rest=120), _exercise("Chin Up", 3, 8, notes="Add weight when appropriate"), _exercise("Chest Supported Machine Row", 3, 10)], 60),
            _session("Legs A", "lower body", [_exercise("Barbell Back Squat", 5, 3, movement="knee_dominant", rest=120), _exercise("Good Morning", 3, 8, movement="hip_hinge"), _exercise("Leg Press", 3, 10)], 60),
            _session("Push B", "push", [_exercise("Standing Overhead Barbell Press", 5, 3, rest=120), _exercise("Incline Bench Press", 3, 8), _exercise("Narrow Grip Bench Press", 3, 10)], 60),
            _session("Pull B", "pull", [_exercise("Deadlift", 5, 3, rest=120, notes="Use the source snatch-grip variation"), _exercise("Barbell Row", 3, 8), _exercise("Pull Up", 3, 10, notes="Add weight when appropriate")], 60),
            _session("Legs B", "lower body", [_exercise("Front Squat", 5, 3, rest=120), _exercise("Romanian Deadlift", 3, 8), _exercise("Barbell Hip Thrust", 3, 10)], 60),
        ], "Heavy, medium, and lighter rep-goal PPL sessions are represented as editable starting targets.",
    ),
    "low_volume_high_intensity_6": _program(
        "Low-Volume High-Intensity · 6 days", "https://www.muscleandstrength.com/workouts/6-day-low-volume-high-intensity-workout-split", "six_to_twenty_four_months", [
            _session("Chest & Triceps", "push", [_exercise("Incline Smith Machine Bench Press", 2, 6), _exercise("Flat Machine Chest Press", 2, 6), _exercise("Pec Deck", 2, 8), _exercise("Machine Tricep Dip", 2, 8, movement="vertical_push")], 75),
            _session("Back Thickness", "pull", [_exercise("Machine Row", 2, 6, notes="Use the Hammer Strength low row"), _exercise("Chest Supported Machine Row", 2, 6), _exercise("Rack Deadlifts", 2, 6)], 75),
            _session("Quads", "lower body", [_exercise("Front Squat", 2, 6), _exercise("Hack Squat", 2, 6), _exercise("Leg Press", 2, 8, notes="Perform one leg at a time"), _exercise("Walking Lunge", 2, 8)], 75),
            _session("Shoulders & Biceps", "push", [_exercise("Machine Shoulder Press", 2, 6), _exercise("Standing Military Press", 2, 6), _exercise("Lateral Raise", 2, 8, notes="Use a cable"), _exercise("Barbell Curl", 2, 8)], 75),
            _session("Back Width", "pull", [_exercise("Cable Row", 2, 6, notes="Perform one arm at a time"), _exercise("Dumbbell Pullover", 2, 8, movement="vertical_pull"), _exercise("Narrow Grip Pull Down", 2, 8, notes="Use an underhand grip")], 75),
            _session("Hamstrings", "lower body", [_exercise("Lying Leg Curl", 2, 6), _exercise("Stiff Leg Deadlifts", 2, 6), _exercise("Seated Hamstring Curl", 2, 8), _exercise("Barbell Hip Thrust", 2, 8, notes="Source movement: barbell glute bridge")], 75),
        ], "Six focused low-volume sessions; each exercise uses the source's heavy starting rep target.",
    ),
    "built_different_ppl_6": _program(
        "Built Different PPL · 6 days", "https://www.muscleandstrength.com/workouts/built-different-ppl-workout", "two_plus_years", [
            _session("Push 1", "push", [_exercise("Push Up", 5, 10), _exercise("Dumbbell Shoulder Press", 4, 6), _exercise("Dip", 3, 12, movement="vertical_push"), _exercise("Seated Lateral Raise", 3, 12)], 60),
            _session("Pull 1", "pull", [_exercise("Straight Arm Lat Pull Down", 3, 15), _exercise("Dumbbell Row", 4, 6), _exercise("Pull Up", 3, 5), _exercise("Cable Row", 3, 12)], 60),
            _session("Legs 1", "lower body", [_exercise("Trap Bar Deadlift", 4, 6), _exercise("Leg Press", 3, 15), _exercise("Walking Lunge", 3, 15, notes="Use bodyweight"), _exercise("Hamstring Curl", 3, 12)], 60),
            _session("Push 2", "push", [_exercise("Incline Dumbbell Bench Press", 4, 8), _exercise("Machine Pec Deck", 3, 12), _exercise("Machine Shoulder Press", 4, 8), _exercise("Lateral Raise", 4, 10)], 60),
            _session("Pull 2", "pull", [_exercise("Lat Pull Down", 4, 6), _exercise("T-Bar Row", 4, 6), _exercise("Close Grip Pull Down", 3, 12), _exercise("Inverted Row", 3, 10)], 60),
            _session("Legs 2", "lower body", [_exercise("Squat", 4, 8), _exercise("Dumbbell Stiff Leg Deadlift", 3, 10), _exercise("Dumbbell Rear Lunge", 3, 8, notes="Perform laterally"), _exercise("Hyperextension", 3, 15)], 60),
        ], "Advanced six-day PPL with two distinct exposures for every major region.",
    ),
    "muscle_mania_6": _program(
        "Muscle Mania Upper / Lower · 6 days", "https://www.muscleandstrength.com/workouts/muscle-mania-10-week-muscle-growth-workout", "two_plus_years", [
            _session("Upper 1", "upper body", [_exercise("Bench Press", 4, 8), _exercise("Barbell Row", 4, 8), _exercise("Overhead Press", 3, 10), _exercise("Pull Up", 3, 10)], 75),
            _session("Lower 1", "lower body", [_exercise("Squat", 4, 8), _exercise("Romanian Deadlift", 4, 10), _exercise("Leg Press", 3, 12), _exercise("Leg Curl", 3, 12)], 75),
            _session("Upper 2", "upper body", [_exercise("Incline Barbell Bench Press", 4, 10), _exercise("Seated Cable Row", 4, 10), _exercise("Dumbbell Shoulder Press", 3, 12), _exercise("Lat Pull Down", 3, 12)], 75),
            _session("Lower 2", "lower body", [_exercise("Deadlift", 4, 6), _exercise("Front Squat", 4, 10), _exercise("Dumbbell Lunge", 3, 12), _exercise("Lying Leg Curl", 3, 12)], 75),
            _session("Upper 3", "upper body", [_exercise("Dumbbell Bench Press", 4, 12), _exercise("One Arm Dumbbell Row", 4, 12), _exercise("Seated Arnold Press", 3, 12), _exercise("Chin Up", 3, 10)], 75),
            _session("Lower 3", "lower body", [_exercise("Hack Squat", 4, 12), _exercise("Stiff Leg Deadlifts", 4, 12), _exercise("Dumbbell Reverse Lunge", 3, 12, notes="Perform as walking lunges"), _exercise("Seated Leg Curl", 3, 15)], 75),
        ], "Advanced upper/lower rotation trains every major muscle group three times weekly.",
    ),
})

# Policy and source-template drift is a correctness failure, not a runtime
# coaching choice. Validate it when the catalog is loaded.
from coach.program_policy import validate_program_policies
validate_program_policies(PROGRAMS)


PLAN_CHOICES = tuple(
    {
        "key": key,
        "title": program["name"],
        "days": len(program["sessions"]),
        "experience": program["experience"],
        "experience_label": EXPERIENCE_BADGES[program["experience"]]["label"],
        "experience_slug": EXPERIENCE_BADGES[program["experience"]]["slug"],
        "duration_min": max(session["duration_min"] for session in program["sessions"]),
        "source_details": (
            ("Main Goal", "Build Muscle"),
            ("Workout Type", "Full Body" if all(session["focus_tags"][0] == "full body" for session in program["sessions"]) else "Split"),
            ("Training Level", EXPERIENCE_BADGES[program["experience"]]["label"]),
            ("Days Per Week", f"{len(program['sessions'])} days"),
            ("Time Per Workout", f"{max(session['duration_min'] for session in program['sessions'])} min"),
        ),
        "description": program["volume_review"],
        "source_url": program["source_url"],
    }
    for key, program in PROGRAMS.items()
)
PLAN_KEYS = {choice["key"] for choice in PLAN_CHOICES}


def recommend_program(
    *,
    plan_key: str,
    limitations: str,
    session_duration_min: int,
    history_summary: str,
) -> dict[str, Any]:
    if plan_key not in PLAN_KEYS:
        raise ValueError("Choose one of the available gym plans.")
    template = PROGRAMS[plan_key]
    sessions = deepcopy(template["sessions"])
    for session in sessions:
        session["session_role"] = "coach_strength"
        session["target_frequency"] = 1

    reasons = [
        f"You selected the {template['name']} routine.",
        history_summary,
        template["volume_review"],
        f"It contains {len(sessions)} undated gym sessions from the cited source.",
        "Approving it does not assign dates or upload workouts.",
    ]
    if limitations.strip():
        reasons.append("Your constraints are saved and must be checked before scheduling or exercise changes.")
    if session_duration_min < max(s["duration_min"] for s in sessions):
        reasons.append("Your stated time limit may be shorter than this source routine; the proposal is not silently trimmed.")
    reasons.append("Unknown weights stay open for first-session calibration.")

    return {
        "key": plan_key,
        "name": template["name"],
        "sessions": sessions,
        "strength_session_count": len(sessions),
        "days_per_week": len(sessions),
        "attribution": ACSM_ATTRIBUTION,
        "source_url": template["source_url"],
        "rationale": " ".join(reasons),
        "catalog_version": template["catalog_version"],
    }
