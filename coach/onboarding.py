"""Onboarding helpers for generic, user-confirmed coaching setup."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from db import Activity, ProgramSession, TrainingProgram, Workout

_COACH_PREFIX = "\U0001f3cb\ufe0f "


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

TRAINING_TYPE_LABELS = {
    "strength_focused": "Strength focused",
    "endurance_focused": "Endurance focused",
    "mixed_fitness": "Mixed fitness",
    "sport_recreational": "Sport recreational",
    "low_history": "Not enough history yet",
}

GOAL_BY_TYPE = {
    "strength_focused": "Build strength",
    "endurance_focused": "Improve endurance",
    "mixed_fitness": "Balanced fitness",
    "sport_recreational": "Support sport performance",
    "low_history": "General fitness",
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


def _activity_patterns(activities: list[Activity]) -> list[dict[str, Any]]:
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
    dates = [a.start_time.date() for a in activities if a.start_time]
    if not dates:
        return 1.0
    return max(((max(dates) - min(dates)).days + 1) / 7, 1.0)


def _experience_level(total_sessions: int, avg_per_week: float, template_count: int) -> str:
    if total_sessions >= 120 or avg_per_week >= 5 or template_count >= 8:
        return "advanced"
    if total_sessions >= 20 or avg_per_week >= 2 or template_count >= 2:
        return "intermediate"
    return "beginner"


def _classify_history(activities: list[Activity]) -> dict[str, Any]:
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


def _build_defaults(
    activities: list[Activity],
    templates: list[Workout],
    classification: dict[str, Any],
) -> dict[str, Any]:
    training_type = classification["training_type"]
    counts = Counter(activity_family(a.activity_type) for a in activities)
    total = len(activities)
    avg_per_week = total / _history_span_weeks(activities)
    preferred = _preferred_activities(training_type, counts)
    selected_templates = _selected_template_ids(templates, preferred, training_type)
    days_per_week = min(6, max(1, round(avg_per_week))) if total else 3
    if training_type == "low_history":
        days_per_week = 3

    return {
        "training_type": training_type,
        "experience_level": _experience_level(total, avg_per_week, len(templates)),
        "primary_goal": GOAL_BY_TYPE[training_type],
        "preferred_activities": preferred,
        "equipment_access": _equipment_defaults(training_type, preferred, templates),
        "program_name": PROGRAM_BY_TYPE[training_type],
        "plan_mode": "existing_templates" if templates else "schedule_my_routine",
        "selected_templates": selected_templates,
        "days_per_week": days_per_week,
        "training_days": _preferred_weekdays(activities, days_per_week),
        "preferred_time_of_day": _preferred_time_of_day(activities),
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
    templates = (
        session.query(Workout)
        .filter(~Workout.name.startswith(_COACH_PREFIX))
        .order_by(Workout.name.asc())
        .all()
    )

    template_rows = [
        {
            "workout_id": w.workout_id,
            "name": w.name,
            "sport_type": w.sport_type,
            "activity_family": activity_family(w.sport_type),
        }
        for w in templates
    ]
    classification = _classify_history(all_activities)
    defaults = _build_defaults(all_activities, templates, classification)

    return {
        "lookback_days": lookback_days,
        "activity_patterns": _activity_patterns(recent_activities),
        "all_activity_patterns": _activity_patterns(all_activities),
        "classification": classification,
        "defaults": defaults,
        "templates": template_rows,
        "has_history": bool(all_activities),
        "has_templates": bool(template_rows),
        "total_activities": len(all_activities),
    }


def active_program(session: Session) -> TrainingProgram | None:
    return (
        session.query(TrainingProgram)
        .filter(TrainingProgram.active.is_(True))
        .order_by(TrainingProgram.id.desc())
        .first()
    )


def program_sessions_for(session: Session, program_id: int) -> list[ProgramSession]:
    return (
        session.query(ProgramSession)
        .filter(ProgramSession.program_id == program_id)
        .order_by(ProgramSession.sequence_order.asc(), ProgramSession.id.asc())
        .all()
    )
