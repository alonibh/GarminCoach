"""Small, reviewable strength-program library used during onboarding.

These are deliberately conservative starting templates.  They are not a
replacement for the athlete's approval or for later session-by-session
adaptation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


ACSM_ATTRIBUTION = "Built from ACSM resistance-training guidance; adapted to your setup."
ACSM_SOURCE_URL = "https://acsm.org/resistance-training-guidelines-update-2026/"


def _exercise(name: str, sets: int, reps: int, notes: str = "") -> dict[str, Any]:
    return {"exercise_name": name, "sets": sets, "reps": reps, "notes": notes}


def _session(name: str, focus: str, exercises: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "sport_type": "strength_training",
        "duration_min": 60,
        "focus_tags": [focus, "strength"],
        "exercises": exercises,
    }


PROGRAMS: dict[str, dict[str, Any]] = {
    "full_body_2": {
        "name": "Full body strength · 2 days",
        "sessions": [
            _session("Full body A", "full body", [
                _exercise("SQUAT", 3, 8), _exercise("BENCH_PRESS", 3, 8),
                _exercise("BENT_OVER_ROW", 3, 10), _exercise("PLANK", 3, 30, "seconds"),
            ]),
            _session("Full body B", "full body", [
                _exercise("ROMANIAN_DEADLIFT", 3, 8), _exercise("OVERHEAD_PRESS", 3, 8),
                _exercise("LAT_PULL_DOWN", 3, 10), _exercise("LUNGE", 2, 10, "each side"),
            ]),
        ],
    },
    "full_body_3": {
        "name": "Full body strength · 3 days",
        "sessions": [
            _session("Full body A", "full body", [
                _exercise("SQUAT", 3, 8), _exercise("BENCH_PRESS", 3, 8),
                _exercise("BENT_OVER_ROW", 3, 10),
            ]),
            _session("Full body B", "full body", [
                _exercise("ROMANIAN_DEADLIFT", 3, 8), _exercise("OVERHEAD_PRESS", 3, 8),
                _exercise("LAT_PULL_DOWN", 3, 10),
            ]),
            _session("Full body C", "full body", [
                _exercise("LEG_PRESS", 3, 10), _exercise("INCLINE_BENCH_PRESS", 3, 10),
                _exercise("SEATED_CABLE_ROW", 3, 10), _exercise("PLANK", 3, 30, "seconds"),
            ]),
        ],
    },
    "upper_lower_4": {
        "name": "Upper / lower strength · 4 days",
        "sessions": [
            _session("Upper A", "upper body", [_exercise("BENCH_PRESS", 3, 8), _exercise("BENT_OVER_ROW", 3, 8), _exercise("OVERHEAD_PRESS", 2, 10)]),
            _session("Lower A", "lower body", [_exercise("SQUAT", 3, 8), _exercise("ROMANIAN_DEADLIFT", 3, 8), _exercise("CALF_RAISE", 3, 12)]),
            _session("Upper B", "upper body", [_exercise("INCLINE_BENCH_PRESS", 3, 10), _exercise("LAT_PULL_DOWN", 3, 10), _exercise("LATERAL_RAISE", 2, 12)]),
            _session("Lower B", "lower body", [_exercise("DEADLIFT", 3, 5), _exercise("LUNGE", 3, 10, "each side"), _exercise("LEG_CURL", 3, 10)]),
        ],
    },
    "push_pull_legs_3": {
        "name": "Push / pull / legs · 3 days",
        "sessions": [
            _session("Push", "push", [_exercise("BENCH_PRESS", 3, 8), _exercise("OVERHEAD_PRESS", 3, 8), _exercise("TRICEPS_EXTENSION", 2, 12)]),
            _session("Pull", "pull", [_exercise("ROMANIAN_DEADLIFT", 3, 8), _exercise("LAT_PULL_DOWN", 3, 10), _exercise("BICEP_CURL", 2, 12)]),
            _session("Legs", "lower body", [_exercise("SQUAT", 3, 8), _exercise("LUNGE", 3, 10, "each side"), _exercise("CALF_RAISE", 3, 12)]),
        ],
    },
    "sport_support_2": {
        "name": "Strength to support your sport · 2 days",
        "sessions": [
            _session("Strength A", "full body", [_exercise("SQUAT", 3, 6), _exercise("BENCH_PRESS", 3, 8), _exercise("BENT_OVER_ROW", 3, 8), _exercise("PLANK", 3, 30, "seconds")]),
            _session("Strength B", "full body", [_exercise("ROMANIAN_DEADLIFT", 3, 8), _exercise("OVERHEAD_PRESS", 3, 8), _exercise("LAT_PULL_DOWN", 3, 10), _exercise("LUNGE", 2, 8, "each side")]),
        ],
    },
    "minimal_equipment_2": {
        "name": "Minimal-equipment full body · 2 days",
        "sessions": [
            _session("Full body A", "full body", [_exercise("BODYWEIGHT_SQUAT", 3, 12), _exercise("PUSH_UP", 3, 10), _exercise("INVERTED_ROW", 3, 10), _exercise("PLANK", 3, 30, "seconds")]),
            _session("Full body B", "full body", [_exercise("LUNGE", 3, 10, "each side"), _exercise("DUMBBELL_ROW", 3, 10), _exercise("DUMBBELL_SHOULDER_PRESS", 3, 10), _exercise("GLUTE_BRIDGE", 3, 12)]),
        ],
    },
}


def recommend_program(
    *,
    goal: str,
    limitations: str,
    days_per_week: int,
    session_duration_min: int,
    history_summary: str,
) -> dict[str, Any]:
    """Return one deterministic, editable starting program and its rationale."""
    if goal == "Improve a sport/activity" and days_per_week <= 2:
        key = "sport_support_2"
    elif days_per_week >= 4:
        key = "upper_lower_4"
    elif days_per_week == 3:
        key = "full_body_3"
    else:
        key = "full_body_2"

    template = PROGRAMS[key]
    sessions = deepcopy(template["sessions"])
    for session in sessions:
        session["session_role"] = "coach_strength"
        session["target_frequency"] = 1
        session["duration_min"] = session_duration_min
        if session_duration_min <= 45:
            session["exercises"] = session["exercises"][:3]
    reasons = [
        f"Your goal is {goal or 'to build a consistent strength routine'}.",
        history_summary,
        f"It starts with {len(sessions)} strength sessions in sequence, without assigning dates.",
    ]
    if goal == "Improve a sport/activity":
        reasons.append("The gym work is kept conservative so daily coaching can adapt it to your recent training load.")
    if session_duration_min <= 45:
        reasons.append(f"Sessions are trimmed to fit your {session_duration_min}-minute limit.")

    if "overhead" in limitations.lower():
        for session in sessions:
            for exercise in session["exercises"]:
                if exercise["exercise_name"] in {"OVERHEAD_PRESS", "DUMBBELL_SHOULDER_PRESS"}:
                    exercise["exercise_name"] = "PUSH_UP"
                    exercise["notes"] = "Overhead press replaced from your limitation."
        reasons.append("Overhead pressing was removed from the starting plan based on your limitation.")
    elif limitations.strip():
        reasons.append("Your limitations are saved for review before progression or scheduling.")
    reasons.append("Starting weights stay open unless recent Garmin strength data provides a trustworthy baseline.")

    return {
        "key": key,
        "name": template["name"],
        "sessions": sessions,
        "strength_session_count": len(template["sessions"]),
        "attribution": ACSM_ATTRIBUTION,
        "source_url": ACSM_SOURCE_URL,
        "rationale": " ".join(part for part in reasons if part),
    }
