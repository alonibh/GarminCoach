"""Curated, source-audited gym routines used by onboarding."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from coach.exercises import CATALOG_VERSION, exercise_key, exercise_metadata


ACSM_SOURCE_URL = "https://acsm.org/resistance-training-guidelines-update-2026/"
ACSM_ATTRIBUTION = "Muscle & Strength routine reviewed against ACSM 2026 resistance-training guidance."

# These labels describe the routine's demands—not an absolute judgment of the
# athlete. They are assigned during catalog review from training age, weekly
# frequency, exercise complexity, and total volume.
EXPERIENCE_BADGES = {
    "new": {"label": "Beginner", "slug": "beginner"},
    "six_to_twenty_four_months": {"label": "Intermediate", "slug": "intermediate"},
    "two_plus_years": {"label": "Expert", "slug": "expert"},
    "returning": {"label": "Beginner", "slug": "beginner"},
}


# Only these compound categories qualify for the 20-minute return rule.
_WARMUP_COMPOUND_CATEGORIES = {
    "SQUAT", "LUNGE", "DEADLIFT", "HIP_RAISE", "BENCH_PRESS",
    "SHOULDER_PRESS", "PULL_UP", "ROW",
}
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
    if reps is None or duration_seconds is not None:
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


def _is_direct_shoulder_compound(meta: dict[str, Any] | None) -> bool:
    """A press directly loads the shoulders, unlike their stabilizing role in a bench press."""
    return (meta or {}).get("category") == "SHOULDER_PRESS"


def _apply_session_warmups(exercises: list[dict[str, Any]]) -> None:
    """Apply the daily-anchor, cold-joint, flow, and 20-minute return rules."""
    touched_regions: set[str] = set()
    joint_touched_at: dict[str, int] = {}
    elapsed_seconds = 0
    previous_joint_systems: set[str] = set()
    direct_shoulder_press_seen = False
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
        is_first_direct_shoulder_press = _is_direct_shoulder_compound(meta) and not direct_shoulder_press_seen
        last_touched = joint_touched_at.get(primary_joint_system) if primary_joint_system else None
        is_long_break_return = bool(
            _is_heavy_compound(exercise)
            and last_touched is not None
            and elapsed_seconds - last_touched >= 20 * 60
        )
        if can_warm_up and not is_back_to_back_flow and (
            is_anchor or (is_new_major_region and has_cold_joint) or is_first_direct_shoulder_press or is_long_break_return
        ):
            exercise.update(defaults)
        exercise_end = elapsed_seconds + _estimated_exercise_seconds(exercise)
        touched_regions.update({region} if region else set())
        for system in joint_systems:
            joint_touched_at[system] = exercise_end
        direct_shoulder_press_seen = direct_shoulder_press_seen or _is_direct_shoulder_compound(meta)
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
        focus = session["focus_tags"][0]
        focus_regions = {
            "full body": {"lower", "push", "pull"}, "upper body": {"push", "pull"},
            "lower body": {"lower"}, "push": {"push"}, "pull": {"pull"},
        }
        if region in focus_regions.get(focus, set()):
            return True
        patterns = {exercise["movement_pattern"] for exercise in session["exercises"]}
        relevant = {"lower": {"knee_dominant", "hip_hinge"}, "push": {"horizontal_push", "vertical_push", "elbow_extension"}, "pull": {"horizontal_pull", "vertical_pull", "elbow_flexion"}}
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
        "catalog_version": CATALOG_VERSION,
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
            _session("Workout A", "full body", [
                _exercise("Trap Bar Deadlift", 5, 6, rest=120), _exercise("Military Press", 5, 6, rest=120),
                _exercise("Lat Pull Down", 4, 12, rest=120), _exercise("T Bar Row", 4, 12, rest=120),
                _exercise("Push Up", 3, None, rest=120, notes="AMRAP; leave one rep in reserve"),
            ], 90),
            _session("Workout B", "full body", [
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
            _session("Full Body A", "full body", [
                _exercise("Trap Bar Deadlift", 3, 5, rest=180), _exercise("Bent Over Row", 3, 8, rest=90),
                _exercise("Dumbbell Overhead Press", 3, 10, rest=90), _exercise("Lat Pull Down", 3, 10, rest=90),
                _exercise("Dumbbell Bicep Curl", 2, 12, rest=45), _exercise("Rope Pressdown", 2, 12, rest=45),
                _exercise("Plank", 2, None, rest=45, notes="Hold to technical failure"),
            ], 75),
            _session("Full Body B", "full body", [
                _exercise("Front Squat", 3, 5, rest=180), _exercise("Romanian Deadlift", 3, 10, rest=90),
                _exercise("Farmer's Carry", 3, None, movement="core", rest=90, duration=45),
                _exercise("Dumbbell Row", 3, 15, rest=90), _exercise("Incline Dumbbell Bench Press", 3, 12, rest=90),
                _exercise("Calf Raise", 3, 20, rest=45), _exercise("Dead Bugs", 2, 12, rest=45, notes="Each side"),
            ], 75),
            _session("Full Body C", "full body", [
                _exercise("Bench Press", 3, 5, rest=180), _exercise("Leg Press", 3, 10, rest=90),
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
            _session("Workout A", "full body", [
                _exercise("Squat", 3, 5, rest=120), _exercise("Bench Press", 3, 5, rest=120),
                _exercise("Barbell Row", 3, 5, rest=120), _exercise("Upright Row", 3, 10, rest=90),
                _exercise("Skullcrushers", 3, 10, rest=90), _exercise("Dumbbell Curls", 3, 10, rest=90),
                _exercise("Leg Curls", 3, 15, rest=90), _exercise("Ab Wheel Roll Out", 3, 15, rest=90),
            ], 90),
            _session("Workout B", "full body", [
                _exercise("Deadlift", 1, 5, rest=120), _exercise("Romanian Deadlift", 2, 12, rest=120),
                _exercise("Seated Overhead Press", 3, 10, rest=120), _exercise("Pull Ups", 3, 15, rest=90),
                _exercise("Dips", 3, 20, movement="vertical_push", rest=90), _exercise("Barbell Shrugs", 3, 10, rest=90),
                _exercise("Seated Calf Raise", 3, 15, rest=90), _exercise("Plank", 3, None, rest=90, duration=60),
            ], 90),
            _session("Workout C", "full body", [
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
            _session("Day 1", "full body", [
                _exercise("Squat", 5, 5), _exercise("Dumbbell Bench Press", 4, 10),
                _exercise("Dumbbell Row", 4, 10), _exercise("Seated Dumbbell Press", 4, 10),
                _exercise("Lunge", 4, 10), _exercise("Dumbbell Curl", 3, 10),
                _exercise("Standing Barbell Tricep Extension", 3, 10), _exercise("Calf Raise", 3, 12),
                _exercise("Plank", 5, None, duration=20),
            ], 90),
            _session("Day 2", "full body", [
                _exercise("Bench Press", 5, 5), _exercise("Machine Pec Deck", 3, 12),
                _exercise("Leg Extension", 4, 10), _exercise("Leg Curl", 4, 10),
                _exercise("Pullup", 4, 10), _exercise("Seated Lateral Raise", 4, 10),
                _exercise("Dumbbell Hammer Curls", 3, 10), _exercise("Rope Extension", 3, 10),
                _exercise("Plank", 5, None, duration=20),
            ], 90),
            _session("Day 3", "full body", [
                _exercise("Deadlift", 5, 5), _exercise("Incline Dumbbell Press", 4, 10),
                _exercise("Lateral Raise", 4, 10), _exercise("Pulldown", 4, 10),
                _exercise("Leg Press", 4, 10), _exercise("EZ Bar Curl", 3, 10),
                _exercise("Skullcrushers", 3, 10), _exercise("Dumbbell Shrugs", 3, 12),
                _exercise("Plank", 5, None, duration=20),
            ], 90),
        ],
        "High-volume full-body routine with three weekly exposures; recommend only to established trainees.",
    ),
    "upper_lower_full_3": _program(
        "Upper / Lower / Full Body · 3 days",
        "https://www.muscleandstrength.com/workouts/get-ripped-3-day-split",
        "six_to_twenty_four_months",
        [
            _session("Upper", "upper body", [
                _exercise("Push Up", 3, 10, rest=90), _exercise("Pull Up", 3, 10, rest=90),
                _exercise("Band Pull Apart", 3, 10, rest=90), _exercise("Incline Bench Press", 3, 12, rest=90),
                _exercise("Tricep Skull Crusher", 3, 12, rest=90), _exercise("Dumbbell Row", 3, 12, rest=90),
                _exercise("Dumbbell Alternating Curl", 3, 12, rest=90), _exercise("Military Press", 3, 12, rest=90),
            ], 45),
            _session("Lower", "lower body", [
                _exercise("Bodyweight Squats", 3, 10, rest=90), _exercise("Bodyweight Lunges", 3, 10, rest=90, notes="Each side"),
                _exercise("Jump Tucks", 3, 10, movement="knee_dominant", rest=90), _exercise("Squat", 3, 12, rest=90),
                _exercise("Deadlift", 3, 12, rest=90), _exercise("Leg Press", 3, 15, rest=90),
            ], 45),
            _session("Full Body", "full body", [
                _exercise("Squat", 3, 12, rest=90), _exercise("Incline Dumbbell Press", 3, 12, rest=90),
                _exercise("Barbell Row", 3, 12, rest=90), _exercise("Dumbbell Lateral Raise", 3, 12, movement="vertical_push", rest=90),
                _exercise("Barbell Curl", 3, 12, rest=90), _exercise("Barbell Overhead Extension", 3, 12, rest=90),
            ], 45),
        ],
        "Upper, lower, and full-body sessions provide two weekly exposures; source cardio is excluded.",
    ),
    "upper_lower_4": _program(
        "Upper / Lower Bodybuilding · 4 days",
        "https://www.muscleandstrength.com/workouts/upper-lower-4-day-gym-bodybuilding-workout",
        "new",
        [
            _session("Upper A", "upper body", [
                _exercise("Bench Press", 3, 12), _exercise("Barbell Row", 3, 12), _exercise("Seated Overhead Dumbbell Press", 3, 12),
                _exercise("Pec Deck", 2, 12), _exercise("V-Bar Lat Pull Down", 2, 12), _exercise("Side Lateral Raise", 2, 15),
                _exercise("Cable Tricep Extensions", 3, 12), _exercise("Cable Curls", 3, 12),
            ]),
            _session("Lower A", "lower body", [
                _exercise("Squat", 3, 12), _exercise("Stiff Leg Deadlifts", 3, 12), _exercise("Standing Calf Raise", 3, 15),
                _exercise("Leg Extension", 2, 12), _exercise("Leg Curl", 2, 12), _exercise("Seated Calf Raise", 2, 12),
                _exercise("Cable Crunch", 3, 12), _exercise("Cable Pull Through", 3, 12),
            ]),
            _session("Upper B", "upper body", [
                _exercise("Incline Dumbbell Bench Press", 3, 12), _exercise("Rack Deadlifts", 3, 8), _exercise("Military Press", 3, 12),
                _exercise("Machine Chest Press", 2, 12), _exercise("Machine Row", 2, 12), _exercise("Machine Shoulder Press", 2, 12),
                _exercise("Dumbbell Curls", 3, 12), _exercise("Machine Tricep Dip", 3, 12),
            ]),
            _session("Lower B", "lower body", [
                _exercise("Leg Press", 3, 20), _exercise("Dumbbell Stiff Leg Deadlift", 3, 12), _exercise("Leg Press Calf Raise", 3, 15),
                _exercise("Hack Squat", 2, 12), _exercise("Seated Leg Curl", 2, 12), _exercise("Seated Calf Raise", 2, 12),
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
                _exercise("Front Squat", 3, 5, rest=180), _exercise("Trap Bar Deadlift", 3, 5, rest=180),
                _exercise("Hack Squat", 3, 15, rest=90), _exercise("Glute Ham Raise", 3, 10, movement="hip_hinge", rest=90),
                _exercise("Seated Calf Raise", 4, 10, rest=90),
            ]),
            _session("Upper Strength", "upper body", [
                _exercise("Dumbbell Bench Press", 3, 5, rest=180), _exercise("One Arm Dumbbell Row", 3, 5, rest=180),
                _exercise("Overhead Press", 3, 5, rest=180), _exercise("Pull Up", 3, 10, rest=90),
                _exercise("Incline Bench Press", 3, 10, rest=90), _exercise("Tricep Dip", 2, 10, movement="vertical_push", rest=90),
                _exercise("Farmer's Carry", 2, None, movement="core", rest=90, duration=40),
            ]),
            _session("Lower Hypertrophy", "lower body", [
                _exercise("Front Squat", 3, 12), _exercise("Dumbbell Reverse Lunge", 3, 12), _exercise("Barbell Hip Thrust", 3, 12),
                _exercise("Leg Extension", 3, 15), _exercise("Romanian Deadlift", 3, 15), _exercise("Standing Machine Calf Raise", 3, 12),
            ]),
            _session("Upper Hypertrophy", "upper body", [
                _exercise("Incline Dumbbell Bench Press", 3, 12), _exercise("Decline Bench Press", 3, 12),
                _exercise("Lat Pull Down", 3, 12), _exercise("Inverted Row", 3, 12), _exercise("Face Pull", 3, 12),
                _exercise("Lateral Raise", 3, 12), _exercise("Barbell Curl", 3, 12), _exercise("Incline Skullcrusher", 3, 12),
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
                _exercise("Weighted Wide Grip Pull Ups", 2, 6, rest=120), _exercise("Bent Over Barbell Row", 4, 6, rest=120),
                _exercise("Narrow Grip T-Bar Row", 2, 6, rest=120), _exercise("Standing Overhead Barbell Press", 4, 6, rest=120),
                _exercise("Incline Dumbbell Bench Press", 4, 6, rest=120), _exercise("Weighted Dips", 2, 6, movement="vertical_push", rest=120),
                _exercise("EZ Bar Skullcrusher", 2, 6, rest=120), _exercise("EZ Bar Bicep Curls", 2, 6, rest=120),
            ], 90),
            _session("Lower Strength", "lower body", [
                _exercise("Squats", 4, 6, rest=120), _exercise("Hack Squats", 2, 6, rest=120),
                _exercise("Deadlifts", 4, 6, rest=120), _exercise("Lying Leg Curls", 2, 6, rest=120),
                _exercise("Standing Calf Raise", 4, 6, rest=120), _exercise("Seated Calf Raise", 2, 6, rest=120),
            ], 90),
            _session("Back & Shoulders Size", "pull", [
                _exercise("Wide Grip Pull Down", 4, 12), _exercise("Narrow Grip Pull Down", 4, 12),
                _exercise("Chest Supported Machine Row", 4, 12), _exercise("Narrow Grip Low Pulley Cable Row", 2, 12),
                _exercise("Straight Arm Rope Pull Down", 2, 12), _exercise("Lower Back Hyperextensions", 2, 12),
                _exercise("Dumbbell Shoulder Press", 4, 12), _exercise("Standing Dumbbell Side Lateral Raise", 2, 12),
                _exercise("Standing EZ Bar Front Raise", 2, 12), _exercise("Dumbbell Rear Delt Lateral Raise", 2, 12),
                _exercise("Cable EZ Bar Upright Row", 2, 12), _exercise("Rope Face Pull", 2, 12),
            ], 90),
            _session("Chest & Arms Size", "push", [
                _exercise("Incline Barbell Bench Press", 4, 12), _exercise("Flat Machine Chest Press", 2, 12),
                _exercise("Incline Dumbbell Fly", 2, 12), _exercise("Cable Crossover", 2, 12),
                _exercise("Narrow Grip Bench Press", 2, 12), _exercise("Seated Overhead EZ Bar Tricep Extension", 2, 12),
                _exercise("Single Arm Cable Press Down", 2, 12), _exercise("EZ Bar Preacher Curl", 2, 12),
                _exercise("Standing Alternating Dumbbell Hammer Curl", 2, 12), _exercise("High Pulley Single Arm Bicep Curl", 2, 12),
            ], 90),
            _session("Legs Size", "lower body", [
                _exercise("Seated Hamstring Curl", 4, 12), _exercise("Leg Extension", 4, 12), _exercise("Front Squat", 4, 12),
                _exercise("Leg Press", 4, 12), _exercise("Barbell Walking Lunge", 4, 12, notes="Each side"),
                _exercise("Abductor Machine", 2, 12, movement="hip_hinge"), _exercise("Adductor Machine", 2, 12, movement="hip_hinge"),
                _exercise("Glute Kick Backs", 2, 12, notes="Each side"), _exercise("Donkey Calf Raise", 4, 12),
                _exercise("Seated Calf Raise", 4, 12), _exercise("Single Leg Calf Press", 4, 12, notes="Each side"),
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
}


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
