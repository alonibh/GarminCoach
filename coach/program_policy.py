"""Versioned operating rules for the curated source programs.

Weekday labels on source pages are deliberately not represented here. Rest is
expressed relative to the last completed session so sequence state survives
missed days and calendar-week boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


POLICY_VERSION = "2026-07-22.3"
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
    "ppl_6": ProgramPolicy(
        "ppl_6",
        "https://www.muscleandstrength.com/workouts/6-day-push-pull-legs-planet-fitness-workout",
        ("Push A", "Pull A", "Legs A", "Push B", "Pull B", "Legs B"),
        (0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 0, 1),
    ),
    "dumbbell_full_body_3": ProgramPolicy(
        "dumbbell_full_body_3",
        "https://www.muscleandstrength.com/workouts/3-day-full-body-dumbbell-workout",
        ("Full Body 1", "Full Body 2", "Full Body 3"),
        (1, 1, 1), (1, 1, 1),
        source_duration_weeks=8, source_reviewed_at="2026-07-22",
    ),
    "planet_fitness_full_body_3": ProgramPolicy(
        "planet_fitness_full_body_3", "https://www.muscleandstrength.com/workouts/3-day-full-body-planet-fitness-workout",
        ("Full Body A", "Full Body B", "Full Body C"), (1, 1, 1), (1, 1, 1), source_reviewed_at="2026-07-22",
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
    "dumbbell_upper_lower_4": ProgramPolicy(
        "dumbbell_upper_lower_4", "https://www.muscleandstrength.com/workouts/dumbbell-only-upper-lower-workout-routine",
        ("Upper A", "Lower A", "Upper B", "Lower B"), (0, 1, 0, 2), (0, 1, 0, 2), source_duration_weeks=12,
        source_reviewed_at="2026-07-22",
    ),
    "barbell_no_rack_4": ProgramPolicy(
        "barbell_no_rack_4", "https://www.muscleandstrength.com/workouts/4-day-barbell-only-workout",
        ("Upper A", "Lower A", "Upper B", "Lower B"), (0, 1, 0, 2), (0, 1, 0, 2), source_duration_weeks=8,
        source_reviewed_at="2026-07-22", omitted_source_components=("Optional direct biceps and hip-abduction accessories",),
    ),
    "barbell_upper_lower_4": ProgramPolicy(
        "barbell_upper_lower_4", "https://www.muscleandstrength.com/workouts/home-gym-barbell-workout-routine",
        ("Lower A", "Upper A", "Lower B", "Upper B"), (0, 1, 0, 2), (0, 1, 0, 2), source_duration_weeks=10,
        source_reviewed_at="2026-07-22",
    ),
    "maul_5": ProgramPolicy(
        "maul_5", "https://www.muscleandstrength.com/workouts/maul-workout",
        ("Upper Mechanical", "Lower Mechanical", "Upper Full", "Shoulders & Arms", "Lower Full"),
        (0, 0, 0, 0, 1), (0, 0, 0, 0, 1), source_duration_weeks=12, source_reviewed_at="2026-07-22",
        omitted_source_components=("Non-defining isolation accessories",),
    ),
    "dumbbell_split_5": ProgramPolicy(
        "dumbbell_split_5", "https://www.muscleandstrength.com/workouts/5-day-dumbbell-only-workout-split",
        ("Chest, Shoulders & Triceps", "Legs & Core A", "Back & Biceps", "Legs & Core B", "Complete Upper Body"),
        (0, 0, 0, 0, 2), (0, 0, 0, 0, 2), source_duration_weeks=12, source_reviewed_at="2026-07-22",
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


# Values are copied from each source page's Workout Summary. The catalog does
# not independently grade a routine from its exercise selection or schedule.
SOURCE_TRAINING_LEVELS: Mapping[str, str] = {
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

# Every selectable routine needs an affirmative sustainable-default review.
# Adding a policy alone is insufficient; the admission record is also required.
DEFAULT_ROUTINE_ADMISSION: Mapping[str, str] = {
    key: "repeatable_base_template" for key in (
        "full_body_2", "beginner_full_body_3", "ms_full_body_3", "total_package_3",
        "upper_lower_4", "shul_4", "split_full_4", "ppl_6",
        "dumbbell_full_body_3", "planet_fitness_full_body_3",
        "whole_body_toning_3", "planet_fitness_upper_lower_4", "optimized_volume_4",
        "phul_4", "dumbbell_upper_lower_4", "barbell_no_rack_4", "barbell_upper_lower_4",
        "maul_5", "dumbbell_split_5", "built_different_ppl_6", "muscle_mania_6",
    )
}

REJECTED_DEFAULT_ROUTINES: Mapping[str, str] = {
    "hundred_rep_full_body_2": "temporary four-week 100-rep challenge",
    "muscle_rebound_4": "return-from-hiatus transition block",
    "rp21_4": "mandatory mesocycle and deload behavior is not implemented",
    "advanced_upper_lower_4": "source explicitly says it is not for long-duration use",
    "body_fat_demolition_5": "calorie-deficit fat-loss phase rather than a general default",
}

_SOURCE_LEVEL_TO_EXPERIENCE = {
    "Beginner": "new",
    "Intermediate": "six_to_twenty_four_months",
    "Advanced": "two_plus_years",
}


def validate_program_policies(programs: Mapping[str, dict]) -> None:
    """Fail fast when a curated template and its reviewed policy diverge."""
    if set(programs) != set(PROGRAM_POLICIES):
        raise ValueError("Every curated program must have exactly one policy")
    if set(programs) != set(DEFAULT_ROUTINE_ADMISSION):
        raise ValueError("Every curated program must pass the sustainable-default admission review")
    if set(programs) != set(SOURCE_TRAINING_LEVELS):
        raise ValueError("Every curated program must record the source-published training level")
    rejected = set(programs) & set(REJECTED_DEFAULT_ROUTINES)
    if rejected:
        raise ValueError(f"Temporary or phase-specific routines cannot be defaults: {sorted(rejected)}")
    for key, program in programs.items():
        policy = PROGRAM_POLICIES[key]
        names = tuple(item["name"] for item in program["sessions"])
        if names != policy.session_names or program["source_url"] != policy.source_url:
            raise ValueError(f"Program policy does not match curated template: {key}")
        source_level = SOURCE_TRAINING_LEVELS[key]
        if program["experience"] != _SOURCE_LEVEL_TO_EXPERIENCE[source_level]:
            raise ValueError(f"Catalog level differs from the source-published level: {key}")
        size = len(names)
        if len(policy.minimum_rest_days_after) != size or len(policy.preferred_rest_days_after) != size:
            raise ValueError(f"Program policy rest rules have wrong length: {key}")
        if any(value < 0 for value in (*policy.minimum_rest_days_after, *policy.preferred_rest_days_after)):
            raise ValueError(f"Program policy rest rules cannot be negative: {key}")
        if any(preferred < minimum for minimum, preferred in zip(
            policy.minimum_rest_days_after, policy.preferred_rest_days_after
        )):
            raise ValueError(f"Preferred rest cannot be shorter than minimum rest: {key}")
        duration = policy.source_duration_weeks
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 1 <= duration <= 52
        ):
            raise ValueError(f"Program policy source duration is invalid: {key}")
