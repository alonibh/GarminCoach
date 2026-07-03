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


def activity_family(activity_type: str | None) -> str:
    raw = (activity_type or "").lower()
    for hint, label in COMMON_ACTIVITY_LABELS.items():
        if hint in raw:
            return label
    return (activity_type or "Other").replace("_", " ").title()


def analyze_user_history(session: Session, lookback_days: int = 90) -> dict[str, Any]:
    """Summarize recent Garmin history as editable onboarding assumptions."""
    since = datetime.combine(date.today() - timedelta(days=lookback_days), datetime.min.time())
    activities = (
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

    template_rows = [
        {
            "workout_id": w.workout_id,
            "name": w.name,
            "sport_type": w.sport_type,
        }
        for w in templates
    ]

    return {
        "lookback_days": lookback_days,
        "activity_patterns": activity_patterns,
        "templates": template_rows,
        "has_history": bool(activity_patterns),
        "has_templates": bool(template_rows),
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
