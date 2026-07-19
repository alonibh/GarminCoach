"""Closed-catalog, deterministic Telegram chat routing.

No model participates in intent selection, slot filling, or response creation.
Every state-changing request is converted into a versioned pending interaction
that can only be applied by its Telegram confirmation button.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import re
import unicodedata
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from db import (
    Activity,
    ChatDialogueState,
    ChatIntentAudit,
    DailyHealth,
    DailyMetrics,
    DecisionRecord,
    ObservationFreshness,
    PendingInteraction,
    PlannedSession,
    ProgramSession,
    SessionExercise,
    Sleep,
    SyncState,
)
from time_utils import format_chat_date, format_chat_datetime, get_local_now


IntentName = Literal[
    "recommend_workout", "get_workout_details", "find_workout_time",
    "schedule_workout", "reschedule_workout", "cancel_workout",
    "report_safety_issue", "request_sync", "get_sync_status", "get_program",
    "get_calendar", "get_metrics", "get_activity_history", "explain_decision",
    "help", "multiple_intents", "unknown",
]

MUTATING_INTENTS = {
    "schedule_workout", "reschedule_workout", "cancel_workout",
    "report_safety_issue", "request_sync",
}


class IntentClassification(BaseModel):
    """Typed output of the local catalog recognizer (kept audit-friendly)."""

    model_config = ConfigDict(extra="forbid")

    intent: IntentName
    date_text: str | None = Field(default=None, max_length=64)
    time_text: str | None = Field(default=None, max_length=32)
    workout_text: str | None = Field(default=None, max_length=128)
    topic: str | None = Field(default=None, max_length=128)
    missing_slots: list[Literal["date", "time", "workout"]] = Field(default_factory=list)


@dataclass
class RoutedTurn:
    text: str
    interactions: list[PendingInteraction]
    reply_markup: dict | None = None


_DATE_PATTERN = re.compile(
    r"\b(today|tomorrow|tonight|this evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_EXPLICIT_TIME_PATTERN = re.compile(
    r"\b((?:[01]?\d|2[0-3]):[0-5]\d|(?:1[0-2]|0?[1-9])\s*(?:a\.?m\.?|p\.?m\.?))\b",
    re.IGNORECASE,
)
_CANCEL_VERBS = r"cancel|delete|remove|unschedule"
_CANCEL_VERB = re.compile(rf"\b(?:{_CANCEL_VERBS})\b")
_MUTATION_VERB = re.compile(
    rf"\b(?:schedule|book|{_CANCEL_VERBS}|reschedule|move|start|sync|record|report)\b"
)
_NEGATION_WORD = re.compile(r"\b(?:no|not|never|don't|dont|do\s+not|shouldn't|shouldnt|cannot|can't|cant|without)\b")
_WORKOUT_REFERENCE = re.compile(r"\b(workout|work out|session|training|day\s+\d+|it)\b", re.IGNORECASE)


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = value.replace("’", "'")
    value = value.replace("’", "'")
    return " ".join(value.strip().split())


def _evidence(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None


def _workout_evidence(text: str) -> str | None:
    match = _WORKOUT_REFERENCE.search(text)
    return match.group(1) if match else None


def _metric_topic(text: str) -> str | None:
    topics = (
        ("readiness", r"\b(?:training\s+)?readiness\b"),
        ("sleep", r"\bsleep(?:\s+score)?\b"),
        ("hrv", r"\bhrv\b|heart\s+rate\s+variability"),
        ("recovery", r"\brecovery\b|body\s+battery"),
        ("training_load", r"\btraining\s+load\b|\bload\b"),
    )
    for topic, pattern in topics:
        if re.search(pattern, text):
            return topic
    return None


def _explicit_classification(user_text: str) -> IntentClassification:
    text = _normalized(user_text)
    date_text = _evidence(_DATE_PATTERN, user_text)
    time_text = _evidence(_EXPLICIT_TIME_PATTERN, user_text)
    workout_text = _workout_evidence(user_text)
    # Conservative by design: any negation in a clause containing an operation
    # verb prevents that clause from reaching a mutating intent.
    negated_mutation = bool(_MUTATION_VERB.search(text) and _NEGATION_WORD.search(text))

    if text in {"/start", "/help", "/menu", "help", "menu", "what can you do"}:
        return IntentClassification(intent="help")

    first_person = bool(re.search(r"\b(i|i'm|im|my|me)\b", text))
    symptom = re.search(
        r"\b(dizz(?:y|iness)|faint(?:ed|ing)?|chest pain|sharp pain|difficulty breathing|can't breathe|cannot breathe|injur(?:y|ed))\b",
        text,
    )
    if first_person and symptom:
        return IntentClassification(intent="report_safety_issue", topic=symptom.group(1))

    if not negated_mutation and _CANCEL_VERB.search(text):
        if workout_text or date_text or re.search(r"\bbooking\b", text):
            return IntentClassification(
                intent="cancel_workout", date_text=date_text, workout_text=workout_text,
            )

    if not negated_mutation and re.search(
        r"\b(reschedule|move)\b|\bchange\s+(?:the\s+)?(?:(?:workout|session)\s+)?(?:date|time)\b",
        text,
    ):
        if workout_text or date_text or re.search(r"\bbooking\b", text):
            return IntentClassification(
                intent="reschedule_workout", date_text=date_text,
                time_text=time_text, workout_text=workout_text,
            )

    schedule_verb = re.search(r"\b(schedule|book|arrange)\b|\bfit\b.+\bin\b|\bbook me in\b", text)
    if schedule_verb and not negated_mutation and (workout_text or date_text or "book me in" in text):
        missing = [] if date_text else ["date"]
        return IntentClassification(
            intent="schedule_workout", date_text=date_text, time_text=time_text,
            workout_text=workout_text, missing_slots=missing,
        )

    if not negated_mutation and (
        re.search(r"\b(sync|refresh|fetch|update)\b.*\b(garmin|data|now)\b", text)
        or text in {"sync now", "start garmin sync", "refresh garmin", "refresh my garmin data"}
    ):
        return IntentClassification(intent="request_sync")

    if re.search(r"\b(sync status|last sync|is (?:my )?data fresh|data freshness|up to date)\b", text):
        return IntentClassification(intent="get_sync_status")

    if re.search(r"\b(why|explain)\b", text) and re.search(r"\b(recommendation|decision|today|rest|workout)\b", text):
        return IntentClassification(intent="explain_decision")

    if re.search(r"\b(recommend|what should i do|should i (?:train|work out)|today's recommendation|todays recommendation)\b", text):
        return IntentClassification(intent="recommend_workout", date_text=date_text)

    if text == "find a workout time" or (
        re.search(r"\b(when|what time|available|availability|free|could i|can i)\b", text)
        and re.search(r"\b(train|training|workout|work out|session|do it)\b", text)
    ):
        return IntentClassification(intent="find_workout_time", date_text=date_text, workout_text=workout_text)

    if re.search(r"\b(details?|exercises?|sets?|reps?|what(?:'s| is) in|tell me about)\b", text) and (
        workout_text or re.search(r"\bday\s+\d+\b", text)
    ):
        return IntentClassification(intent="get_workout_details", workout_text=workout_text)
    if re.fullmatch(r"(?:what is|what's)?\s*(?:my\s+)?next\s+(?:workout|session)\??", text):
        return IntentClassification(intent="get_workout_details", workout_text="next workout")

    topic = _metric_topic(text)
    if topic and re.search(r"\b(what|show|tell|how|mean|status|my|today|latest)\b", text):
        return IntentClassification(intent="get_metrics", topic=topic, date_text=date_text)
    if text in {"metrics", "show metrics", "my metrics"}:
        return IntentClassification(intent="get_metrics", topic="summary")

    if re.search(r"\b(recent activities|activity history|last workout|workout history|what did i do)\b", text):
        return IntentClassification(intent="get_activity_history")

    if re.search(r"\b(program status|training program|my program|training plan|next program session)\b", text):
        return IntentClassification(intent="get_program")

    if (
        re.search(r"\b(calendar|my schedule|scheduled workouts?|what(?:'s| is) planned|plan for (?:today|tomorrow|this week))\b", text)
        and not schedule_verb
    ):
        return IntentClassification(intent="get_calendar", date_text=date_text)

    return IntentClassification(intent="unknown")


def classify_intent(user_text: str, dialogue: ChatDialogueState | None = None) -> IntentClassification:
    """Map text to the closed catalog without any network or model call."""
    explicit = _explicit_classification(user_text)
    if explicit.intent != "unknown" or dialogue is None:
        return explicit

    if dialogue.missing_slot == "date":
        date_text = _evidence(_DATE_PATTERN, user_text)
        if date_text:
            return IntentClassification(intent=dialogue.intent, date_text=date_text)
    if dialogue.missing_slot == "time":
        from coach.scheduling import _parse_clock
        time_text = _evidence(_EXPLICIT_TIME_PATTERN, user_text)
        if time_text is None and _parse_clock(user_text.strip()):
            time_text = user_text.strip()
        if time_text:
            return IntentClassification(intent=dialogue.intent, time_text=time_text)
    return explicit


def _audit(session: Session, user_text: str, result: IntentClassification, status: str = "valid") -> None:
    session.add(ChatIntentAudit(
        message_text=user_text,
        provider="deterministic",
        model="closed-catalog-v1",
        router_mode="deterministic",
        intent=result.intent,
        evidence_json=json.dumps(result.model_dump(), sort_keys=True),
        validation_status=status,
        failure_reason="",
        latency_ms=0,
        created_at=get_local_now().replace(tzinfo=None),
    ))


def _dialogue(session: Session, now: datetime) -> ChatDialogueState | None:
    row = session.get(ChatDialogueState, 1)
    if not row:
        return None
    try:
        slots = json.loads(row.slots_json or "{}")
    except ValueError:
        session.delete(row)
        session.flush()
        return None
    target_text = slots.get("target_date") or slots.get("date_iso")
    if target_text:
        try:
            if date.fromisoformat(target_text) < now.date():
                session.delete(row)
                session.flush()
                return None
        except ValueError:
            pass
    planned_id = slots.get("planned_session_id")
    if planned_id:
        planned = session.get(PlannedSession, planned_id)
        if not planned or planned.status in {"completed", "cancelled"}:
            session.delete(row)
            session.flush()
            return None
    return row


def _save_dialogue(session: Session, intent: str, slots: dict, missing: str, now: datetime) -> None:
    existing = session.get(ChatDialogueState, 1)
    created_at = existing.created_at if existing else now
    session.merge(ChatDialogueState(
        state_id=1,
        intent=intent,
        slots_json=json.dumps(slots, sort_keys=True),
        missing_slot=missing,
        created_at=created_at,
        updated_at=now,
        # Context has semantic rather than inactivity expiry. This far-future
        # value preserves the existing non-null schema during migration.
        expires_at=datetime(2099, 12, 31, 23, 59),
    ))


def _clear_dialogue(session: Session) -> None:
    row = session.get(ChatDialogueState, 1)
    if row:
        session.delete(row)
        session.flush()


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
        expires_at=now + timedelta(hours=6), status="pending",
    )
    session.add(row)
    session.flush()
    return row


def _planned_candidates(session: Session, now: datetime, date_text: str | None = None) -> list[PlannedSession]:
    query = (
        session.query(PlannedSession)
        .filter(PlannedSession.target_date >= now.date())
        .filter(PlannedSession.status == "approved")
    )
    if date_text:
        from coach.scheduling import requested_day
        target = requested_day(date_text, now.date())
        if target:
            query = query.filter(PlannedSession.target_date == target)
    return query.order_by(PlannedSession.target_date, PlannedSession.suggested_time, PlannedSession.id).limit(5).all()


def _planned_target(session: Session, now: datetime, date_text: str | None = None) -> PlannedSession | None:
    candidates = _planned_candidates(session, now, date_text)
    return candidates[0] if len(candidates) == 1 else None


def _menu_markup() -> dict:
    return {
        "keyboard": [
            [{"text": "Today's recommendation"}, {"text": "Next workout"}],
            [{"text": "Explain recommendation"}, {"text": "Find a workout time"}],
            [{"text": "Schedule workout"}, {"text": "Change workout date"}],
            [{"text": "Cancel workout"}, {"text": "My calendar"}],
            [{"text": "Metrics"}, {"text": "Recent activities"}],
            [{"text": "Program status"}, {"text": "Sync status"}],
            [{"text": "Start Garmin sync"}, {"text": "Help"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _last_sync_text(session: Session) -> str:
    row = session.get(SyncState, "last_sync_at")
    formatted = format_chat_datetime(row.value) if row and row.value else None
    return f"Last sync: {formatted}." if formatted else "Last sync: unavailable."


def _metric_response(session: Session, topic: str | None, today: date) -> str:
    health = session.get(DailyHealth, today)
    sleep = session.get(Sleep, today)
    metrics = session.get(DailyMetrics, today)
    unavailable = "unavailable for today"
    if topic == "readiness":
        value = health.training_readiness if health else None
        answer = f"Training readiness: {int(value)}." if value is not None else f"Training readiness is {unavailable}."
    elif topic == "sleep":
        if sleep and sleep.total_s:
            score = f", score {int(sleep.score)}" if sleep.score is not None else ""
            answer = f"Sleep: {sleep.total_s / 3600:.1f} hours{score}."
        else:
            answer = f"Sleep is {unavailable}."
    elif topic == "hrv":
        value = health.hrv_overnight if health else None
        answer = f"Overnight HRV: {value:.0f} ms." if value is not None else f"HRV is {unavailable}."
    elif topic == "recovery":
        readiness = health.training_readiness if health else None
        battery = health.body_battery_current if health else None
        parts = []
        if readiness is not None:
            parts.append(f"readiness {int(readiness)}")
        if battery is not None:
            parts.append(f"Body Battery {int(battery)}")
        answer = f"Recovery: {', '.join(parts)}." if parts else f"Recovery data is {unavailable}."
    elif topic == "training_load":
        value = metrics.acute_load if metrics else None
        answer = f"Current training load: {value:.1f}." if value is not None else f"Training load is {unavailable}."
    else:
        parts = []
        if health and health.training_readiness is not None:
            parts.append(f"readiness {int(health.training_readiness)}")
        if sleep and sleep.total_s:
            parts.append(f"sleep {sleep.total_s / 3600:.1f} h")
        if health and health.body_battery_current is not None:
            parts.append(f"Body Battery {int(health.body_battery_current)}")
        answer = f"Today's metrics: {', '.join(parts)}." if parts else "Today's supported metrics are unavailable."
    return f"{answer}\n{_last_sync_text(session)}"


def metric_detail_response(session: Session, topic: str) -> str:
    """Render the fixed expanded metric contract for a More details callback."""
    today = get_local_now().date()
    health = session.get(DailyHealth, today)
    sleep = session.get(Sleep, today)
    metrics = session.get(DailyMetrics, today)
    signal = {
        "readiness": "training_readiness", "sleep": "sleep", "hrv": "hrv",
        "recovery": "training_readiness", "training_load": "training_load",
        "summary": "sleep",
    }.get(topic, topic)
    freshness = session.get(ObservationFreshness, (signal, today))
    fetched = format_chat_datetime(freshness.fetched_at) if freshness else None
    freshness_text = f"Freshness: {freshness.state}; fetched {fetched}." if fetched else "Freshness: unavailable."
    if topic == "readiness":
        value = health.training_readiness if health else None
        from coach.decision_engine import training_readiness_category
        body = (
            f"Training readiness: {int(value)} ({training_readiness_category(int(value))})."
            if value is not None else "Training readiness is unavailable; no substitute was inferred."
        )
    elif topic == "sleep":
        if sleep and sleep.total_s:
            score = f" Score: {int(sleep.score)}." if sleep.score is not None else ""
            window = (
                f" Window: {format_chat_datetime(sleep.sleep_start_time)} to {format_chat_datetime(sleep.sleep_end_time)}."
                if sleep.sleep_start_time and sleep.sleep_end_time else ""
            )
            body = f"Sleep duration: {sleep.total_s / 3600:.1f} hours.{score}{window}"
        else:
            body = "Sleep is unavailable; no duration or score was inferred."
    elif topic == "hrv":
        value = health.hrv_overnight if health else None
        baseline = (
            f" Personal baseline: {health.hrv_baseline_low:.0f}-{health.hrv_baseline_high:.0f} ms."
            if health and health.hrv_baseline_low is not None and health.hrv_baseline_high is not None else ""
        )
        body = f"Overnight HRV: {value:.0f} ms.{baseline}" if value is not None else "HRV is unavailable."
    elif topic == "recovery":
        values = []
        if health and health.training_readiness is not None:
            values.append(f"readiness {int(health.training_readiness)}")
        if health and health.body_battery_current is not None:
            values.append(f"Body Battery {int(health.body_battery_current)}")
        if health and health.resting_hr is not None:
            values.append(f"resting HR {int(health.resting_hr)} bpm")
        body = "Recovery facts: " + ", ".join(values) + "." if values else "Recovery facts are unavailable."
    elif topic == "training_load":
        values = []
        if metrics and metrics.acute_load is not None:
            values.append(f"acute {metrics.acute_load:.1f}")
        if metrics and metrics.chronic_load is not None:
            values.append(f"chronic {metrics.chronic_load:.1f}")
        body = "Training load: " + ", ".join(values) + "." if values else "Training load is unavailable."
    else:
        body = _metric_response(session, "summary", today).splitlines()[0]
    return f"{body}\n{freshness_text}\n{_last_sync_text(session)}"


def _details_markup(topic: str) -> dict:
    return {"inline_keyboard": [[{
        "text": "More details",
        "callback_data": f"catalog_details_metric_{topic}",
    }]]}


def _workout_details(session: Session) -> str:
    from coach.onboarding import active_program
    from coach.program_state import program_state_facts
    program = active_program(session)
    if not program:
        return "There is no active approved training program."
    state = program_state_facts(session, program)
    session_id = (state or {}).get("next_session_id")
    item = session.get(ProgramSession, session_id) if session_id else None
    if not item:
        return "The next approved workout is unavailable."
    exercises = (
        session.query(SessionExercise)
        .filter_by(program_session_id=item.id)
        .order_by(SessionExercise.order_index, SessionExercise.id)
        .all()
    )
    lines = [f"Next workout: {item.name} ({item.duration_min or 60} min)."]
    if exercises:
        rendered = []
        for exercise in exercises:
            dose = f"{exercise.sets}×{exercise.reps}" if exercise.sets and exercise.reps else ""
            rendered.append(" ".join(part for part in (exercise.exercise_name, dose) if part))
        lines.append("Exercises: " + "; ".join(rendered) + ".")
    return "\n".join(lines)


def _calendar_response(session: Session, now: datetime) -> str:
    from coach.calendar import get_upcoming_schedule_result
    planned = (
        session.query(PlannedSession)
        .filter(PlannedSession.target_date >= now.date())
        .filter(PlannedSession.status.notin_(("completed", "cancelled")))
        .order_by(PlannedSession.target_date, PlannedSession.suggested_time)
        .limit(5).all()
    )
    calendar = get_upcoming_schedule_result(days=7)
    if calendar["state"] == "error":
        return "Calendar data is unavailable. No scheduling conclusion was inferred."
    lines = [
        f"Workout: {item.title} — {item.target_date:%a} {item.suggested_time}."
        for item in planned
    ]
    lines.extend(
        f"Calendar: {event.get('title') or 'Untitled event'} — "
        f"{format_chat_datetime(event.get('start')) or event.get('start', '')}."
        for event in calendar["events"][:5]
    )
    return "\n".join(lines) if lines else "Nothing is scheduled in the next 7 days."


def _activity_response(session: Session) -> str:
    rows = session.query(Activity).order_by(Activity.start_time.desc(), Activity.id.desc()).limit(5).all()
    if not rows:
        return "No synced activities are available."
    return "Recent activities:\n" + "\n".join(
        f"• {(row.name or row.activity_type or 'Activity')} — {format_chat_date(row.start_time)}"
        for row in rows if row.start_time
    )


_REASON_TEXT = {
    "PROGRAM_SPACING_REQUIRES_REST": "the approved program requires recovery spacing",
    "GARMIN_READINESS_POOR": "Garmin readiness is Poor",
    "GARMIN_READINESS_LOW": "Garmin readiness is Low",
    "NEXT_PROGRAM_SESSION_ELIGIBLE": "the next program session is eligible",
    "PLANNED_SESSION_ALREADY_EXISTS": "a workout is already scheduled",
    "CALENDAR_CONFLICT": "the scheduled time conflicts with the calendar",
    "GARMIN_READINESS_UNAVAILABLE_NO_SUBSTITUTE": "readiness is unavailable and was not inferred",
    "ATHLETE_REQUESTED_ANSWER_WITH_MISSING_DATA": "the decision is best effort because required data is missing",
}


def _explain_decision(session: Session, now: datetime) -> str:
    row = (
        session.query(DecisionRecord)
        .filter(DecisionRecord.evaluated_at >= datetime.combine(now.date(), datetime.min.time()))
        .order_by(DecisionRecord.evaluated_at.desc()).first()
    )
    if not row:
        return "No morning decision has been recorded today."
    reasons = json.loads(row.reason_codes_json or "[]")
    rendered = [_REASON_TEXT.get(reason, reason.replace("_", " ").lower()) for reason in reasons]
    return "Today's decision was based on " + "; ".join(rendered) + "."


def _available_time_prompt(
    session: Session, *, intent: str, slots: dict, target_day: date,
    duration_min: int, now: datetime,
) -> RoutedTurn:
    from coach.calendar import get_upcoming_schedule_result
    from coach.scheduling import available_start_times
    calendar = get_upcoming_schedule_result(days=7)
    if calendar["state"] != "fresh":
        return RoutedTurn("Calendar is unavailable, so no valid times can be offered.", [])
    times = available_start_times(
        session, now=now, schedule=calendar["events"], target_day=target_day,
        duration_min=duration_min, limit=3,
    )
    if not times:
        return RoutedTurn(f"No valid full-workout time is available on {target_day:%A}.", [])
    values = [value.strftime("%H:%M") for value in times]
    _save_dialogue(
        session, intent,
        {**slots, "target_date": target_day.isoformat(), "available_times": values},
        "time", now,
    )
    return RoutedTurn(
        f"Available on {target_day:%A}: {', '.join(values)}. Which time should I use?",
        [],
    )


def _route_deterministic(
    session: Session, user_text: str, result: IntentClassification,
    dialogue: ChatDialogueState | None, now: datetime,
) -> RoutedTurn:
    prior_slots = json.loads(dialogue.slots_json or "{}") if dialogue and dialogue.intent == result.intent else {}
    slots = dict(prior_slots)
    for key in ("date_text", "time_text", "workout_text"):
        value = getattr(result, key)
        if value:
            slots[key] = value

    if dialogue and result.intent != dialogue.intent and result.intent != "unknown":
        _clear_dialogue(session)
        slots = {key: value for key, value in slots.items() if key not in {"planned_session_id", "target_date", "available_times"}}

    if result.intent == "schedule_workout":
        from coach.interactions import _stage_explicit_schedule
        from coach.scheduling import requested_day
        date_text = slots.get("date_text")
        if not date_text:
            _save_dialogue(session, result.intent, slots, "date", now)
            return RoutedTurn("Which date should I use?", [])
        target_day = requested_day(date_text, now.date())
        if not target_day:
            return RoutedTurn("Use today, tomorrow, a weekday, or YYYY-MM-DD.", [])
        if slots.get("selection_mode") and not slots.get("time_text"):
            return _available_time_prompt(
                session, intent=result.intent, slots=slots, target_day=target_day,
                duration_min=int(slots.get("duration_min") or 60), now=now,
            )
        if slots.get("available_times") and slots.get("time_text"):
            from coach.scheduling import _parse_clock
            parsed = _parse_clock(slots["time_text"])
            value = parsed.strftime("%H:%M") if parsed else ""
            if value not in slots["available_times"]:
                return RoutedTurn(f"Choose one of these valid times: {', '.join(slots['available_times'])}.", [])
        combined = " ".join(filter(None, ("schedule workout", date_text, slots.get("time_text"))))
        text, interactions = _stage_explicit_schedule(session, combined, now)
        if interactions:
            _clear_dialogue(session)
        return RoutedTurn(text, interactions)

    if result.intent == "reschedule_workout":
        from coach.scheduling import _parse_clock, requested_day
        planned = session.get(PlannedSession, slots.get("planned_session_id")) if slots.get("planned_session_id") else None
        if planned is None:
            candidates = _planned_candidates(session, now, result.date_text)
            if len(candidates) > 1:
                rows = [
                    _stage_simple_action(
                        session, action_type="request_reschedule", target_type="planned_session",
                        target_id=item.id,
                        payload={"planned_session_id": item.id, "title": item.title}, now=now,
                    )
                    for item in candidates
                ]
                choices = "\n".join(
                    f"• {item.title} — {item.target_date:%A} at {item.suggested_time}"
                    for item in candidates
                )
                return RoutedTurn(f"Which workout should be changed?\n{choices}", rows)
            planned = candidates[0] if candidates else None
        if not planned:
            return RoutedTurn("There is no current scheduled workout to change.", [])
        date_text = slots.get("date_text")
        if not date_text:
            _save_dialogue(
                session, result.intent,
                {**slots, "planned_session_id": planned.id, "duration_min": planned.duration_min},
                "date", now,
            )
            return RoutedTurn("Which new date should I use?", [])
        target_day = requested_day(date_text, now.date())
        if not target_day:
            return RoutedTurn("Use today, tomorrow, a weekday, or YYYY-MM-DD.", [])
        if not slots.get("time_text"):
            return _available_time_prompt(
                session, intent=result.intent,
                slots={**slots, "planned_session_id": planned.id, "duration_min": planned.duration_min},
                target_day=target_day, duration_min=planned.duration_min, now=now,
            )
        parsed = _parse_clock(slots["time_text"])
        value = parsed.strftime("%H:%M") if parsed else ""
        if not value or (slots.get("available_times") and value not in slots["available_times"]):
            choices = ", ".join(slots.get("available_times", []))
            return RoutedTurn(f"Choose one of these valid times: {choices}." if choices else "State an exact valid time.", [])
        row = _stage_simple_action(
            session, action_type="reschedule_planned_time", target_type="planned_session",
            target_id=planned.id,
            payload={"planned_session_id": planned.id, "target_date": target_day.isoformat(), "suggested_time": value},
            now=now,
        )
        _clear_dialogue(session)
        return RoutedTurn(f"Move {planned.title} to {target_day:%A} at {value}?", [row])

    if result.intent == "cancel_workout":
        candidates = _planned_candidates(session, now, result.date_text)
        if not candidates:
            return RoutedTurn("There is no matching scheduled workout to cancel.", [])
        rows = [
            _stage_simple_action(
                session, action_type="cancel_planned_session", target_type="planned_session",
                target_id=planned.id,
                payload={"planned_session_id": planned.id, "title": planned.title}, now=now,
            )
            for planned in candidates
        ]
        if len(candidates) == 1:
            planned = candidates[0]
            text = f"Cancel {planned.title} on {planned.target_date:%A} at {planned.suggested_time}? The program workout will remain pending."
        else:
            choices = "\n".join(
                f"• {item.title} — {item.target_date:%A} at {item.suggested_time}"
                for item in candidates
            )
            text = f"Choose the exact cancellation. Each program workout remains pending.\n{choices}"
        return RoutedTurn(text, rows)

    if result.intent == "find_workout_time":
        from coach.calendar import get_upcoming_schedule_result
        from coach.scheduling import next_available_time, requested_day
        target = requested_day(result.date_text or "", now.date())
        calendar = get_upcoming_schedule_result(days=7)
        if calendar["state"] != "fresh":
            reason = "not connected" if calendar["state"] == "unconfigured" else "unavailable"
            return RoutedTurn(f"Calendar is {reason}, so I cannot verify a workout time.", [])
        suggestion = next_available_time(
            session, now=now, schedule=calendar["events"], start_day=target,
            max_days=1 if target else 7,
        )
        if suggestion:
            return RoutedTurn(suggestion.render(), [])
        return RoutedTurn(
            f"No full workout slot is available {target:%A}." if target
            else "No full workout slot is available in the next 7 days.",
            [],
        )

    if result.intent == "recommend_workout":
        from coach.decision_engine import evaluate_morning_decision
        from coach.renderer import render_morning
        from db import MorningBriefState
        morning = session.get(MorningBriefState, now.date())
        decision = evaluate_morning_decision(
            session, target=now.date(), evaluated_at=now,
            allow_incomplete=bool(morning and morning.answer_anyway),
        )
        text, _markup, ids = render_morning(session, decision)
        rows = [session.get(PendingInteraction, item) for item in ids]
        return RoutedTurn(text or "Today's decision is waiting for required data.", [row for row in rows if row])

    if result.intent == "request_sync":
        row = _stage_simple_action(
            session, action_type="start_sync", target_type="sync", target_id=None,
            payload={"full": False}, now=now,
        )
        return RoutedTurn("Start a Garmin sync now?", [row])

    if result.intent == "report_safety_issue":
        lowered = user_text.casefold()
        urgent = any(value in lowered for value in ("chest pain", "can't breathe", "cannot breathe", "faint"))
        report_type = "dizziness" if any(value in lowered for value in ("dizz", "faint")) else "pain" if "pain" in lowered else "difficulty"
        row = _stage_simple_action(
            session, action_type="confirm_safety_report", target_type="safety_report",
            target_id=None, payload={"report_type": report_type, "report_text": user_text}, now=now,
        )
        opening = "Stop exercising and seek urgent medical help now." if urgent else "Stop the workout if the symptom is continuing."
        return RoutedTurn(f"{opening} I cannot diagnose this. Record this safety report?", [row])

    if result.intent == "get_sync_status":
        from sync import sync_runner
        state = "A Garmin sync is running." if sync_runner.is_running() else "No Garmin sync is currently running."
        return RoutedTurn(f"{state}\n{_last_sync_text(session)}", [])

    if result.intent == "get_program":
        from coach.onboarding import active_program
        from coach.program_state import program_state_facts
        program = active_program(session)
        if not program:
            return RoutedTurn("There is no active approved training program.", [])
        state = program_state_facts(session, program)
        return RoutedTurn(
            f"Active program: {program.name}. Next workout: {(state or {}).get('next_session_name') or 'unavailable'}.",
            [],
        )

    if result.intent == "get_workout_details":
        return RoutedTurn(_workout_details(session), [])
    if result.intent == "get_calendar":
        return RoutedTurn(_calendar_response(session, now), [])
    if result.intent == "get_metrics":
        topic = result.topic or "summary"
        return RoutedTurn(_metric_response(session, topic, now.date()), [], _details_markup(topic))
    if result.intent == "get_activity_history":
        return RoutedTurn(_activity_response(session), [])
    if result.intent == "explain_decision":
        return RoutedTurn(_explain_decision(session, now), [])
    if result.intent == "help":
        return RoutedTurn(
            "Choose a supported action. Every schedule change requires its exact confirmation button. "
            "To report a safety issue, describe the symptom you experienced during or after training.",
            [], _menu_markup(),
        )

    _clear_dialogue(session)
    normalized = _normalized(user_text)
    if _MUTATION_VERB.search(normalized) and _NEGATION_WORD.search(normalized):
        text = "Nothing was changed. Choose a supported action if you want to do something else."
    else:
        text = "I couldn't match that safely to a supported request. Choose an action from the menu."
    return RoutedTurn(text, [], _menu_markup())


def _multiple_mutations(user_text: str) -> list[tuple[str, IntentClassification]]:
    clauses = [part.strip(" ,.") for part in re.split(r"\s+(?:and|then)\s+|;", user_text, flags=re.IGNORECASE) if part.strip()]
    classified = [(clause, _explicit_classification(clause)) for clause in clauses]
    mutations = [(clause, result) for clause, result in classified if result.intent in MUTATING_INTENTS]
    return mutations if len(mutations) > 1 else []


def route_chat(session: Session, user_text: str) -> RoutedTurn:
    """Route one message through the deterministic catalog."""
    now = get_local_now().replace(tzinfo=None)
    dialogue = _dialogue(session, now)

    mutations = _multiple_mutations(user_text) if dialogue is None else []
    if mutations:
        _clear_dialogue(session)
        turns = []
        interactions: list[PendingInteraction] = []
        for clause, result in mutations:
            _audit(session, clause, result)
            turn = _route_deterministic(session, clause, result, None, now)
            turns.append(turn.text)
            interactions.extend(turn.interactions)
        return RoutedTurn("\n\n".join(turns), interactions)

    result = classify_intent(user_text, dialogue)
    _audit(session, user_text, result)
    return _route_deterministic(session, user_text, result, dialogue, now)
