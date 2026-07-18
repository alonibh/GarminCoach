"""Versioned operating rules for the curated source programs.

Weekday labels on source pages are deliberately not represented here. Rest is
expressed relative to the last completed session so sequence state survives
missed days and calendar-week boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


POLICY_VERSION = "2026-07-18.1"
RECOVERY_EVIDENCE_URL = "https://pubmed.ncbi.nlm.nih.gov/29755363/"


@dataclass(frozen=True)
class RecoveryActivity:
    label: str
    duration_min: tuple[int, int]
    instruction: str
    evidence_url: str = RECOVERY_EVIDENCE_URL
    evidence_status: str = "optional_low_fatigue_no_recovery_promise"
    source_location: str = "source program rest-day guidance"


@dataclass(frozen=True)
class ProgramPolicy:
    program_key: str
    source_url: str
    session_names: tuple[str, ...]
    minimum_rest_days_after: tuple[int, ...]
    preferred_rest_days_after: tuple[int, ...]
    recovery_activity: RecoveryActivity | None = None
    consecutive_day_override_allowed: bool = False
    source_duration_weeks: int | None = None
    source_reviewed_at: str = "2026-07-18"
    progression_policy: str = "source_metadata_only_no_automatic_weight_changes"
    substitution_policy: str = "user_confirmed_only"
    identity_rule: str = "all_configured_exercises_unique_within_active_program"
    adaptation_label: str = "rolling_schedule_adaptation"
    omitted_source_components: tuple[str, ...] = ()
    version: str = POLICY_VERSION


_EASY_WALK = RecoveryActivity(
    label="Optional easy walking",
    duration_min=(20, 30),
    instruction=(
        "Walk at conversational effort. This is optional light movement, not a "
        "claim that recovery will be faster."
    ),
)
_SOURCE_WALK = RecoveryActivity(
    label="Optional easy walking",
    duration_min=(30, 45),
    instruction=(
        "Walk at conversational effort; do not add weights. This is optional "
        "light movement, not a claim that recovery will be faster."
    ),
)


PROGRAM_POLICIES: Mapping[str, ProgramPolicy] = {
    "full_body_2": ProgramPolicy(
        "full_body_2",
        "https://www.muscleandstrength.com/workouts/a-b-2-day-workout-for-busy-people",
        ("Workout A", "Workout B"), (0, 0), (1, 1),
        consecutive_day_override_allowed=True,
    ),
    "beginner_full_body_3": ProgramPolicy(
        "beginner_full_body_3",
        "https://www.muscleandstrength.com/workouts/3-day-workout-routine-and-diet-for-beginners",
        ("Full Body A", "Full Body B", "Full Body C"), (1, 1, 1), (1, 1, 1),
    ),
    "ms_full_body_3": ProgramPolicy(
        "ms_full_body_3",
        "https://www.muscleandstrength.com/workouts/muscle-strength-full-body-workout-routine",
        ("Workout A", "Workout B", "Workout C"), (1, 1, 1), (1, 1, 1), _EASY_WALK,
    ),
    "total_package_3": ProgramPolicy(
        "total_package_3",
        "https://www.muscleandstrength.com/workouts/total-package-workout",
        ("Day 1", "Day 2", "Day 3"), (1, 1, 1), (1, 1, 1), _SOURCE_WALK,
    ),
    "upper_lower_4": ProgramPolicy(
        "upper_lower_4",
        "https://www.muscleandstrength.com/workouts/upper-lower-4-day-gym-bodybuilding-workout",
        ("Upper A", "Lower A", "Upper B", "Lower B"), (0, 1, 0, 2), (0, 1, 0, 2), _EASY_WALK,
    ),
    "shul_4": ProgramPolicy(
        "shul_4",
        "https://www.muscleandstrength.com/workouts/shul-workout",
        ("Lower Strength", "Upper Strength", "Lower Hypertrophy", "Upper Hypertrophy"),
        (0, 1, 0, 2), (0, 1, 0, 2), _EASY_WALK,
    ),
    "split_full_4": ProgramPolicy(
        "split_full_4",
        "https://www.muscleandstrength.com/workouts/4-day-workout-to-build-muscle",
        ("Back & Biceps", "Legs", "Chest, Shoulders & Triceps", "Full Body"),
        (0, 0, 1, 2), (0, 0, 1, 2),
    ),
    "muscle_strength_5": ProgramPolicy(
        "muscle_strength_5",
        "https://www.muscleandstrength.com/workouts/5-day-muscle-and-strength-building-workout-split",
        ("Upper Strength", "Lower Strength", "Back & Shoulders Size", "Chest & Arms Size", "Legs Size"),
        (0, 1, 0, 0, 1), (0, 1, 0, 0, 1),
        omitted_source_components=("Optional separate three-times-weekly ab workout",),
    ),
    "ppl_6": ProgramPolicy(
        "ppl_6",
        "https://www.muscleandstrength.com/workouts/6-day-push-pull-legs-planet-fitness-workout",
        ("Push A", "Pull A", "Legs A", "Push B", "Pull B", "Legs B"),
        (0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 0, 1),
    ),
}


def validate_program_policies(programs: Mapping[str, dict]) -> None:
    """Fail fast when a curated template and its reviewed policy diverge."""
    if set(programs) != set(PROGRAM_POLICIES):
        raise ValueError("Every curated program must have exactly one policy")
    for key, program in programs.items():
        policy = PROGRAM_POLICIES[key]
        names = tuple(item["name"] for item in program["sessions"])
        if names != policy.session_names or program["source_url"] != policy.source_url:
            raise ValueError(f"Program policy does not match curated template: {key}")
        size = len(names)
        if len(policy.minimum_rest_days_after) != size or len(policy.preferred_rest_days_after) != size:
            raise ValueError(f"Program policy rest rules have wrong length: {key}")
        if any(value < 0 for value in (*policy.minimum_rest_days_after, *policy.preferred_rest_days_after)):
            raise ValueError(f"Program policy rest rules cannot be negative: {key}")
        if any(preferred < minimum for minimum, preferred in zip(
            policy.minimum_rest_days_after, policy.preferred_rest_days_after
        )):
            raise ValueError(f"Preferred rest cannot be shorter than minimum rest: {key}")
