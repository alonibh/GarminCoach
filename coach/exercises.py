"""Garmin strength exercise catalog generated from the official FIT Profile."""
from __future__ import annotations

import json
import re
from pathlib import Path


CATALOG_VERSION = "fit-profile-2026-07-14"

_MOVEMENT_BY_CATEGORY = {
    "BENCH_PRESS": "horizontal_push", "FLYE": "horizontal_push", "PUSH_UP": "horizontal_push",
    "SHOULDER_PRESS": "vertical_push", "LATERAL_RAISE": "vertical_push", "SHOULDER_STABILITY": "vertical_push",
    "PULL_UP": "vertical_pull", "ROW": "horizontal_pull", "SHRUG": "horizontal_pull",
    "SQUAT": "knee_dominant", "LUNGE": "knee_dominant", "PLYO": "knee_dominant",
    "DEADLIFT": "hip_hinge", "LEG_CURL": "hip_hinge", "HIP_RAISE": "hip_hinge",
    "HIP_STABILITY": "hip_hinge", "HIP_SWING": "hip_hinge", "HYPEREXTENSION": "hip_hinge",
    "CALF_RAISE": "calves", "CURL": "elbow_flexion", "TRICEPS_EXTENSION": "elbow_extension",
    "CHOP": "core", "CORE": "core", "CRUNCH": "core", "LEG_RAISE": "core",
    "PLANK": "core", "SIT_UP": "core",
}

_MUSCLE_BY_CATEGORY = {
    "BENCH_PRESS": "chest", "FLYE": "chest", "PUSH_UP": "chest",
    "SHOULDER_PRESS": "shoulders", "LATERAL_RAISE": "shoulders", "SHOULDER_STABILITY": "shoulders",
    "PULL_UP": "back", "ROW": "back", "SHRUG": "back",
    "SQUAT": "quads", "LUNGE": "quads", "PLYO": "quads",
    "DEADLIFT": "hamstrings_glutes", "LEG_CURL": "hamstrings_glutes",
    "HIP_RAISE": "hamstrings_glutes", "HIP_STABILITY": "hamstrings_glutes",
    "HIP_SWING": "hamstrings_glutes", "HYPEREXTENSION": "hamstrings_glutes",
    "CALF_RAISE": "calves", "CURL": "biceps", "TRICEPS_EXTENSION": "triceps",
    "CHOP": "core", "CORE": "core", "CRUNCH": "core", "LEG_RAISE": "core",
    "PLANK": "core", "SIT_UP": "core", "CARRY": "core",
    "BANDED_EXERCISES": "shoulders",
    "OLYMPIC_LIFT": "full_body", "TOTAL_BODY": "full_body",
    "BATTLE_ROPE": "full_body", "SLED": "full_body", "SLEDGE_HAMMER": "full_body",
}

_MUSCLE_BY_MOVEMENT = {
    "horizontal_push": "chest", "vertical_push": "shoulders",
    "horizontal_pull": "back", "vertical_pull": "back",
    "knee_dominant": "quads", "hip_hinge": "hamstrings_glutes",
    "calves": "calves", "elbow_flexion": "biceps",
    "elbow_extension": "triceps", "core": "core",
}

# Friendly source names that are broader than Garmin's exact enum label.
_ALIASES = {
    "BENCH_PRESS": "BENCH_PRESS:BARBELL_BENCH_PRESS",
    "DEADLIFT": "DEADLIFT:BARBELL_DEADLIFT",
    "FRONT_SQUAT": "SQUAT:BARBELL_FRONT_SQUAT",
    "INCLINE_BENCH_PRESS": "BENCH_PRESS:INCLINE_BARBELL_BENCH_PRESS",
    "DECLINE_BENCH_PRESS": "BENCH_PRESS:DECLINE_DUMBBELL_BENCH_PRESS",
    "LAT_PULL_DOWN": "PULL_UP:LAT_PULLDOWN",
    "PULLDOWN": "PULL_UP:LAT_PULLDOWN",
    "CABLE_ROW": "ROW:SEATED_CABLE_ROW",
    "BODYWEIGHT_HIP_THRUST": "HIP_RAISE:HIP_RAISE",
    "ONE_ARM_DUMBBELL_ROW": "ROW:ONE_ARM_BENT_OVER_ROW",
    "BENT_OVER_BARBELL_ROW": "ROW:BENT_OVER_ROW_WITH_BARBELL",
    "BENT_OVER_ROW": "ROW:BENT_OVER_ROW_WITH_BARBELL",
    "MILITARY_PRESS": "SHOULDER_PRESS:MILITARY_PRESS",
    "OVERHEAD_PRESS": "SHOULDER_PRESS:OVERHEAD_BARBELL_PRESS",
    "SEATED_OVERHEAD_PRESS": "SHOULDER_PRESS:SEATED_BARBELL_SHOULDER_PRESS",
    "DIPS": "TRICEPS_EXTENSION:BODY_WEIGHT_DIP",
    "TRICEP_DIP": "TRICEPS_EXTENSION:BODY_WEIGHT_DIP",
    "WEIGHTED_DIPS": "TRICEPS_EXTENSION:WEIGHTED_DIP",
    "ROPE_EXTENSION": "TRICEPS_EXTENSION:ROPE_PRESSDOWN",
    "CABLE_TRICEP_EXTENSIONS": "TRICEPS_EXTENSION:TRICEPS_PRESSDOWN",
    "TRICEP_PUSHDOWN": "TRICEPS_EXTENSION:TRICEPS_PRESSDOWN",
    "SKULLCRUSHERS": "TRICEPS_EXTENSION:LYING_EZ_BAR_TRICEPS_EXTENSION",
    "LEG_PRESS_CALF_RAISE": "CALF_RAISE:SEATED_CALF_RAISE",
    "AB_WHEEL_ROLL_OUT": "CORE:KNEELING_AB_WHEEL",
    # Curated source-routine names reviewed against Garmin's canonical labels.
    "ABDUCTOR_MACHINE": "HIP_STABILITY:WEIGHTED_STANDING_HIP_ABDUCTION",
    "ADDUCTOR_MACHINE": "HIP_STABILITY:WEIGHTED_SLIDING_HIP_ADDUCTION",
    "BAND_PULL_APART": "BANDED_EXERCISES:PULL_APART",
    "BARBELL_CURL": "CURL:BARBELL_BICEPS_CURL",
    "BARBELL_CURLS": "CURL:BARBELL_BICEPS_CURL",
    "BARBELL_HIP_THRUST": "HIP_RAISE:BARBELL_HIP_THRUST_WITH_BENCH",
    "BARBELL_OVERHEAD_EXTENSION": "TRICEPS_EXTENSION:SEATED_BARBELL_OVERHEAD_TRICEPS_EXTENSION",
    "BARBELL_SHRUGS": "SHRUG:BARBELL_SHRUG",
    "BARBELL_SQUAT": "SQUAT:BARBELL_BACK_SQUAT",
    "BARBELL_WALKING_LUNGE": "LUNGE:BARBELL_LUNGE",
    "BODYWEIGHT_LUNGES": "LUNGE:WALKING_LUNGE",
    "BODYWEIGHT_SQUATS": "SQUAT:BACK_SQUATS",
    "CABLE_CURL": "CURL:CABLE_BICEPS_CURL",
    "CABLE_CURLS": "CURL:CABLE_BICEPS_CURL",
    "CABLE_EZ_BAR_UPRIGHT_ROW": "SHRUG:BARBELL_UPRIGHT_ROW",
    "CABLE_FACE_PULL": "ROW:BANDED_FACE_PULLS",
    "CABLE_OVERHEAD_TRICEP_EXTENSION": "TRICEPS_EXTENSION:CABLE_OVERHEAD_TRICEPS_EXTENSION",
    "CALF_RAISE": "CALF_RAISE:STANDING_CALF_RAISE",
    "CHEST_SUPPORTED_MACHINE_ROW": "ROW:CHEST_SUPPORTED_DUMBBELL_ROW",
    "CLOSE_GRIP_PULL_DOWN": "PULL_UP:CLOSE_GRIP_LAT_PULLDOWN",
    "DEAD_BUGS": "HIP_STABILITY:DEAD_BUG",
    "DEADLIFTS": "DEADLIFT:BARBELL_DEADLIFT",
    "DECLINE_BENCH_PRESS": "BENCH_PRESS:DECLINE_DUMBBELL_BENCH_PRESS",
    "DUMBBELL_ALTERNATING_CURL": "CURL:ALTERNATING_DUMBBELL_BICEPS_CURL",
    "DUMBBELL_BICEP_CURL": "CURL:DUMBBELL_BICEPS_CURL",
    "DUMBBELL_CURL": "CURL:DUMBBELL_BICEPS_CURL",
    "DUMBBELL_CURLS": "CURL:DUMBBELL_BICEPS_CURL",
    "DUMBBELL_HAMMER_CURLS": "CURL:DUMBBELL_HAMMER_CURL",
    "DUMBBELL_OVERHEAD_PRESS": "SHOULDER_PRESS:OVERHEAD_DUMBBELL_PRESS",
    "DUMBBELL_REAR_DELT_LATERAL_RAISE": "LATERAL_RAISE:SEATED_REAR_LATERAL_RAISE",
    "DUMBBELL_REAR_LUNGE": "LUNGE:DUMBBELL_REVERSE_LUNGE",
    "DUMBBELL_SHRUGS": "SHRUG:DUMBBELL_SHRUG",
    "DUMBBELL_STIFF_LEG_DEADLIFT": "DEADLIFT:DUMBBELL_STRAIGHT_LEG_DEADLIFT",
    "DUMBBELL_STIFF_LEGGED_DEADLIFT": "DEADLIFT:DUMBBELL_STRAIGHT_LEG_DEADLIFT",
    "EZ_BAR_BICEP_CURLS": "CURL:STANDING_EZ_BAR_BICEPS_CURL",
    "EZ_BAR_CURL": "CURL:STANDING_EZ_BAR_BICEPS_CURL",
    "EZ_BAR_SKULLCRUSHER": "TRICEPS_EXTENSION:LYING_EZ_BAR_TRICEPS_EXTENSION",
    "FARMERS_CARRY": "CARRY:FARMERS_CARRY",
    "FARMER_S_CARRY": "CARRY:FARMERS_CARRY",
    "FLAT_MACHINE_CHEST_PRESS": "BENCH_PRESS:SMITH_MACHINE_BENCH_PRESS",
    "GLUTE_HAM_RAISE": "LEG_CURL:LEG_CURL",
    "GLUTE_HYPEREXTENSION": "HYPEREXTENSION:SWISS_BALL_HYPEREXTENSION",
    "GLUTE_KICK_BACKS": "HIP_RAISE:HIP_RAISE",
    "HACK_SQUAT": "SQUAT:BARBELL_HACK_SQUAT",
    "HACK_SQUATS": "SQUAT:BARBELL_HACK_SQUAT",
    "HAMMER_CURL": "CURL:DUMBBELL_HAMMER_CURL",
    "HIGH_PULLEY_SINGLE_ARM_BICEP_CURL": "CURL:STANDING_EZ_BAR_BICEPS_CURL",
    "HYPEREXTENSION": "HYPEREXTENSION:SPINE_EXTENSION",
    "INCLINE_DUMBBELL_FLY": "FLYE:INCLINE_DUMBBELL_FLYE",
    "INCLINE_DUMBBELL_PRESS": "BENCH_PRESS:INCLINE_DUMBBELL_BENCH_PRESS",
    "INCLINE_SKULLCRUSHER": "TRICEPS_EXTENSION:LYING_EZ_BAR_TRICEPS_EXTENSION",
    "JUMP_TUCKS": "PLYO:JUMP_SQUAT",
    "LATERAL_RAISE_MACHINE": "LATERAL_RAISE:SEATED_LATERAL_RAISE",
    "LEG_CURLS": "LEG_CURL:LEG_CURL",
    "LEG_PRESS_CALF_RAISE": "CALF_RAISE:SEATED_CALF_RAISE",
    "LOWER_BACK_HYPEREXTENSIONS": "HYPEREXTENSION:SPINE_EXTENSION",
    "LYING_DUMBBELL_TRICEP_EXTENSIONS": "TRICEPS_EXTENSION:DUMBBELL_LYING_TRICEPS_EXTENSION",
    "LYING_LEG_CURL": "LEG_CURL:LEG_CURL",
    "LYING_LEG_CURLS": "LEG_CURL:LEG_CURL",
    "MACHINE_CHEST_PRESS": "BENCH_PRESS:SMITH_MACHINE_BENCH_PRESS",
    "MACHINE_PEC_DECK": "FLYE:DUMBBELL_FLYE",
    "MACHINE_ROW": "ROW:SEATED_CABLE_ROW",
    "MACHINE_SHOULDER_PRESS": "SHOULDER_PRESS:BARBELL_SHOULDER_PRESS",
    "MACHINE_TRICEP_DIP": "TRICEPS_EXTENSION:BODY_WEIGHT_DIP",
    "NARROW_GRIP_BENCH_PRESS": "BENCH_PRESS:CLOSE_GRIP_BARBELL_BENCH_PRESS",
    "NARROW_GRIP_LOW_PULLEY_CABLE_ROW": "ROW:V_GRIP_CABLE_ROW",
    "NARROW_GRIP_PULL_DOWN": "PULL_UP:CLOSE_GRIP_LAT_PULLDOWN",
    "NARROW_GRIP_T_BAR_ROW": "ROW:T_BAR_ROW",
    "PALLOF_PRESS": "CORE:CABLE_CORE_PRESS",
    "PEC_DECK": "FLYE:DUMBBELL_FLYE",
    "PULL_UPS": "PULL_UP:PULL_UP",
    "PULLUP": "PULL_UP:PULL_UP",
    "RACK_DEADLIFTS": "DEADLIFT:RACK_PULL",
    "ROPE_FACE_PULL": "ROW:BANDED_FACE_PULLS",
    "SEATED_ARNOLD_PRESS": "SHOULDER_PRESS:ARNOLD_PRESS",
    "SEATED_DUMBBELL_PRESS": "SHOULDER_PRESS:SEATED_DUMBBELL_SHOULDER_PRESS",
    "SEATED_HAMSTRING_CURL": "LEG_CURL:LEG_CURL",
    "SEATED_LEG_CURL": "LEG_CURL:WEIGHTED_LEG_CURL",
    "SEATED_OVERHEAD_DUMBBELL_PRESS": "SHOULDER_PRESS:OVERHEAD_DUMBBELL_PRESS",
    "SEATED_OVERHEAD_EZ_BAR_TRICEP_EXTENSION": "TRICEPS_EXTENSION:SEATED_EZ_BAR_OVERHEAD_TRICEPS_EXTENSION",
    "SIDE_LATERAL_RAISE": "LATERAL_RAISE:DUMBBELL_LATERAL_RAISE",
    "SINGLE_ARM_CABLE_PRESS_DOWN": "TRICEPS_EXTENSION:TRICEPS_PRESSDOWN",
    "SINGLE_LEG_CALF_PRESS": "CALF_RAISE:SINGLE_LEG_DONKEY_CALF_RAISE",
    "SKULLCRUSHER": "TRICEPS_EXTENSION:LYING_EZ_BAR_TRICEPS_EXTENSION",
    "SMITH_MACHINE_FRONT_SQUAT": "SQUAT:BARBELL_FRONT_SQUAT",
    "SMITH_MACHINE_ROW": "ROW:SEATED_CABLE_ROW",
    "SQUATS": "SQUAT:BACK_SQUATS",
    "STANDING_ALTERNATING_DUMBBELL_HAMMER_CURL": "CURL:STANDING_ALTERNATING_DUMBBELL_CURLS",
    "STANDING_BARBELL_TRICEP_EXTENSION": "TRICEPS_EXTENSION:SEATED_BARBELL_OVERHEAD_TRICEPS_EXTENSION",
    "STANDING_DUMBBELL_PRESS": "SHOULDER_PRESS:OVERHEAD_DUMBBELL_PRESS",
    "STANDING_DUMBBELL_SIDE_LATERAL_RAISE": "LATERAL_RAISE:DUMBBELL_LATERAL_RAISE",
    "STANDING_EZ_BAR_FRONT_RAISE": "LATERAL_RAISE:PLATE_RAISES",
    "STANDING_MACHINE_CALF_RAISE": "CALF_RAISE:STANDING_CALF_RAISE",
    "STANDING_MILITARY_PRESS": "SHOULDER_PRESS:MILITARY_PRESS",
    "STANDING_OVERHEAD_BARBELL_PRESS": "SHOULDER_PRESS:OVERHEAD_BARBELL_PRESS",
    "STIFF_LEG_DEADLIFTS": "DEADLIFT:STRAIGHT_LEG_DEADLIFT",
    "STRAIGHT_ARM_LAT_PULL_DOWN": "PULL_UP:STRAIGHT_ARM_PULLDOWN",
    "STRAIGHT_ARM_ROPE_PULL_DOWN": "PULL_UP:STRAIGHT_ARM_PULLDOWN",
    "T_BAR_MACHINE_ROW": "ROW:T_BAR_ROW",
    "TRICEP_SKULL_CRUSHER": "TRICEPS_EXTENSION:LYING_EZ_BAR_TRICEPS_EXTENSION",
    "V_BAR_LAT_PULL_DOWN": "PULL_UP:LAT_PULLDOWN",
    "WEIGHTED_WIDE_GRIP_PULL_UPS": "PULL_UP:WEIGHTED_WIDE_GRIP_PULL_UP",
    "WIDE_GRIP_PULL_DOWN": "PULL_UP:WIDE_GRIP_LAT_PULLDOWN",
}


def _token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _muscle_group_from_name(label: str) -> str | None:
    """Infer a primary muscle group for mixed Garmin categories and source labels."""
    token = _token(label)
    rules = (
        ("calves", ("CALF",)),
        ("hamstrings_glutes", ("LEG_CURL", "HAMSTRING", "DEADLIFT", "STIFF_LEG", "GLUTE",
                                  "HIP_THRUST", "HIP_RAISE", "ABDUCTOR", "ADDUCTOR",
                                  "HYPEREXTENSION", "GLUTE_HAM")),
        ("quads", ("SQUAT", "LUNGE", "LEG_PRESS", "LEG_EXTENSION", "JUMP_TUCK")),
        ("triceps", ("TRICEP", "SKULLCRUSH", "PRESSDOWN", "PRESS_DOWN", "OVERHEAD_EXTENSION")),
        ("biceps", ("BICEP", "CURL")),
        ("core", ("PLANK", "CRUNCH", "SIT_UP", "AB_WHEEL", "PALLOF", "DEAD_BUG", "CHOP", "CARRY")),
        ("chest", ("BENCH", "CHEST", "PUSH_UP", "PUSHUP", "FLY", "PEC_DECK", "INCLINE_DUMBBELL_PRESS")),
        ("shoulders", ("SHOULDER", "OVERHEAD_PRESS", "MILITARY_PRESS", "ARNOLD_PRESS",
                       "LATERAL_RAISE", "FRONT_RAISE", "REAR_DELT", "UPRIGHT_ROW",
                       "FACE_PULL", "BAND_PULL_APART", "SEATED_DUMBBELL_PRESS",
                       "OVERHEAD_DUMBBELL_PRESS", "OVERHEAD_BARBELL_PRESS")),
        ("back", ("PULL_UP", "PULLUP", "PULL_DOWN", "PULLDOWN", "ROW", "SHRUG", "LAT_PULL", "T_BAR")),
    )
    for muscle_group, markers in rules:
        if any(marker in token for marker in markers):
            return muscle_group
    return None


_raw = json.loads(Path(__file__).with_name("garmin_exercise_catalog.json").read_text(encoding="utf-8"))["exercises"]
GARMIN_EXERCISES: dict[str, dict] = {}
_by_name: dict[str, list[dict]] = {}
_by_label: dict[str, dict] = {}
for item in _raw:
    movement_pattern = _MOVEMENT_BY_CATEGORY.get(item["category"], "other")
    muscle_group = _MUSCLE_BY_CATEGORY.get(item["category"]) or _muscle_group_from_name(item["label"])
    enriched = {**item, "movement_pattern": movement_pattern, "muscle_group": muscle_group}
    GARMIN_EXERCISES[item["key"]] = enriched
    _by_name.setdefault(item["garmin_name"], []).append(enriched)
    label_key = _token(item["label"])
    if label_key not in _by_label or (_by_label[label_key]["movement_pattern"] == "other" and enriched["movement_pattern"] != "other"):
        _by_label[label_key] = enriched


def exercise_metadata(label_or_key: str) -> dict | None:
    token = _token(label_or_key)
    if label_or_key in GARMIN_EXERCISES:
        return GARMIN_EXERCISES[label_or_key]
    alias = _ALIASES.get(token)
    if alias:
        return GARMIN_EXERCISES.get(alias)
    if token in _by_label:
        return _by_label[token]
    matches = _by_name.get(token, [])
    strength_matches = [item for item in matches if item["movement_pattern"] != "other"]
    if len(strength_matches) == 1:
        return strength_matches[0]
    return matches[0] if len(matches) == 1 else None


def exercise_key(label_or_key: str) -> str:
    meta = exercise_metadata(label_or_key)
    return meta["key"] if meta else _token(label_or_key)


def muscle_group_for(label_or_key: str, movement_pattern: str | None = None) -> str | None:
    """Return the best available primary muscle group for an exercise."""
    meta = exercise_metadata(label_or_key)
    if meta and meta.get("muscle_group"):
        return meta["muscle_group"]
    return _muscle_group_from_name(label_or_key) or _MUSCLE_BY_MOVEMENT.get(movement_pattern or "")


def catalog_for_ui() -> list[dict]:
    return sorted(GARMIN_EXERCISES.values(), key=lambda item: (item["label"], item["category"]))
