"""Deterministic, read-only Telegram text renderers."""
from __future__ import annotations

from datetime import datetime, timedelta
import json

from sqlalchemy.orm import Session

from coach.onboarding import active_program
from db import (
    Activity,
    DailyHealth,
    DailyMetrics,
    DecisionRecord,
    PlannedSession,
    ProgramCursor,
    ProgramSession,
    Sleep,
    SyncState,
)
from time_utils import get_local_now


def _json(value: str | None, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def render_recommendation(session: Session) -> str:
    record = (
        session.query(DecisionRecord)
        .order_by(DecisionRecord.evaluated_at.desc())
        .first()
    )
    if record is None:
        return "No official recommendation is available yet."
    result = _json(record.result_json, {})
    outcome = str(
        result.get("workout_outcome")
        or result.get("decision_type")
        or record.decision_type
    ).replace("_", " ").title()
    readiness = result.get("readiness_score")
    session_name = (
        result.get("planned_session_name")
        or result.get("next_program_session_name")
    )
    lines = [f"Official recommendation: {outcome}."]
    if readiness is not None:
        lines.append(f"Training readiness: {readiness}.")
    if session_name:
        lines.append(f"Session: {session_name}.")
    reasons = result.get("reason_codes") or []
    if reasons:
        lines.append(
            "Reasons: "
            + ", ".join(str(reason).replace("_", " ").lower() for reason in reasons)
            + "."
        )
    return "\n".join(lines)


def render_next_workout(session: Session) -> str:
    planned = (
        session.query(PlannedSession)
        .filter(
            PlannedSession.target_date >= get_local_now().date(),
            PlannedSession.status.notin_(("completed", "cancelled")),
        )
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time)
        .first()
    )
    if planned:
        when = planned.target_date.strftime("%A %d %B")
        if planned.suggested_time:
            when += f" at {planned.suggested_time}"
        return f"Next workout: {planned.title}, {when}."
    program = active_program(session)
    cursor = session.get(ProgramCursor, program.id) if program else None
    next_session = (
        session.get(ProgramSession, cursor.next_program_session_id)
        if cursor and cursor.next_program_session_id
        else None
    )
    if next_session:
        return f"Next program session: {next_session.name}. It is not scheduled yet."
    return "No upcoming workout is available."


def render_metrics(session: Session) -> str:
    sleep = session.query(Sleep).order_by(Sleep.day.desc()).first()
    health = session.query(DailyHealth).order_by(DailyHealth.day.desc()).first()
    metrics = session.query(DailyMetrics).order_by(DailyMetrics.day.desc()).first()
    lines = ["Latest recovery metrics:"]
    lines.append(
        f"Sleep: {sleep.total_s / 3600:.1f} h."
        if sleep and sleep.total_s is not None
        else "Sleep: unavailable."
    )
    lines.append(
        f"Sleep score: {sleep.score:g}."
        if sleep and sleep.score is not None
        else "Sleep score: unavailable."
    )
    lines.append(
        f"HRV: {health.hrv_overnight:g} ms."
        if health and health.hrv_overnight is not None
        else "HRV: unavailable."
    )
    readiness = (
        health.training_readiness
        if health and health.training_readiness is not None
        else metrics.readiness if metrics else None
    )
    lines.append(
        f"Training readiness: {readiness:g}."
        if readiness is not None
        else "Training readiness: unavailable."
    )
    return "\n".join(lines)


def render_activities(session: Session) -> str:
    cutoff = get_local_now().replace(tzinfo=None) - timedelta(days=14)
    rows = (
        session.query(Activity)
        .filter(Activity.start_time >= cutoff)
        .order_by(Activity.start_time.desc())
        .limit(10)
        .all()
    )
    if not rows:
        return "No activities were recorded in the last 14 days."
    lines = ["Recent activities:"]
    for row in rows:
        duration = (
            f", {row.duration_s / 60:.0f} min"
            if row.duration_s is not None
            else ""
        )
        day = row.start_time.strftime("%a %d %b") if row.start_time else "Unknown date"
        lines.append(f"• {day}: {row.name or row.activity_type or 'Activity'}{duration}")
    return "\n".join(lines)


def render_program(session: Session) -> str:
    program = active_program(session)
    if program is None:
        return "No active training program is available."
    sessions = (
        session.query(ProgramSession)
        .filter(ProgramSession.program_id == program.id)
        .order_by(ProgramSession.sequence_order, ProgramSession.id)
        .all()
    )
    lines = [f"Active program: {program.name}."]
    lines.extend(
        f"• {item.sequence_order}. {item.name}"
        + (f" ({item.duration_min} min)" if item.duration_min else "")
        for item in sessions
    )
    return "\n".join(lines)


def render_sync_status(session: Session) -> str:
    row = session.get(SyncState, "last_sync_at")
    if not row or not row.value:
        return "Garmin data has not been synchronized yet."
    return f"Last Garmin sync: {row.value}."


def upcoming_planned_sessions(session: Session, days: int = 7) -> list[dict]:
    today = get_local_now().date()
    rows = (
        session.query(PlannedSession)
        .filter(
            PlannedSession.target_date >= today,
            PlannedSession.target_date < today + timedelta(days=days),
            PlannedSession.status.in_(("approved", "scheduled", "planned")),
        )
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time, PlannedSession.id)
        .all()
    )
    return [
        {
            "title": row.title or "Workout",
            "date": row.target_date.isoformat(),
            "start_time": row.suggested_time or "",
            "duration_min": row.duration_min,
        }
        for row in rows
    ]


def render_calendar(
    private_calendar: dict | None, workouts: list[dict], *, days: int = 7
) -> str:
    """Render private and GarminCoach events without reading legacy SyncState."""
    state = (private_calendar or {}).get("state", "unconfigured")
    personal = (private_calendar or {}).get("events", [])
    today = get_local_now().date()
    entries: list[tuple[str, str, str]] = []
    for event in personal if isinstance(personal, list) else []:
        if not isinstance(event, dict):
            continue
        start = str(event.get("start") or "")
        try:
            event_day = datetime.fromisoformat(start[:10]).date()
        except ValueError:
            continue
        if not today <= event_day < today + timedelta(days=days):
            continue
        when = event_day.strftime("%a %d %b")
        if event.get("all_day"):
            when += " (all day)"
        elif len(start) >= 16:
            when += f" {start[11:16]}"
        entries.append(
            (start, "Personal calendar", f"{when}: {event.get('title') or 'Event'}")
        )
    for workout in workouts:
        start = f"{workout.get('date', '')} {workout.get('start_time', '')}".strip()
        try:
            workout_day = datetime.fromisoformat(str(workout.get("date"))).date()
        except (TypeError, ValueError):
            continue
        when = workout_day.strftime("%a %d %b")
        if workout.get("start_time"):
            when += f" {workout['start_time']}"
        entries.append(
            (
                start or workout_day.isoformat(),
                "GarminCoach workouts",
                f"{when}: {workout.get('title') or 'Workout'}",
            )
        )
    entries.sort(key=lambda item: item[0])

    lines = [f"Next {days} days:"]
    if state == "unconfigured":
        lines.append("Personal calendar: No private calendar connected.")
    elif state == "error":
        lines.append("Personal calendar: Calendar temporarily unavailable.")
    if not entries:
        lines.append("No events in the next 7 days.")
        return "\n".join(lines)
    current_source = None
    for _, source, text in entries:
        if source != current_source:
            lines.append(source + ":")
            current_source = source
        lines.append(f"â€¢ {text}")
    return "\n".join(lines)
    lines = ["Next 7 days:"]
    for event in upcoming[:10]:
        lines.append(
            f"• {event.get('date')} {event.get('start_time', '')} "
            f"{event.get('title', 'Workout')}".strip()
        )
    return "\n".join(lines)
