"""AI-first intent recognition with deterministic, confirmation-only actions.

The model may label a request and quote spans from it. It never supplies IDs,
normalized dates, decisions, or executable payloads; those are resolved here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import logging
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

import config
from coach import llm
from db import ChatDialogueState, ChatIntentAudit, PendingInteraction, PlannedSession
from time_utils import get_local_now

logger = logging.getLogger(__name__)

IntentName = Literal[
    "recommend_workout", "get_workout_details", "find_workout_time",
    "schedule_workout", "reschedule_workout", "cancel_workout", "skip_workout",
    "report_safety_issue", "request_sync", "get_sync_status", "get_program",
    "get_calendar", "get_metrics", "get_activity_history", "explain_decision",
    "help", "general_question", "multiple_intents", "unknown",
]


class IntentClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: IntentName
    date_text: str | None = Field(default=None, max_length=64)
    time_text: str | None = Field(default=None, max_length=32)
    workout_text: str | None = Field(default=None, max_length=128)
    topic: str | None = Field(default=None, max_length=128)
    missing_slots: list[Literal["date", "time", "workout"]] = Field(default_factory=list, max_length=3)


@dataclass
class RoutedTurn:
    text: str
    interactions: list[PendingInteraction]


_CLASSIFIER_PROMPT = """You classify one English message for a single-user fitness coach.
Return only one JSON object matching this exact shape:
{"intent":"unknown","date_text":null,"time_text":null,"workout_text":null,"topic":null,"missing_slots":[]}

Allowed intent values:
recommend_workout, get_workout_details, find_workout_time, schedule_workout,
reschedule_workout, cancel_workout, skip_workout, report_safety_issue,
request_sync, get_sync_status, get_program, get_calendar, get_metrics,
get_activity_history, explain_decision, help, general_question,
multiple_intents, unknown.

date_text, time_text, and workout_text must be exact verbatim substrings of the
CURRENT_MESSAGE or null. Never normalize dates, invent identifiers, choose a
workout, or output an action. Use multiple_intents if the user requests more
than one state change. Previous state is context only, never evidence.
"""


def _model_name() -> str:
    return {
        "claude": config.CLAUDE_MODEL,
        "gemini": config.GEMINI_MODEL,
        "ollama": config.OLLAMA_MODEL,
    }.get(config.LLM_PROVIDER, "")


def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline >= 0 else text
        if text.endswith("```"):
            text = text[:-3].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("classifier returned no JSON object")
    if text[:start].strip() or text[end + 1:].strip():
        raise ValueError("classifier returned text outside the JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("classifier JSON must be an object")
    return value


def _validate_evidence(result: IntentClassification, user_text: str) -> None:
    lowered = user_text.casefold()
    for field in ("date_text", "time_text", "workout_text"):
        value = getattr(result, field)
        if value is not None and value.casefold() not in lowered:
            raise ValueError(f"{field} is not verbatim evidence from the current message")


def classify_intent(user_text: str, dialogue: ChatDialogueState | None = None) -> IntentClassification:
    state = None
    if dialogue:
        state = {
            "intent": dialogue.intent,
            "verified_slots": json.loads(dialogue.slots_json or "{}"),
            "missing_slot": dialogue.missing_slot,
        }
    prompt = (
        f"PREVIOUS_TYPED_STATE: {json.dumps(state, sort_keys=True)}\n"
        f"CURRENT_MESSAGE: {json.dumps(user_text)}"
    )
    raw = llm.generate_structured(
        _CLASSIFIER_PROMPT, prompt, IntentClassification.model_json_schema()
    )
    result = IntentClassification.model_validate(_extract_json(raw))
    _validate_evidence(result, user_text)
    return result


def _audit(
    session: Session, user_text: str, *, result: IntentClassification | None,
    status: str, failure: str, latency_ms: int,
) -> None:
    evidence = result.model_dump() if result else {}
    session.add(ChatIntentAudit(
        message_text=user_text,
        provider=config.LLM_PROVIDER,
        model=_model_name(),
        router_mode=config.CHAT_ROUTER_MODE,
        intent=result.intent if result else "unknown",
        evidence_json=json.dumps(evidence, sort_keys=True),
        validation_status=status,
        failure_reason=failure[:1000],
        latency_ms=latency_ms,
        created_at=get_local_now().replace(tzinfo=None),
    ))


def _dialogue(session: Session, now: datetime) -> ChatDialogueState | None:
    row = session.get(ChatDialogueState, 1)
    if row and row.expires_at < now:
        session.delete(row)
        session.flush()
        return None
    return row


def _save_dialogue(session: Session, intent: str, slots: dict, missing: str, now: datetime) -> None:
    session.merge(ChatDialogueState(
        state_id=1, intent=intent, slots_json=json.dumps(slots, sort_keys=True),
        missing_slot=missing, created_at=now, updated_at=now,
        expires_at=now + timedelta(minutes=config.CHAT_DIALOGUE_TTL_MINUTES),
    ))


def _clear_dialogue(session: Session) -> None:
    row = session.get(ChatDialogueState, 1)
    if row:
        session.delete(row)


def _stage_simple_action(
    session: Session, *, action_type: str, target_type: str, target_id: int | None,
    payload: dict, now: datetime,
) -> PendingInteraction:
    from coach.interactions import calendar_version, program_version, sync_version
    row = PendingInteraction(
        interaction_id=str(uuid4()), decision_id=None, action_type=action_type,
        target_type=target_type, target_id=target_id,
        payload_json=json.dumps(payload, sort_keys=True),
        program_version=program_version(session), sync_version=sync_version(session),
        calendar_version=calendar_version(session), created_at=now,
        expires_at=now + timedelta(hours=1), status="pending",
    )
    session.add(row)
    session.flush()
    return row


def _planned_target(session: Session, now: datetime) -> PlannedSession | None:
    return (
        session.query(PlannedSession)
        .filter(PlannedSession.target_date >= now.date())
        .filter(PlannedSession.status == "approved")
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time, PlannedSession.id)
        .first()
    )


def _route_guarded(
    session: Session, user_text: str, result: IntentClassification,
    dialogue: ChatDialogueState | None, now: datetime,
) -> RoutedTurn | None:
    prior_slots = json.loads(dialogue.slots_json or "{}") if dialogue else {}
    slots = dict(prior_slots if dialogue and dialogue.intent == result.intent else {})
    for key in ("date_text", "time_text", "workout_text"):
        value = getattr(result, key)
        if value:
            slots[key] = value

    if result.intent == "multiple_intents":
        _clear_dialogue(session)
        return RoutedTurn("Please choose one change first. What would you like me to do?", [])

    if result.intent == "schedule_workout":
        date_text = slots.get("date_text")
        if not date_text:
            _save_dialogue(session, result.intent, slots, "date", now)
            return RoutedTurn("Which day should I schedule it for?", [])
        from coach.interactions import _stage_explicit_schedule
        combined = " ".join(filter(None, ("schedule workout", date_text, slots.get("time_text"))))
        text, interactions = _stage_explicit_schedule(session, combined, now)
        if interactions:
            _clear_dialogue(session)
        return RoutedTurn(text, interactions)

    if result.intent == "find_workout_time":
        from coach.calendar import get_upcoming_schedule_result
        from coach.scheduling import next_available_time, requested_day
        target = requested_day(slots.get("date_text", ""), now.date())
        calendar = get_upcoming_schedule_result(days=7)
        if calendar["state"] != "fresh":
            reason = "not connected" if calendar["state"] == "unconfigured" else "unavailable"
            return RoutedTurn(f"Calendar is {reason}, so I cannot verify a workout time.", [])
        suggestion = next_available_time(
            session, now=now, schedule=calendar["events"], start_day=target,
            max_days=1 if target else 7,
        )
        return RoutedTurn(
            suggestion.render() if suggestion else "No full workout slot is available.", []
        )

    if result.intent == "recommend_workout":
        from coach.decision_engine import evaluate_morning_decision
        from coach.renderer import render_morning
        decision = evaluate_morning_decision(session, target=now.date(), evaluated_at=now)
        text, _markup, ids = render_morning(session, decision)
        rows = [session.get(PendingInteraction, item) for item in ids]
        return RoutedTurn(text or "Today's decision is waiting for required data.", [r for r in rows if r])

    if result.intent == "reschedule_workout":
        planned = _planned_target(session, now)
        if not planned:
            return RoutedTurn("There is no current scheduled workout to reschedule.", [])
        time_text = slots.get("time_text")
        if not time_text:
            _save_dialogue(session, result.intent, {**slots, "planned_session_id": planned.id}, "time", now)
            return RoutedTurn("What exact same-day time should I use?", [])
        from coach.scheduling import _parse_clock
        parsed = _parse_clock(time_text)
        if not parsed:
            return RoutedTurn("State an exact time, for example 18:00 or 6 pm.", [])
        value = parsed.strftime("%H:%M")
        row = _stage_simple_action(
            session, action_type="reschedule_planned_time", target_type="planned_session",
            target_id=planned.id,
            payload={"planned_session_id": planned.id, "suggested_time": value}, now=now,
        )
        _clear_dialogue(session)
        return RoutedTurn(f"Confirm: move {planned.title} to {value} on {planned.target_date}.", [row])

    if result.intent == "cancel_workout":
        planned = _planned_target(session, now)
        if not planned:
            return RoutedTurn("There is no current scheduled workout to cancel.", [])
        row = _stage_simple_action(
            session, action_type="cancel_planned_session", target_type="planned_session",
            target_id=planned.id, payload={"planned_session_id": planned.id}, now=now,
        )
        return RoutedTurn(
            f"Confirm: cancel {planned.title} on {planned.target_date} at {planned.suggested_time}.", [row]
        )

    if result.intent == "request_sync":
        row = _stage_simple_action(
            session, action_type="start_sync", target_type="sync", target_id=None,
            payload={"full": False}, now=now,
        )
        return RoutedTurn("Confirm: start a Garmin sync now.", [row])

    if result.intent == "report_safety_issue":
        report_type = "dizziness" if any(x in user_text.casefold() for x in ("dizz", "faint")) else "pain" if "pain" in user_text.casefold() else "difficulty"
        row = _stage_simple_action(
            session, action_type="confirm_safety_report", target_type="safety_report",
            target_id=None, payload={"report_type": report_type, "report_text": user_text}, now=now,
        )
        return RoutedTurn(f"Confirm this safety report: {user_text}", [row])

    if result.intent == "skip_workout":
        from coach.decision_engine import evaluate_morning_decision
        from coach.interactions import stage_decision_actions
        decision = evaluate_morning_decision(session, target=now.date(), evaluated_at=now)
        rows = stage_decision_actions(session, decision, action_types={"skip_today"})
        return RoutedTurn("Confirm: skip today's workout." if rows else "Skipping is not available for the current decision.", rows)

    if result.intent == "get_sync_status":
        from sync import sync_runner
        return RoutedTurn("A Garmin sync is running." if sync_runner.is_running() else "No Garmin sync is currently running.", [])

    if result.intent == "get_program":
        from coach.onboarding import active_program
        from coach.program_state import program_state_facts
        program = active_program(session)
        if not program:
            return RoutedTurn("There is no active training program.", [])
        state = program_state_facts(session, program)
        return RoutedTurn(f"Active program: {program.name}. Next session: {(state or {}).get('next_session_name') or 'not available'}.", [])

    if result.intent == "help":
        return RoutedTurn("I can explain your data and program, recommend the current session, find a time, schedule, reschedule, cancel, skip, or start a sync. Every change requires confirmation.", [])

    # Read-only topics use the existing grounded informational answer path.
    if result.intent in {
        "get_workout_details", "get_calendar", "get_metrics", "get_activity_history",
        "explain_decision", "general_question", "unknown",
    }:
        _clear_dialogue(session)
        return None
    return RoutedTurn("I could not safely map that request. Please rephrase it.", [])


def route_chat(session: Session, user_text: str) -> RoutedTurn | None:
    """Classify every free-text turn; shadow records only, guarded dispatches."""
    now = get_local_now().replace(tzinfo=None)
    dialogue = _dialogue(session, now)
    started = time.monotonic()
    try:
        result = classify_intent(user_text, dialogue)
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        logger.warning("Intent classification failed: %s", exc)
        _audit(session, user_text, result=None, status="invalid", failure=str(exc), latency_ms=latency)
        if config.CHAT_ROUTER_MODE == "shadow" and not dialogue:
            return None
        return RoutedTurn("I couldn't safely identify that request. Please try again.", [])

    latency = int((time.monotonic() - started) * 1000)
    _audit(session, user_text, result=result, status="valid", failure="", latency_ms=latency)
    if config.CHAT_ROUTER_MODE == "shadow" and not dialogue:
        return None
    return _route_guarded(session, user_text, result, dialogue, now)
