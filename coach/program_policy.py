"""Versioned operating rules for the curated source programs.

Weekday labels on source pages are deliberately not represented here. Rest is
expressed relative to the last completed session so sequence state survives
missed days and calendar-week boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


POLICY_VERSION = "2026-07-22.2"
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
        ("Full Body 1", "Full Body 2"), (0, 0), (1, 1),
        consecutive_day_override_allowed=True,
    ),
    "beginner_full_body_3": ProgramPolicy(
        "beginner_full_body_3",
        "https://www.muscleandstrength.com/workouts/3-day-workout-routine-and-diet-for-beginners",
        ("Full Body 1", "Full Body 2", "Full Body 3"), (1, 1, 1), (1, 1, 1),
    ),
    "ms_full_body_3": ProgramPolicy(
        "ms_full_body_3",
        "https://www.muscleandstrength.com/workouts/muscle-strength-full-body-workout-routine",
        ("Full Body 1", "Full Body 2", "Full Body 3"), (1, 1, 1), (1, 1, 1), _EASY_WALK,
    ),
    "total_package_3": ProgramPolicy(
        "total_package_3",
        "https://www.muscleandstrength.com/workouts/total-package-workout",
        ("Full Body 1", "Full Body 2", "Full Body 3"), (1, 1, 1), (1, 1, 1), _SOURCE_WALK,
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
    "hundred_rep_full_body_2": ProgramPolicy(
        "hundred_rep_full_body_2",
        "https://www.muscleandstrength.com/workouts/100-reps-set-shocker-fullbody-workout",
        ("100-Rep Full Body 1", "100-Rep Full Body 2"),
        (2, 2), (2, 2),
        source_duration_weeks=4,
        source_reviewed_at="2026-07-22",
        omitted_source_components=("Optional lagging-muscle-group version",),
    ),
    "planet_fitness_full_body_3": ProgramPolicy(
        "planet_fitness_full_body_3", "https://www.muscleandstrength.com/workouts/3-day-full-body-planet-fitness-workout",
        ("Full Body A", "Full Body B", "Full Body C"), (1, 1, 1), (1, 1, 1), source_reviewed_at="2026-07-22",
    ),
    "long_cycle_full_body_3": ProgramPolicy(
        "long_cycle_full_body_3", "https://www.muscleandstrength.com/workouts/beginner-long-cycle-muscle-strength-building-workout",
        ("Workout 1", "Workout 2", "Workout 3"), (1, 1, 1), (1, 1, 1), source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining isolation accessories",),
    ),
    "whole_body_toning_3": ProgramPolicy(
        "whole_body_toning_3", "https://www.muscleandstrength.com/workouts/3-day-whole-body-toning-workout.html",
        ("Series 1", "Series 2", "Series 3"), (1, 1, 1), (1, 1, 1), source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining arm and calf isolation accessories",),
    ),
    "planet_fitness_upper_lower_4": ProgramPolicy(
        "planet_fitness_upper_lower_4", "https://www.muscleandstrength.com/workouts/4-day-upper-lower-planet-fitness-workout",
        ("Upper A", "Lower A", "Upper B", "Lower B"), (0, 1, 0, 2), (0, 1, 0, 2), source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining isolation accessories",),
    ),
    "optimized_volume_4": ProgramPolicy(
        "optimized_volume_4", "https://www.muscleandstrength.com/workouts/ovw-workout",
        ("Upper 1", "Lower 1", "Upper 2", "Lower 2"), (0, 1, 0, 2), (0, 1, 0, 2), source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining arm and core accessories",),
    ),
    "phul_4": ProgramPolicy(
        "phul_4", "https://www.muscleandstrength.com/workouts/phul-workout",
        ("Upper Power", "Lower Power", "Upper Hypertrophy", "Lower Hypertrophy"), (0, 1, 0, 2), (0, 1, 0, 2),
        source_duration_weeks=12, source_reviewed_at="2026-07-22", omitted_source_components=("Non-defining arm and calf isolation accessories",),
    ),
    "muscle_rebound_4": ProgramPolicy(
        "muscle_rebound_4", "https://www.muscleandstrength.com/workouts/muscle-rebound-workout",
        ("Pull", "Push", "Legs", "Full Body"), (0, 0, 1, 2), (0, 0, 1, 2), source_duration_weeks=6,
        source_reviewed_at="2026-07-22", omitted_source_components=("Source supersets are represented as ordered straight sets", "Explosive squat-jump accessory"),
    ),
    "rp21_4": ProgramPolicy(
        "rp21_4", "https://www.muscleandstrength.com/workouts/4-day-rp21-rest-pause-workout-system",
        ("Lower 1", "Upper 1", "Lower 2", "Upper 2"), (0, 1, 0, 2), (0, 1, 0, 2), source_duration_weeks=4,
        source_reviewed_at="2026-07-22", omitted_source_components=("Optional one or two conditioning days", "Non-defining arm accessories"),
    ),
    "advanced_upper_lower_4": ProgramPolicy(
        "advanced_upper_lower_4", "https://www.muscleandstrength.com/workouts/4-day-advanced-upper-lower-workout-program-to-build-mass",
        ("Upper A", "Lower A", "Upper B", "Lower B"), (0, 1, 0, 2), (0, 1, 0, 2), source_duration_weeks=12,
        source_reviewed_at="2026-07-22", omitted_source_components=("Non-defining isolation accessories",),
    ),
    "maul_5": ProgramPolicy(
        "maul_5", "https://www.muscleandstrength.com/workouts/maul-workout",
        ("Upper Mechanical", "Lower Mechanical", "Upper Full", "Shoulders & Arms", "Lower Full"),
        (0, 0, 0, 0, 1), (0, 0, 0, 0, 1), source_duration_weeks=12, source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining isolation accessories",),
    ),
    "body_fat_demolition_5": ProgramPolicy(
        "body_fat_demolition_5", "https://www.muscleandstrength.com/workouts/8-week-body-fat-demolition-workout",
        ("Upper A", "Lower A", "Upper B", "Lower B", "Full Body"), (0, 1, 0, 0, 1), (0, 1, 0, 0, 1),
        source_duration_weeks=8, source_reviewed_at="2026-07-22", omitted_source_components=("Optional cardio guidance", "Non-defining isolation accessories"),
    ),
    "powerbuilding_ppl_6": ProgramPolicy(
        "powerbuilding_ppl_6", "https://www.muscleandstrength.com/workouts/6-day-powerbuilding-split-meal-plan",
        ("Push A", "Pull A", "Legs A", "Push B", "Pull B", "Legs B"), (0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 0, 1),
        source_duration_weeks=12, source_reviewed_at="2026-07-22", omitted_source_components=("Meal plan", "Non-defining isolation accessories"),
    ),
    "low_volume_high_intensity_6": ProgramPolicy(
        "low_volume_high_intensity_6", "https://www.muscleandstrength.com/workouts/6-day-low-volume-high-intensity-workout-split",
        ("Chest & Triceps", "Back Thickness", "Quads", "Shoulders & Biceps", "Back Width", "Hamstrings"),
        (0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 0, 1), source_duration_weeks=6, source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining isolation accessories",),
    ),
    "built_different_ppl_6": ProgramPolicy(
        "built_different_ppl_6", "https://www.muscleandstrength.com/workouts/built-different-ppl-workout",
        ("Push 1", "Pull 1", "Legs 1", "Push 2", "Pull 2", "Legs 2"), (0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 0, 1),
        source_duration_weeks=10, source_reviewed_at="2026-07-22", omitted_source_components=("Optional morning low-intensity cardio", "User-selected core or rear-delt slots"),
    ),
    "muscle_mania_6": ProgramPolicy(
        "muscle_mania_6", "https://www.muscleandstrength.com/workouts/muscle-mania-10-week-muscle-growth-workout",
        ("Upper 1", "Lower 1", "Upper 2", "Lower 2", "Upper 3", "Lower 3"), (0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 0, 1),
        source_duration_weeks=10, source_reviewed_at="2026-07-22", omitted_source_components=("Non-defining isolation accessories",),
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
