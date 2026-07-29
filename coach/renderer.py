"""Cold, concise rendering of an already-decided coaching result."""
from __future__ import annotations

from datetime import date
import json

from sqlalchemy.orm import Session

from coach.decision_engine import DecisionResult
from db import Sleep
from time_utils import format_chat_date


def _clock(value) -> str | None:
    return value.strftime("%H:%M") if value else None


def _metric_line(session: Session, result: DecisionResult) -> str:
    parts = []
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
    if result.readiness_score is not None:
        parts.append(f"Garmin readiness {result.readiness_score} ({result.readiness_category})")
    return "; ".join(parts) + ("." if parts else "")


def render_morning(
    session: Session, result: DecisionResult, *, plan_only: bool = False,
) -> tuple[str | None, dict | None, list[str]]:
    # Recovery is advisory in this phase: rendering must not stage interactions.
    recommends_workout = result.workout_outcome in {"KEEP_SELECTED_WORKOUT", "KEEP_SELECTED_WORKOUT_WITH_WARNING"}
    metrics = "" if plan_only else _metric_line(session, result)
    if result.decision_type == "NO_SELECTED_WORKOUT":
        body = "No workout is selected for today. Recovery data is informational until a workout is selected."
    elif result.decision_type == "WORKOUT_SELECTION_REQUIRED":
        candidates = next((item["value"] for item in result.observations if item["signal"] == "selected_workout_candidates"), [])
        body = "Choose a specific workout before recovery can be evaluated: " + ", ".join(
            f"{item['name']} ({item['scheduled_time'] or 'time unset'})" for item in candidates
        ) + "."
    elif result.decision_type == "PROGRAM_REST_RECOMMENDED":
        body = f"Program rest is recommended; {result.planned_session_name} remains selected and pending."
    else:
        name = result.planned_session_name or "Workout"
        at = f" at {result.planned_start_time}" if result.planned_start_time else ""
        if result.decision_type == "REST_RECOMMENDED":
            body = f"Rest is recommended instead of {name}{at}. Garmin Training Readiness is Poor; the selected workout remains pending."
        elif result.decision_type == "KEEP_SELECTED_WORKOUT_WITH_WARNING":
            body = f"Keep {name}{at}. Garmin Training Readiness is Low; this is a warning only."
        elif result.decision_type == "KEEP_SELECTED_WORKOUT":
            body = f"Planned: {name}{at}."
        else:
            reason = result.reason_codes[0].replace("_", " ").lower() if result.reason_codes else "unavailable"
            body = f"{name}{at} remains selected. Garmin Training Readiness has no workout authority today ({reason})."

    text = "\n".join(part for part in (metrics, body) if part)
    return text, None, []
