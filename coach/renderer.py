"""Cold, concise rendering of an already-decided coaching result."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from coach.decision_engine import DecisionResult, sleep_score_category
from db import Sleep


def _clock(value) -> str | None:
    return value.strftime("%H:%M") if value else None


def _duration(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h {remainder}m" if hours else f"{remainder}m"


def _facts_by_signal(result: DecisionResult) -> dict[str, object]:
    return {item["signal"]: item["value"] for item in result.observations}


def recovery_fact_lines(
    session: Session, result: DecisionResult, *, include_private_facts: bool = False,
) -> list[str]:
    """Present the canonical persisted recovery facts without adding authority."""
    values = _facts_by_signal(result)
    lines: list[str] = []
    duration = values.get("sleep")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        sleep_day = date.fromisoformat(result.decision_date) if result.decision_date else None
        sleep_row = session.get(Sleep, sleep_day) if sleep_day else None
        start = _clock(sleep_row.sleep_start_time) if sleep_row else None
        end = _clock(sleep_row.sleep_end_time) if sleep_row else None
        timing = f" {start}-{end}" if start and end else ""
        lines.append(f"Sleep{timing}: {duration:g}h")
    score = values.get("sleep_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        category = sleep_score_category(float(score))
        suffix = f" ({category})" if category else ""
        lines.append(f"Garmin Sleep Score: {int(round(score))}{suffix}")
    hrv_status = values.get("hrv_status")
    if isinstance(hrv_status, str) and hrv_status:
        lines.append(f"Garmin HRV Status: {hrv_status}")
    recovery_time = values.get("recovery_time")
    if isinstance(recovery_time, int) and not isinstance(recovery_time, bool):
        lines.append(f"Recovery Time: {_duration(recovery_time)}")
    if include_private_facts:
        hrv = values.get("hrv")
        if isinstance(hrv, (int, float)) and not isinstance(hrv, bool):
            lines.append(f"Overnight HRV: {hrv:g} ms")
        resting_hr = values.get("resting_hr")
        if isinstance(resting_hr, (int, float)) and not isinstance(resting_hr, bool):
            lines.append(f"Resting HR: {resting_hr:g} bpm")
        stress = values.get("stress")
        if isinstance(stress, (int, float)) and not isinstance(stress, bool):
            lines.append(f"Stress: {stress:g}")
    return lines


def readiness_authority_explanation(result: DecisionResult) -> str:
    reason = result.reason_codes[0] if result.reason_codes else ""
    explanations = {
        "TRAINING_READINESS_UNSUPPORTED_NO_SUBSTITUTE": "this device does not support it",
        "TRAINING_READINESS_SUPPORT_UNVERIFIED": "device support has not yet been verified",
        "TRAINING_READINESS_EXPECTED_PENDING": "today's reading is still pending",
        "TRAINING_READINESS_MISSING": "no current reading is available",
        "TRAINING_READINESS_STALE": "the available reading is stale",
        "TRAINING_READINESS_ERROR": "Garmin returned an error while obtaining it",
        "TRAINING_READINESS_INVALID": "the available reading is invalid",
    }
    return explanations.get(reason, "no valid current reading is available")


def authoritative_readiness_line(result: DecisionResult) -> str | None:
    """Present only the evaluator-granted Training Readiness authority."""
    if result.decision_type not in {
        "KEEP_SELECTED_WORKOUT",
        "KEEP_SELECTED_WORKOUT_WITH_WARNING",
        "REST_RECOMMENDED",
    }:
        return None
    if (
        type(result.readiness_score) is not int
        or not 1 <= result.readiness_score <= 100
        or result.readiness_category not in {"Poor", "Low", "Moderate", "High", "Prime"}
    ):
        return None
    return f"Garmin Training Readiness: {result.readiness_score} ({result.readiness_category})"


def render_morning(
    session: Session, result: DecisionResult, *, plan_only: bool = False,
) -> tuple[str | None, dict | None, list[str]]:
    # Recovery is advisory in this phase: rendering must not stage interactions.
    context = [] if plan_only else recovery_fact_lines(session, result)
    if context:
        context.append(
            "Sleep, HRV Status, and Recovery Time are informational only; only fresh Garmin Training Readiness guides this decision."
        )
    readiness = None if plan_only else authoritative_readiness_line(result)
    if result.decision_type == "NO_SELECTED_WORKOUT":
        body = "No workout is selected for today. Recovery data is informational until a workout is selected."
    elif result.decision_type == "WORKOUT_SELECTION_REQUIRED":
        candidates = next((item["value"] for item in result.observations if item["signal"] == "selected_workout_candidates"), [])
        body = "Choose a specific workout before recovery can be evaluated: " + ", ".join(
            f"{item['name']} ({item['scheduled_time'] or 'time unset'})" for item in candidates
        ) + "."
    elif result.decision_type == "PROGRAM_REST_RECOMMENDED":
        body = f"Program rest is recommended; {result.planned_session_name} remains selected and pending."
    elif result.decision_type == "PROGRAM_REST_DAY":
        earliest = result.earliest_eligible_date or "tomorrow"
        body = f"Program rest day. {result.next_program_session_name or 'Next session'} is next; earliest {earliest}."
        if result.optional_recovery_activity and not plan_only:
            dur = result.optional_recovery_activity.get("duration_min")
            if isinstance(dur, (list, tuple)) and len(dur) == 2:
                low, high = dur
            else:
                low, high = 20, 30
            body += f" Optional: {low}-{high} min easy walking at conversational effort."
    elif result.decision_type == "PROPOSE_NEXT_SESSION":
        name = result.next_program_session_name or "Workout"
        at = f" at {result.planned_start_time}" if result.planned_start_time else ""
        body = f"Suggested today: {name}{at}."
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
            body = (
                f"{name}{at} remains selected. Garmin Training Readiness cannot guide a workout recommendation today: "
                f"{readiness_authority_explanation(result)}."
            )

    if not plan_only and any(action.get("type") == "choose_recovery_outcome" for action in result.permitted_actions):
        if result.decision_type == "NO_BIOMETRIC_AUTHORITY":
            body += " GarminCoach is not using the informational recovery facts to choose a replacement."
        body += " Choose what to do today: keep the selected workout, use the 30-minute walk, or rest."
    return "\n".join([*context, *([readiness] if readiness else []), body]), None, []
