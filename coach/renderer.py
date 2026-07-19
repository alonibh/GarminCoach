"""Cold, concise rendering of an already-decided coaching result."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from coach.decision_engine import DecisionResult
from coach.interactions import reply_markup, stage_decision_actions
from db import Sleep


def _clock(value) -> str | None:
    return value.strftime("%H:%M") if value else None


def _metric_line(session: Session, result: DecisionResult) -> str:
    parts = []
    if result.readiness_score is not None:
        parts.append(f"Garmin readiness {result.readiness_score} ({result.readiness_category})")
    values = {item["signal"]: item["value"] for item in result.observations}
    duration = values.get("sleep_duration_hours")
    score = values.get("sleep_score")
    if duration is not None:
        sleep_day = date.fromisoformat(result.decision_date) if result.decision_date else None
        sleep_row = session.get(Sleep, sleep_day) if sleep_day else None
        start = _clock(sleep_row.sleep_start_time) if sleep_row else None
        end = _clock(sleep_row.sleep_end_time) if sleep_row else None
        sleep = f"sleep {start}-{end}, {duration:g}h" if start and end else f"sleep {duration:g}h"
        if isinstance(score, dict):
            sleep += f", score {score['score']} ({score['category']})"
        parts.append(sleep)
    return "; ".join(parts) + ("." if parts else "")


def render_morning(session: Session, result: DecisionResult) -> tuple[str | None, dict | None, list[str]]:
    if result.decision_type in {"WAITING_FOR_DATA", "SYNC_REQUIRED"}:
        return None, None, []

    metrics = _metric_line(session, result)
    if result.workout_outcome == "PROGRAM_REST_DAY":
        body = (
            f"Program rest day. {result.next_program_session_name} is next; "
            f"earliest {result.earliest_eligible_date}."
        )
        recovery = result.optional_recovery_activity
        if recovery:
            low, high = recovery["duration_min"]
            body += f" Optional: {low}-{high} min easy walking at conversational effort."
    else:
        name = result.planned_session_name or result.next_program_session_name or "Workout"
        at = f" at {result.planned_start_time}" if result.planned_start_time else ""
        if result.decision_type == "ADVISE_SKIP_SESSION":
            body = f"Skip {name}{at}. Readiness is Poor. The original session is unchanged."
        elif result.decision_type == "WARN_ORIGINAL_SESSION":
            body = f"{name}{at} remains unchanged. Low readiness warrants caution, not a modified workout."
        elif result.workout_outcome == "KEEP_PLANNED_SESSION":
            body = f"Planned: {name}{at}."
        elif result.workout_outcome == "PROPOSE_NEXT_SESSION":
            body = f"Suggested today: {name}."
        else:
            body = "No workout action is available today."

    if result.calendar_conflict:
        conflict = result.calendar_conflict
        body += f" Calendar conflict: {conflict.get('title', 'another event')} at {conflict.get('start', '')[-5:]}."
    elif "CALENDAR_ACCESS_ERROR" in result.reason_codes:
        body += " Calendar could not be checked."

    if result.best_effort:
        omitted = ", ".join(
            item["signal"].replace("_", " ")
            for item in result.missing_observations if item["critical"]
        )
        body += f" Best effort; missing {omitted}."
    noncritical = [
        item["signal"].replace("_", " ")
        for item in result.missing_observations if not item["critical"]
    ]
    if noncritical:
        body += f" Missing non-critical data: {', '.join(noncritical)}."
    text = "\n".join(part for part in (metrics, body) if part)
    interactions = stage_decision_actions(session, result)
    return text, reply_markup(interactions), [item.interaction_id for item in interactions]
