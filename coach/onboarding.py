"""Onboarding helpers for generic, user-confirmed coaching setup."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import re
from typing import Any

from sqlalchemy.orm import Session

from db import Activity, ExerciseSet, ProgramSession, TrainingProgram, Workout
from coach.exercises import exercise_metadata
from coach.programs import PROGRAMS
from coach.workout_identity import user_workouts_query


COMMON_ACTIVITY_LABELS = {
    "strength": "Strength",
    "weight": "Strength",
    "run": "Running",
    "cycl": "Cycling",
    "walk": "Walking",
    "swim": "Swimming",
    "soccer": "Soccer",
    "yoga": "Yoga / mobility",
    "pilates": "Yoga / mobility",
}

ENDURANCE_FAMILIES = {"Running", "Cycling", "Swimming", "Walking"}
SPORT_FAMILIES = {"Soccer"}
DEFAULT_WEEKDAYS = ["Monday", "Wednesday", "Friday"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SPLIT_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

_EXERCISE_GROUPS = {
    "lower": ("SQUAT", "DEADLIFT", "LUNGE", "LEG_", "CALF", "HIP_THRUST", "GLUTE"),
    "push": ("BENCH", "CHEST", "OVERHEAD_PRESS", "SHOULDER_PRESS", "PUSH_UP", "TRICEPS"),
    "pull": ("ROW", "PULL", "BICEP", "REAR_DELT"),
}
_NAME_PATTERNS = (("push", "push"), ("pull", "pull"), ("leg", "lower"), ("lower", "lower"), ("upper", "upper"), ("full body", "full_body"))

TRAINING_TYPE_LABELS = {
    "strength_focused": "Strength focused",
    "endurance_focused": "Endurance focused",
    "mixed_fitness": "Mixed fitness",
    "sport_recreational": "Sport recreational",
    "low_history": "Not enough history yet",
}

GOAL_BY_TYPE = {
    "strength_focused": "Build strength & muscle",
    "endurance_focused": "Improve a sport/activity",
    "mixed_fitness": "Feel fitter & more consistent",
    "sport_recreational": "Improve a sport/activity",
    "low_history": "Feel fitter & more consistent",
}

PROGRAM_BY_TYPE = {
    "strength_focused": "Strength routine",
    "endurance_focused": "Running plan",
    "mixed_fitness": "Mixed fitness routine",
    "sport_recreational": "Sport routine",
    "low_history": "My routine",
}


def activity_family(activity_type: str | None) -> str:
    raw = (activity_type or "").lower()
    for hint, label in COMMON_ACTIVITY_LABELS.items():
        if hint in raw:
            return label
    return (activity_type or "Other").replace("_", " ").title()


def clean_session_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"^[^\w\s]+\s*", "", name).strip()
    cleaned = re.sub(r"\s*@\s*\d{1,2}:\d{2}\s*$", "", cleaned).strip()
    return cleaned


def _usable_completed_activities(activities: list[Activity]) -> list[Activity]:
    return [
        a for a in activities
        if a.start_time and a.activity_type and activity_family(a.activity_type) != "Other"
        and (a.duration_s is not None and a.duration_s > 0)
    ]


def _activity_patterns(activities: list[Activity]) -> list[dict[str, Any]]:
    activities = _usable_completed_activities(activities)
    family_counts = Counter(activity_family(a.activity_type) for a in activities)
    weekly_counts: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for a in activities:
        if a.start_time:
            iso_year, iso_week, _ = a.start_time.isocalendar()
            weekly_counts[activity_family(a.activity_type)].add((iso_year, iso_week))

    activity_patterns = []
    for label, count in family_counts.most_common():
        weeks = max(len(weekly_counts.get(label, set())), 1)
        activity_patterns.append(
            {
                "label": label,
                "sessions": count,
                "avg_per_week": round(count / weeks, 1),
            }
        )
    return activity_patterns


def _history_span_weeks(activities: list[Activity]) -> float:
    dates = [a.start_time.date() for a in _usable_completed_activities(activities)]
    if not dates:
        return 1.0
    return max(((max(dates) - min(dates)).days + 1) / 7, 1.0)


def _experience_level(total_sessions: int, avg_per_week: float, template_count: int) -> str:
    if total_sessions >= 80 or template_count >= 8:
        return "two_plus_years"
    # Frequency is evidence of training age only after there is enough history
    # to show a recurring habit. A single busy week must not promote a new
    # athlete into an established routine.
    if total_sessions >= 20 or (total_sessions >= 12 and avg_per_week >= 1.5) or template_count >= 2:
        return "six_to_twenty_four_months"
    return "new"


def _classify_history(activities: list[Activity]) -> dict[str, Any]:
    activities = _usable_completed_activities(activities)
    total = len(activities)
    if total < 3:
        return {
            "training_type": "low_history",
            "label": TRAINING_TYPE_LABELS["low_history"],
            "confidence": 0.35,
            "confidence_label": "Low",
            "reason": "There are fewer than 3 synced activities, so GarminCoach will start conservatively.",
        }

    counts = Counter(activity_family(a.activity_type) for a in activities)
    strength = counts.get("Strength", 0)
    endurance = sum(counts.get(f, 0) for f in ENDURANCE_FAMILIES)
    sport = sum(counts.get(f, 0) for f in SPORT_FAMILIES)
    top_label, top_count = counts.most_common(1)[0]

    if sport >= max(3, total * 0.45):
        training_type = "sport_recreational"
    elif strength >= total * 0.55:
        training_type = "strength_focused"
    elif endurance >= total * 0.55:
        training_type = "endurance_focused"
    elif strength >= max(2, total * 0.25) and endurance >= max(2, total * 0.25):
        training_type = "mixed_fitness"
    elif top_label == "Strength":
        training_type = "strength_focused"
    elif top_label in ENDURANCE_FAMILIES:
        training_type = "endurance_focused"
    elif top_label in SPORT_FAMILIES:
        training_type = "sport_recreational"
    else:
        training_type = "mixed_fitness"

    confidence = max(0.45, min(0.95, top_count / total))
    confidence_label = "High" if confidence >= 0.7 else "Medium" if confidence >= 0.5 else "Low"
    return {
        "training_type": training_type,
        "label": TRAINING_TYPE_LABELS[training_type],
        "confidence": round(confidence, 2),
        "confidence_label": confidence_label,
        "reason": f"{top_count} of {total} synced activities are {top_label}.",
    }


def _preferred_activities(training_type: str, counts: Counter[str]) -> list[str]:
    detected = [label for label, _ in counts.most_common() if label != "Other"]
    if detected:
        return detected[:4]
    if training_type == "strength_focused":
        return ["Strength"]
    if training_type == "endurance_focused":
        return ["Running"]
    if training_type == "sport_recreational":
        return ["Soccer", "Strength"]
    if training_type == "mixed_fitness":
        return ["Strength", "Running"]
    return ["Strength", "Running", "Walking"]


def _equipment_defaults(training_type: str, preferred: list[str], templates: list[Workout]) -> list[str]:
    equipment = []
    if "Strength" in preferred or any(activity_family(w.sport_type) == "Strength" for w in templates):
        equipment.append("gym")
    if any(p in ENDURANCE_FAMILIES or p in SPORT_FAMILIES for p in preferred):
        equipment.append("outdoor")
    if training_type in {"mixed_fitness", "low_history"}:
        equipment.append("bodyweight")
    return list(dict.fromkeys(equipment or ["bodyweight"]))


def _selected_template_ids(
    templates: list[Workout],
    preferred_activities: list[str],
    training_type: str,
) -> list[int]:
    preferred = set(preferred_activities)
    selected = [w.workout_id for w in templates if activity_family(w.sport_type) in preferred]
    if selected:
        return selected
    if training_type != "low_history" and templates:
        return [w.workout_id for w in templates[:6]]
    return []


def _preferred_weekdays(activities: list[Activity], days_per_week: int) -> list[str]:
    counts = Counter(WEEKDAYS[a.start_time.weekday()] for a in activities if a.start_time)
    if counts:
        return [day for day, _ in counts.most_common(days_per_week)]
    return DEFAULT_WEEKDAYS[:days_per_week]


def _preferred_time_of_day(activities: list[Activity]) -> str:
    buckets = Counter()
    for activity in activities:
        if not activity.start_time:
            continue
        hour = activity.start_time.hour
        if hour < 11:
            buckets["morning"] += 1
        elif hour < 16:
            buckets["midday"] += 1
        elif hour < 21:
            buckets["evening"] += 1
        else:
            buckets["night"] += 1
    return buckets.most_common(1)[0][0] if buckets else "evening"


def _coach_strength_budget(activities: list[Activity]) -> int:
    strength = [a for a in _usable_completed_activities(activities) if activity_family(a.activity_type) == "Strength"]
    if not strength:
        return 2
    weekly_strength = len(strength) / _history_span_weeks(strength)
    return min(6, max(2, round(weekly_strength)))


def _movement_pattern(name: str) -> str | None:
    meta = exercise_metadata(name)
    if meta:
        return meta["movement_pattern"]
    upper = name.upper()
    hints = (
        ("knee_dominant", ("SQUAT", "LUNGE", "LEG_PRESS", "LEG_EXTENSION")),
        ("hip_hinge", ("DEADLIFT", "LEG_CURL", "HIP_THRUST", "GLUTE")),
        ("horizontal_push", ("BENCH", "CHEST", "PUSH_UP", "FLY")),
        ("vertical_push", ("SHOULDER", "OVERHEAD", "MILITARY", "LATERAL_RAISE")),
        ("horizontal_pull", ("ROW", "FACE_PULL")),
        ("vertical_pull", ("PULL_UP", "PULLDOWN", "PULL_DOWN", "CHIN_UP")),
        ("elbow_flexion", ("CURL", "BICEP")),
        ("elbow_extension", ("TRICEP", "SKULL")),
        ("calves", ("CALF",)),
        ("core", ("PLANK", "CRUNCH", "AB_", "DEAD_BUG")),
    )
    return next((pattern for pattern, words in hints if any(word in upper for word in words)), None)


def _exercise_pattern(exercises: set[str], activity_name: str | None) -> str | None:
    groups = {
        group for group, hints in _EXERCISE_GROUPS.items()
        if any(hint in exercise for exercise in exercises for hint in hints)
    }
    if not groups:
        return None
    name = (activity_name or "").lower()
    for hint, pattern in _NAME_PATTERNS:
        if hint in name and (pattern in groups or pattern in {"upper", "full_body"}):
            return pattern
    if "push" in groups and "pull" in groups and "lower" in groups:
        return "full_body"
    if "push" in groups and "pull" in groups:
        return "upper"
    if "lower" in groups and len(groups) == 1:
        return "lower"
    if "push" in groups and len(groups) == 1:
        return "push"
    if "pull" in groups and len(groups) == 1:
        return "pull"
    if "pull" in groups and "lower" in groups and len(exercises) >= 3:
        return "pull"  # Romanian deadlifts often belong in a pull workout.
    if "lower" in groups and ("push" in groups or "pull" in groups):
        return "full_body"

    return None


def _detected_split(labels: Counter[str]) -> str | None:
    if all(labels[pattern] >= 2 for pattern in ("push", "pull", "lower")):
        return "push/pull/legs"
    if labels["upper"] >= 2 and labels["lower"] >= 2:
        return "upper/lower"
    if labels["full_body"] >= 3:
        return "full-body"
    if sum(labels.values()) >= 3:
        return "mixed"
    return None


def _program_split(program: dict[str, Any]) -> str:
    focuses = [session["focus_tags"][0].lower() for session in program["sessions"]]
    if focuses and all(focus == "full body" for focus in focuses):
        return "full-body"
    if {"push", "pull", "lower body"}.issubset(focuses):
        return "push/pull/legs"
    if "upper body" in focuses and "lower body" in focuses:
        return "upper/lower"
    return "mixed"


def _plan_match_scores(
    weekly: int,
    experience: str,
    detected_split: str | None,
    observed_categories: set[str],
) -> dict[str, dict[str, Any]]:
    """Score every catalog routine using explainable, non-clinical fit signals."""
    level_rank = {"new": 0, "returning": 0, "six_to_twenty_four_months": 1, "two_plus_years": 2}
    athlete_rank = level_rank.get(experience, 0)
    split_fit = {
        "full-body": {"full-body": 100, "mixed": 80, "upper/lower": 65, "push/pull/legs": 45},
        "upper/lower": {"upper/lower": 100, "mixed": 80, "push/pull/legs": 65, "full-body": 65},
        "push/pull/legs": {"push/pull/legs": 100, "mixed": 75, "upper/lower": 65, "full-body": 45},
        "mixed": {"mixed": 100, "full-body": 90, "upper/lower": 80, "push/pull/legs": 75},
    }
    frequency_fit = {0: 100, 1: 70, 2: 30, 3: 10, 4: 0}
    raw_matches: dict[str, dict[str, Any]] = {}

    for key, program in PROGRAMS.items():
        days = len(program["sessions"])
        day_difference = min(abs(days - weekly), 4)
        frequency_score = frequency_fit[day_difference]
        routine_rank = level_rank.get(program["experience"], 0)
        level_difference = routine_rank - athlete_rank
        if level_difference == 0:
            experience_score = 100
        elif level_difference < 0:
            experience_score = 75 if level_difference == -1 else 55
        else:
            experience_score = 40 if level_difference == 1 else 10

        routine_split = _program_split(program)
        pattern_score = split_fit.get(detected_split or "", {}).get(routine_split, 60)
        program_categories = {
            exercise.get("garmin_category") or exercise["exercise_key"].split(":", 1)[0]
            for program_session in program["sessions"]
            for exercise in program_session["exercises"]
        }
        if observed_categories and program_categories:
            overlap = len(observed_categories & program_categories)
            exercise_score = round(overlap / min(len(observed_categories), len(program_categories)) * 100)
        else:
            exercise_score = 50

        score = round(
            frequency_score * .35
            + pattern_score * .35
            + experience_score * .20
            + exercise_score * .10
        )
        if frequency_score == 0:
            score = round(score * 0.4)
        elif frequency_score <= 30:
            score = round(score * 0.65)

        label = (
            "Strong match"
            if score >= 80
            else "Good match"
            if score >= 65
            else "Possible match"
            if score >= 50
            else "Poor fit"
        )
        if label == "Poor fit":
            score = max(8, min(25, score))

        raw_matches[key] = {
            "score": score,
            "label": label,
            "tone": "strong" if score >= 80 else "good" if score >= 65 else "possible" if score >= 50 else "poor",
            "evidence": "",
            "is_specialty": False,
            "is_best": False,
        }

    ranked_keys = sorted(
        raw_matches,
        key=lambda key: (
            -raw_matches[key]["score"],
            list(PROGRAMS).index(key),
        ),
    )
    for rank, key in enumerate(ranked_keys, start=1):
        raw_matches[key]["rank"] = rank
    
    top_key = ranked_keys[0]
    raw_matches[top_key]["is_best"] = True

    # Visually calibrate bar scale: Best match (98%), Strong (78%), Good (60%), Possible (42%), Poor fit (12%)
    for key, match in raw_matches.items():
        if key == top_key:
            match["score"] = 98
            match["tone"] = "strong"
        elif match["label"] == "Strong match":
            match["score"] = 78
            match["tone"] = "strong"
        elif match["label"] == "Good match":
            match["score"] = 60
            match["tone"] = "good"
        elif match["label"] == "Possible match":
            match["score"] = 42
            match["tone"] = "possible"
        else:
            match["score"] = 12
            match["tone"] = "poor"

    return raw_matches


def _rank_plans_from_history(
    session: Session,
    activities: list[Activity],
    experience_level: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    strength = [a for a in _usable_completed_activities(activities) if activity_family(a.activity_type) == "Strength"]
    weekly = min(6, max(2, round(len(strength) / _history_span_weeks(strength)))) if strength else 2

    grouped: dict[int, set[str]] = defaultdict(set)
    observed_categories: set[str] = set()
    if strength:
        ids = [a.id for a in strength]
        for exercise in session.query(ExerciseSet).filter(ExerciseSet.activity_id.in_(ids)).all():
            name = (exercise.exercise_category or exercise.exercise_name or "").upper()
            if name:
                grouped[exercise.activity_id].add(name)
            if exercise.exercise_category:
                observed_categories.add(exercise.exercise_category.upper())

    labels = Counter()
    for activity in strength:
        movements = {pattern for name in grouped[activity.id] if (pattern := _movement_pattern(name))}
        lower = bool(movements & {"knee_dominant", "hip_hinge"})
        push = bool(movements & {"horizontal_push", "vertical_push", "elbow_extension"})
        pull = bool(movements & {"horizontal_pull", "vertical_pull", "elbow_flexion"})
        label = "full_body" if lower and push and pull else "upper" if push and pull else "lower" if lower and not (push or pull) else "push" if push and not pull else "pull" if pull and not push else None
        if label:
            labels[label] += 1

    usable = sum(labels.values())
    detected_split = _detected_split(labels)
    scoring_weekly = weekly if usable >= 3 else 2
    matches = _plan_match_scores(scoring_weekly, experience_level, detected_split, observed_categories)
    key = next(key for key, match in matches.items() if match["is_best"])
    if len(strength) < 3:
        reason = "No reliable gym pattern found yet; the comparison starts conservatively with a two-day routine."
    elif usable < 3:
        reason = "No reliable split was found from recent exercise sets; the comparison starts conservatively with a two-day routine."
    else:
        experience_label = {
            "new": "new or under 6 months",
            "six_to_twenty_four_months": "6-24 months",
            "two_plus_years": "more than 2 years",
            "returning": "returning after a break",
        }.get(experience_level, "uncertain")
        reason = (
            f"Best fit from about {weekly} recent gym sessions per week, {usable} exercise-backed "
            f"{detected_split} sessions, and an inferred {experience_label} training background."
        )
    return {"key": key, "reason": reason, "days_per_week": len(PROGRAMS[key]["sessions"])}, matches


def recommend_plan_from_history(
    session: Session,
    activities: list[Activity],
    experience_level: str = "new",
) -> dict[str, Any]:
    """Rank the curated routine catalog from frequency, exercises, and experience."""
    recommendation, _ = _rank_plans_from_history(session, activities, experience_level)
    return recommendation


def _build_defaults(
    recent_activities: list[Activity],
    all_activities: list[Activity],
    templates: list[Workout],
    classification: dict[str, Any],
) -> dict[str, Any]:
    training_type = classification["training_type"]
    usable = _usable_completed_activities(recent_activities)
    background = [a for a in _usable_completed_activities(all_activities) if activity_family(a.activity_type) == "Strength"]
    counts = Counter(activity_family(a.activity_type) for a in usable)
    background_avg_per_week = len(background) / _history_span_weeks(background)
    preferred = _preferred_activities(training_type, counts)
    days_per_week = _coach_strength_budget(recent_activities)

    recent_strength = [a for a in usable if activity_family(a.activity_type) == "Strength"]
    experience = _experience_level(len(background), background_avg_per_week, len(templates))
    if background and not recent_strength:
        experience = "returning"
    return {
        "training_type": training_type,
        # Experience is deliberately long-term background, not current routine.
        "experience_level": experience,
        "primary_goal": GOAL_BY_TYPE[training_type],
        "preferred_activities": preferred,
        "equipment_access": ["gym"],
        "program_name": PROGRAM_BY_TYPE[training_type],
        "plan_mode": "schedule_my_routine",
        "selected_templates": [],
        "days_per_week": days_per_week,
        "training_days": _preferred_weekdays(usable, days_per_week),
        "preferred_time_of_day": _preferred_time_of_day(usable),
        "scheduling_options": ["manual_approval", "calendar_aware", "recovery_based"],
    }


def analyze_user_history(session: Session, lookback_days: int = 90) -> dict[str, Any]:
    """Summarize Garmin history as editable onboarding assumptions."""
    since = datetime.combine(date.today() - timedelta(days=lookback_days), datetime.min.time())
    all_activities = (
        session.query(Activity)
        .order_by(Activity.start_time.desc())
        .all()
    )
    recent_activities = (
        session.query(Activity)
        .filter(Activity.start_time >= since)
        .order_by(Activity.start_time.desc())
        .all()
    )
    templates = user_workouts_query(session).order_by(Workout.name.asc()).all()

    template_rows = [
        {
            "workout_id": w.workout_id,
            "name": w.name,
            "sport_type": w.sport_type,
            "activity_family": activity_family(w.sport_type),
        }
        for w in templates
    ]
    # The onboarding screen describes the athlete's current routine, so every
    # visible count and classification uses the same recent window.
    classification = _classify_history(recent_activities)
    defaults = _build_defaults(recent_activities, all_activities, templates, classification)
    recent_completed = _usable_completed_activities(recent_activities)
    all_completed = _usable_completed_activities(all_activities)
    recent_patterns = _activity_patterns(recent_activities)
    plan_recommendation, plan_matches = _rank_plans_from_history(
        session,
        recent_activities,
        defaults["experience_level"],
    )
    return {
        "lookback_days": lookback_days,
        "activity_patterns": recent_patterns,
        "all_activity_patterns": _activity_patterns(all_activities),
        "classification": classification,
        "defaults": defaults,
        "templates": template_rows,
        "has_history": bool(recent_completed),
        "has_templates": bool(template_rows),
        "total_activities": len(recent_completed),
        "recent_routine": {
            "window_days": lookback_days,
            "total_activities": len(recent_completed),
            "classification": classification,
            "activity_patterns": recent_patterns,
        },
        "training_background": {
            "total_activities": len(all_completed),
            "experience_level": defaults["experience_level"],
        },
        "plan_recommendation": plan_recommendation,
        "plan_matches": plan_matches,
    }


def active_program(session: Session) -> TrainingProgram | None:
    return (
        session.query(TrainingProgram)
        .filter(TrainingProgram.active.is_(True))
        .order_by(TrainingProgram.id.desc())
        .first()
    )


def latest_draft_program(session: Session) -> TrainingProgram | None:
    return (
        session.query(TrainingProgram)
        .filter(TrainingProgram.status == "draft")
        .filter(TrainingProgram.active.is_(False))
        .order_by(TrainingProgram.id.desc())
        .first()
    )


def program_sessions_for(session: Session, program_id: int) -> list[ProgramSession]:
    return (
        session.query(ProgramSession)
        .filter(ProgramSession.program_id == program_id)
        .filter(ProgramSession.session_role == "coach_strength")
        .order_by(ProgramSession.sequence_order.asc(), ProgramSession.id.asc())
        .all()
    )
