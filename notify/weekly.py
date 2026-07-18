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
    ProgramSession,
    Sleep,
    get_session,
)
from notify.outbox import enqueue_notification, process_due_notifications
from time_utils import get_local_date, get_local_now


def _avg(values) -> float | None:
    values = [float(value) for value in values if value is not None]
    return round(sum(values) / len(values), 1) if values else None


def _duration_text(hours: float) -> str:
    total_minutes = int(round(hours * 60))
    whole_hours, minutes = divmod(total_minutes, 60)
    return f"{whole_hours}h {minutes:02d}m"


def _sleep_change(current: float, previous: float) -> str:
    delta_minutes = int(round((current - previous) * 60))
    if delta_minutes == 0:
        return "unchanged from last week"
    direction = "more" if delta_minutes > 0 else "less"
    return f"{abs(delta_minutes)} min {direction} than last week"


def _score_change(current: float, previous: float) -> str:
    delta = int(round(current - previous))
    if delta == 0:
        return "unchanged from last week"
    direction = "up" if delta > 0 else "down"
    return f"{direction} {abs(delta)}"


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
    if program and expected is None:
        expected = session.query(ProgramSession).filter(
            ProgramSession.program_id == program.id,
            ProgramSession.session_role == "coach_strength",
            ProgramSession.is_addon.is_(False),
        ).count() or None
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
        return f"*Weekly summary*\nNo new synced training or sleep data.{next_text}"

    if expected is not None and completed <= expected:
        training_text = f"Training: {completed} of {expected} program sessions completed."
    elif expected is not None:
        training_text = f"Training: {completed} program sessions completed; weekly target {expected}."
    elif completed:
        training_text = f"Training: {completed} program sessions completed."
    else:
        training_text = "Training: no program sessions completed."
    lines = ["*Weekly summary*", training_text]
    if unmatched:
        noun = "activity" if unmatched == 1 else "activities"
        lines.append(f"Additional training: {unmatched} other strength {noun} synced.")
    if missed:
        noun = "session remains" if missed == 1 else "sessions remain"
        lines.append(f"Schedule: {missed} planned {noun} incomplete.")
    if progression:
        lines.append(f"Progress: {progression}.")
    if current_hours is not None or current_score is not None:
        sleep_parts = []
        if current_hours is not None:
            duration = f"{_duration_text(current_hours)} per night"
            if previous_hours is not None:
                duration += f", {_sleep_change(current_hours, previous_hours)}"
            sleep_parts.append(duration)
        if current_score is not None:
            from coach.decision_engine import sleep_score_category

            rounded_score = int(round(current_score))
            category = sleep_score_category(current_score)
            score = f"score {rounded_score}"
            if category:
                score += f" ({category})"
            if previous_score is not None:
                score += f", {_score_change(current_score, previous_score)}"
            sleep_parts.append(score)
        lines.append(f"Sleep: {'; '.join(sleep_parts)}.")
    if issues:
        lines.append(f"Confirmed unresolved safety reports: {issues}.")
    if state:
        lines.append(f"Next: {state['next_session_name']}.")
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
