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

# Friendly source names that are broader than Garmin's exact enum label.
_ALIASES = {
    "BENCH_PRESS": "BENCH_PRESS:BARBELL_BENCH_PRESS",
    "FRONT_SQUAT": "SQUAT:BARBELL_FRONT_SQUAT",
    "INCLINE_BENCH_PRESS": "BENCH_PRESS:INCLINE_BARBELL_BENCH_PRESS",
    "DECLINE_BENCH_PRESS": "BENCH_PRESS:DECLINE_BARBELL_BENCH_PRESS",
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
    "LEG_PRESS_CALF_RAISE": "CALF_RAISE:LEG_PRESS_CALF_RAISE",
    "AB_WHEEL_ROLL_OUT": "CORE:AB_WHEEL",
}


def _token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


_raw = json.loads(Path(__file__).with_name("garmin_exercise_catalog.json").read_text(encoding="utf-8"))["exercises"]
GARMIN_EXERCISES: dict[str, dict] = {}
_by_name: dict[str, list[dict]] = {}
_by_label: dict[str, dict] = {}
for item in _raw:
    enriched = {**item, "movement_pattern": _MOVEMENT_BY_CATEGORY.get(item["category"], "other")}
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


def catalog_for_ui() -> list[dict]:
    return sorted(GARMIN_EXERCISES.values(), key=lambda item: (item["label"], item["category"]))
