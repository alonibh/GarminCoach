"""Deterministic Saturday summary; no LLM and no ACWR."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from coach.onboarding import active_program
from coach.program_state import program_state_facts
from db import (
    Activity,
    ActivityProgramMatch,
    AthleteSafetyReport,
    ExerciseSet,
    PlannedSession,
    Sleep,
    get_session,
)
from notify.outbox import enqueue_notification, process_due_notifications
from time_utils import get_local_date, get_local_now


def _avg(values) -> float | None:
    values = [float(value) for value in values if value is not None]
    return round(sum(values) / len(values), 1) if values else None


def _strength_progression(session: Session, week_start: date, week_end: date) -> str | None:
    previous_start = week_start - timedelta(days=7)
    rows = (
        session.query(ExerciseSet, Activity.start_time)
        .join(Activity, ExerciseSet.activity_id == Activity.id)
        .filter(
            Activity.start_time >= datetime.combine(previous_start, time.min),
            Activity.start_time <= datetime.combine(week_end, time.max),
            ExerciseSet.weight_kg.isnot(None),
            ExerciseSet.weight_kg > 0,
            ExerciseSet.reps.isnot(None),
            ExerciseSet.set_type != "REST",
        )
        .all()
    )
    current, previous = {}, {}
    for exercise, started in rows:
        name = exercise.exercise_name or exercise.exercise_category or "Exercise"
        key = (name, int(exercise.reps))
        target = current if started.date() >= week_start else previous
        target[key] = max(target.get(key, 0), float(exercise.weight_kg))
    gains = [
        (weight - previous[key], key[0], key[1])
        for key, weight in current.items()
        if key in previous and weight > previous[key]
    ]
    if not gains:
        return None
    delta, name, reps = max(gains)
    return f"{name.replace('_', ' ').title()} +{delta:g} kg at {reps} reps"


def build_weekly_summary(session: Session, week_end: date) -> str:
    week_start = week_end - timedelta(days=6)
    start_dt = datetime.combine(week_start, time.min)
    end_dt = datetime.combine(week_end, time.max)
    program = active_program(session)
    completed = (
        session.query(ActivityProgramMatch)
        .join(Activity, ActivityProgramMatch.activity_id == Activity.id)
        .filter(Activity.start_time >= start_dt, Activity.start_time <= end_dt)
        .count()
    )
    expected = program.days_per_week if program and program.days_per_week is not None else None
    strength = (
        session.query(Activity)
        .filter(
            Activity.start_time >= start_dt,
            Activity.start_time <= end_dt,
            (Activity.activity_type.ilike("%strength%")) | (Activity.activity_type.ilike("%weight%")),
        )
        .all()
    )
    matched_ids = {
        item[0]
        for item in session.query(ActivityProgramMatch.activity_id)
        .join(Activity, ActivityProgramMatch.activity_id == Activity.id)
        .filter(Activity.start_time >= start_dt, Activity.start_time <= end_dt)
        .all()
    }
    unmatched = sum(1 for activity in strength if activity.id not in matched_ids)
    missed = session.query(PlannedSession).filter(
        PlannedSession.target_date >= week_start,
        PlannedSession.target_date <= week_end,
        PlannedSession.status.notin_(("completed", "cancelled")),
    ).count()
    progression = _strength_progression(session, week_start, week_end)

    current_sleep = session.query(Sleep).filter(Sleep.day >= week_start, Sleep.day <= week_end).all()
    previous_sleep = session.query(Sleep).filter(
        Sleep.day >= week_start - timedelta(days=7), Sleep.day < week_start
    ).all()
    current_hours = _avg([(row.total_s / 3600) if row.total_s else None for row in current_sleep])
    current_score = _avg([row.score for row in current_sleep])
    previous_hours = _avg([(row.total_s / 3600) if row.total_s else None for row in previous_sleep])
    previous_score = _avg([row.score for row in previous_sleep])
    issues = session.query(AthleteSafetyReport).filter_by(active=True).count()
    state = program_state_facts(session, program, on_date=week_end) if program else None

    if not strength and not current_sleep and not completed:
        next_text = f" Next: {state['next_session_name']}." if state else ""
        return f"Weekly summary: no new synced training or recovery data.{next_text}"

    expected_text = str(expected) if expected is not None else "not defined"
    lines = [
        f"Program sessions: {completed}/{expected_text} completed. "
        f"Unmatched strength activities: {unmatched}. Uncompleted scheduled sessions: {missed}."
    ]
    if progression:
        lines.append(f"Synced progression: {progression}.")
    if current_hours is not None or current_score is not None:
        current = []
        previous = []
        if current_hours is not None:
            current.append(f"{current_hours:g}h average")
        if current_score is not None:
            current.append(f"score {current_score:g}")
        if previous_hours is not None:
            previous.append(f"{previous_hours:g}h")
        if previous_score is not None:
            previous.append(f"score {previous_score:g}")
        comparison = f"; previous week {', '.join(previous)}" if previous else ""
        lines.append(f"Sleep: {', '.join(current)}{comparison}.")
    if issues:
        lines.append(f"Confirmed unresolved safety reports: {issues}.")
    if state:
        lines.append(
            f"Next: {state['next_session_name']}; earliest {state['earliest_recommended_date']}."
        )
    return "\n".join(lines)


def send_weekly_summary() -> None:
    today = get_local_date()
    now = get_local_now().replace(tzinfo=None)
    with get_session() as session:
        enqueue_notification(
            session,
            event_type="weekly_summary",
            due_at=now,
            payload={"week_end": today.isoformat()},
            idempotency_key=f"weekly:{today.isoformat()}",
        )
    process_due_notifications(now)


if __name__ == "__main__":
    send_weekly_summary()
