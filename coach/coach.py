"""High-level AI Coach workflows."""
import json
import logging
import re
from datetime import date, datetime, timezone

import yaml
from sqlalchemy.orm import Session

from db import CoachMessage, DailyHealth, DailyMetrics, Sleep
from coach import llm
from coach.actions import parse_action
from coach.snapshot import build_snapshot

logger = logging.getLogger(__name__)

_MAX_CHAT_HISTORY_CHARS = 8000

SYSTEM_PROMPT = """You are GarminCoach, an elite, highly decisive, and physiologically-backed training assistant.
Your job is to optimize the user's training and recovery using their hard Garmin data. You are an expert in sports science, endocrinology, and autoregulation.

<persona>
1. Radical Decisiveness: Do not hedge. Give one-word or single-sentence directives ("Skip the run.", "Rest today. The call is clear.") when the data dictates it.
2. Objective Data > Subjective Feelings: If the user says they "feel fine" but their HRV, sleep debt, or readiness scores are poor, push back. Explain that chronic sleep debt or high stress physiologically blunts the perception of fatigue. Do not apologize for overriding their subjective feeling.
3. Deep Physiological Explanations: When explaining why something happens, use precise biological mechanisms (e.g., glycogen crash, cortisol rebound, parasympathetic nervous system suppression, intestinal transporter limits) instead of generic advice like "make sure you eat."
4. Handle Missing Data: If a question requires missing data (e.g., weight for macros, or pace for zones), refuse to hallucinate a generic answer. Explicitly ask the user for the exact missing metric, or provide a conditional formula ("If you weigh X, eat Y").
5. Non-judgmental Lifestyle Truth: State the biological effects of alcohol, late nights, etc., objectively and mathematically, without moralizing.
</persona>

<core_rules>
1. Use only the metrics, templates, planned sessions, profile, and history in the snapshot. If data is missing, say what is missing.
2. The user is in control of their schedule, but you are the absolute authority on whether that schedule is biologically viable.
3. Keep responses concise, specific, and conversational. Avoid generic filler.
4. Always respond in English. Keep calendar event names in their original language.
</core_rules>

<scheduling_json>
When a user-facing recommendation should be approval-ready, append exactly one JSON block at the end.

```json
{
  "action": "schedule_session",
  "title": "Upper Body",
  "activity_type": "strength_training",
  "program_session_id": 1,
  "target_date": "2026-07-03",
  "suggested_time": "18:00",
  "duration_min": 60,
  "intensity": "normal",
  "modifications": []
}
```

For an active program session, include its `program_session_id` and omit
`base_workout_id`; approval compiles that editable session directly to Garmin.
Before the JSON, show the complete exercise list with warm-up, working sets,
reps, weights or calibration, and rest. `target_date` is sent to Garmin; the
exact `suggested_time` remains in Telegram and the personal calendar.
</scheduling_json>
"""

CHAT_SYSTEM_PROMPT = """You are GarminCoach, a cold and concise AI assistant.
Use only facts supplied in the current snapshot. Never imply that you are human.
Do not create workout, readiness, recovery, scheduling, medical, or injury-risk
rules. Do not generate JSON or executable actions. Deterministic application
rules own all workout decisions and mutations. If the user asks for today's
workout recommendation, refer to the current morning decision. Explain factual
questions in at most three short paragraphs and name missing data. Never ask the
user to type weights, sets, repetitions, or post-workout RPE. Do not infer an
effect from age, sex, or another demographic unless the supplied facts include
an explicitly reviewed rule that models it. Always respond in English."""

def _is_error_response(text: str) -> bool:
    return text.startswith("Coach is currently") or text.startswith("Coach encountered")


def _format_sleep_minutes(total_s: float) -> str:
    minutes = int(round(total_s / 60.0))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _hrv_feedback(session: Session) -> str | None:
    """Simple verbal HRV feedback for morning briefings."""
    from time_utils import get_local_date

    today = get_local_date()
    today_health = session.get(DailyHealth, today)
    if not (today_health and today_health.hrv_overnight is not None):
        return None

    hrv = float(today_health.hrv_overnight)
    if today_health.hrv_baseline_low is not None and hrv < float(today_health.hrv_baseline_low):
        return "HRV is below your usual range"
    if today_health.hrv_baseline_high is not None and hrv > float(today_health.hrv_baseline_high):
        return "HRV is above your usual range"

    prev_health = (
        session.query(DailyHealth)
        .filter(DailyHealth.day < today)
        .filter(DailyHealth.hrv_overnight.isnot(None))
        .order_by(DailyHealth.day.desc())
        .first()
    )
    if prev_health and prev_health.hrv_overnight is not None:
        prev_hrv = float(prev_health.hrv_overnight)
        if abs(hrv - prev_hrv) <= 5:
            return "HRV looks stable"
        if hrv < prev_hrv:
            return "HRV is a bit lower than yesterday"
        return "HRV is a bit higher than yesterday"

    return "HRV has a usable overnight reading"


def _load_feedback(session: Session) -> str | None:
    """Simple verbal ACWR/load feedback for morning briefings."""
    from metrics.engine import acwr_label

    latest = (
        session.query(DailyMetrics)
        .filter(DailyMetrics.acwr.isnot(None))
        .order_by(DailyMetrics.day.desc())
        .first()
    )
    if not latest or latest.acwr is None:
        return None

    label = acwr_label(latest.acwr)
    if label == "underload":
        return "training load is on the low side"
    if label == "balanced":
        return "training load looks balanced"
    if label == "elevated":
        return "training load is elevated"
    if label:
        return "training load is spiking"
    return None


def _verbalize_morning_snapshot(snapshot_json: str, session: Session) -> str:
    """Remove exact HRV/ACWR numbers from the morning prompt payload."""
    try:
        snapshot = yaml.safe_load(snapshot_json) or {}
    except Exception:
        return snapshot_json

    if not isinstance(snapshot, dict):
        return snapshot_json

    daily = snapshot.get("daily_metrics")
    if isinstance(daily, dict):
        daily.pop("acute_load_7d", None)
        daily.pop("chronic_load_28d", None)
        daily.pop("acwr_ratio", None)
        daily.pop("acwr_3_day_trend", None)
        feedback = _load_feedback(session)
        if feedback:
            daily["load_feedback"] = feedback

    health = snapshot.get("latest_health")
    if isinstance(health, dict):
        health.pop("hrv_overnight", None)
        feedback = _hrv_feedback(session)
        if feedback:
            health["hrv_feedback"] = feedback

    trend = snapshot.get("health_trend_7_days")
    if isinstance(trend, list):
        for row in trend:
            if isinstance(row, dict):
                row.pop("hrv_overnight", None)
                row.pop("hrv_baseline_low", None)
                row.pop("hrv_baseline_high", None)

    snapshot["morning_briefing_style"] = (
        "For HRV and ACWR/load, use simple verbal feedback only. "
        "Do not include exact HRV milliseconds, ACWR ratios, acute/chronic load numbers, or threshold numbers."
    )
    return yaml.dump(snapshot, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _morning_short_sleep_opening(session: Session) -> str | None:
    """Stable factual lead-in for short-but-finalized sleep mornings."""
    from time_utils import get_local_date

    today = get_local_date()
    sleep = session.get(Sleep, today)
    if not (sleep and sleep.total_s and sleep.total_s > 0):
        return None

    if (sleep.total_s / 3600.0) >= 6.5:
        return None

    score_part = f", score {int(round(sleep.score))}" if sleep.score is not None else ""
    first = f"Short night - {_format_sleep_minutes(sleep.total_s)}{score_part}."

    details = []
    if sleep.deep_s and sleep.deep_s > 0:
        deep_h = sleep.deep_s / 3600.0
        quality = "excellent" if deep_h >= 1.5 else "solid" if deep_h >= 1.0 else "light"
        details.append(f"Deep sleep quality was {quality} ({deep_h:.1f}h)")

    today_health = session.get(DailyHealth, today)
    if today_health and today_health.hrv_overnight is not None:
        feedback = _hrv_feedback(session)
        if feedback:
            details.append(feedback)

    if not details:
        return first
    return first + " " + " and ".join(details) + "."


def telegram_workout_reply_markup(message_id: int) -> dict:
    """Inline actions for any approval-ready workout/session proposal."""
    return {
        "inline_keyboard": [
            [
                {"text": "Approve and schedule", "callback_data": f"approve_workout_{message_id}"},
                {"text": "Different time", "callback_data": f"reschedule_workout_{message_id}"},
            ],
            [
                {"text": "Dismiss", "callback_data": f"reject_workout_{message_id}"},
            ],
        ]
    }

def _extract_and_strip_json(text: str) -> tuple[str, str | None]:
    """Finds a ```json ... ``` block, parses it, and returns (stripped_text, json_str)."""
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    if match:
        json_str = match.group(1)
        stripped = text[:match.start()].strip() + "\n\n" + text[match.end():].strip()
    else:
        # Fallback: find the first { and last }
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = text[start:end+1]
            stripped = text.replace(json_str, "").replace("```json", "").replace("```", "").strip()
        else:
            return text, None
        
    try:
        json.loads(json_str)  # Verify validity
        return stripped.strip(), json_str
    except Exception as e:
        logger.error(f"Failed to parse intercepted JSON: {e}")
        return text, None


def _trim_history(history: list[dict], max_chars: int = _MAX_CHAT_HISTORY_CHARS) -> list[dict]:
    """Keep the newest chat turns under a compact character budget."""
    kept: list[dict] = []
    used = 0
    for item in reversed(history):
        content = str(item.get("content") or "")
        room = max_chars - used
        if room <= 0:
            break
        if len(content) > room:
            content = content[-room:]
        kept.append({"role": item.get("role"), "content": content})
        used += len(content)
    kept.reverse()
    return kept


def _generate_with_retry(system_prompt: str, user_prompt: str, history: list = None, session: Session = None, max_retries: int = 1) -> tuple[str, str | None]:
    for attempt in range(max_retries + 1):
        raw_response = llm.generate(system_prompt, user_prompt, history or [])
        chat_text, json_str = _extract_and_strip_json(raw_response)
        
        if not json_str:
            return chat_text, None
            
        try:
            payload = json.loads(json_str)
            if payload.get("action") in ("schedule_workout", "schedule_session"):
                parse_action(payload)
                from db import Workout
                base_workout_id = payload.get("base_workout_id")
                if base_workout_id and session.query(Workout).filter_by(workout_id=base_workout_id).first() is None:
                    raise ValueError(f"base_workout_id {base_workout_id} does not exist in available_garmin_templates.")
                return chat_text, json_str
        except Exception as e:
            if attempt < max_retries:
                logger.warning("LLM hallucinated invalid action, retrying: %s", e)
                error_feedback = f"\n\n[SYSTEM ERROR]: Your generated JSON action was invalid ({e}). Please try again."
                user_prompt += error_feedback
            else:
                logger.warning("Discarding invalid schedule_workout action after retries: %s", e)
                return chat_text, None
                
    return chat_text, None

def _legacy_generate_daily_suggestion(session: Session, *, allow_incomplete: bool = False) -> None:
    """Generate a daily proactive coaching suggestion if one doesn't exist for today."""
    
    # We generate a fresh suggestion every time this is called (both on the
    # automated 4am sync, and whenever the user clicks Manual Sync).
    # The dashboard always shows the most recent suggestion for today.

    from time_utils import get_local_now
    hour = get_local_now().hour
    is_evening = hour >= 17
    if not is_evening:
        from metrics.freshness import proactive_metrics_ready

        if not allow_incomplete and not proactive_metrics_ready(session):
            logger.info("Skipping morning briefing until today's sleep data is finalized.")
            return

    snapshot_json = build_snapshot(session)
    prompt_snapshot_json = snapshot_json if is_evening else _verbalize_morning_snapshot(snapshot_json, session)
    morning_opening = None if is_evening else _morning_short_sleep_opening(session)

    if is_evening:
        time_context = """
This is an EVENING CHECK-IN.
Your job is to decide whether tomorrow needs an actionable workout proposal.

If a useful workout proposal for tomorrow is NOT needed, output exactly:
NO_PUSH

Use NO_PUSH when tomorrow should simply be rest, a workout is already scheduled for tomorrow, data is too stale or incomplete to choose well, or there is no meaningful change/action for the user.

If a useful workout proposal IS needed, write exactly 2 short paragraphs in plain English:
1. Context sentence: Mention only the most relevant signals for tomorrow's plan (today's workout/load, readiness, sleep, HRV, ACWR/load, or tomorrow's calendar). Keep it simple and factual. Use numbers only when they make the recommendation clearer. Do not use jargon like "Zone 2". Do not mention a metric that is missing from the snapshot.
2. Proposal: State "Tomorrow's recommendation: [recovery / light / normal / hard session] [session name or activity] at [HH:MM]." Choose the time from tomorrow's calendar and the scheduling constraints.

Only explain "why" when it is directly supported by the snapshot. Do not say things like avoiding legs, workout fatigue, or poor recovery unless the relevant data is present.
CRITICAL: If you propose a workout, you MUST output the scheduling JSON block for tomorrow. If you do not output a valid scheduling JSON block, the evening check-in will not be sent.
"""
    else:
        fixed_opening_rule = ""
        if morning_opening:
            fixed_opening_rule = f"""
A fixed first sentence will be prepended by the app:
"{morning_opening}"
Do not repeat the sleep duration, sleep score, deep sleep detail, or HRV detail from that sentence. Use the body for readiness/load, the recommendation, the exact workout time, and today's calendar events.
"""
        time_context = """
This is a MORNING BRIEFING.
Write exactly 2 or 3 short paragraphs in plain English:
1. Metrics sentence: Mention each relevant metric at most once for today's decision (readiness, sleep debt, HRV, ACWR/load, or yesterday's workout). Keep it simple and factual. Do not use jargon like "Zone 2". Do not mention a metric that is missing from the snapshot. For HRV and ACWR/load, give simple verbal feedback only; never quote exact HRV milliseconds, ACWR ratios, acute/chronic load values, or threshold numbers. If no fixed first sentence is provided and sleep was short (<6.5h) but sleep score is fair-or-better and HRV is near recent values, the FIRST sentence must lead with the sleep context: "Short night - [duration], score [score]..." and include deep sleep if available plus verbal HRV stability. Say "not a sleep red flag" only when the short sleep is offset by stable HRV/sleep quality; still mention any separate load/readiness risk in the next sentence.
2. Recommendation: Give one clear recommendation for today. If a workout is already listed in `scheduled_workouts_NOT_completed` or `rolling_plan_14_days` for today, reference that session instead of recommending a new one. If training is recommended after short sleep, use a "go with a governor" call: train, but reduce volume or keep it light if the first working sets feel heavier than expected. If training is recommended, state the intensity as exactly one of: "recovery session", "light session", "normal session", or "hard session", and name the confirmed session/activity. If readiness is low but ACWR/load is on the low side, prefer active recovery or a light session over a full rest day unless sleep/HRV are clearly poor. If recovery is the right call, simply recommend resting and say no workout is needed. If HRV is notably unstable or has dropped, add a short, simple, actionable daily tip to aid recovery (e.g., "drink an extra glass of water today", "do 5 mins of deep breathing", "avoid heavy meals before bed").
3. Timing and calendar: If training is recommended, give one exact workout time in HH:MM based on today's calendar and scheduling constraints; do not give only a broad time window. Also state today's calendar events by name and time so the user does not need to check the calendar manually. If there are no calendar events today in the snapshot, say the calendar looks open. If a workout is already scheduled for today, use its scheduled time.

Only explain "why" when it is directly supported by the snapshot. Do not say things like avoiding legs, workout fatigue, or poor recovery unless the relevant data is present.
CRITICAL: If you recommend training today, you MUST output one valid scheduling JSON block for today with `target_date` set to today's date and `suggested_time` set to your exact recommended time. The user must still approve it before scheduling.
CRITICAL: Do not repeat the same sleep score, sleep duration, readiness score, or HRV/load feedback in multiple paragraphs.
""" + fixed_opening_rule

    prompt = f"""Generate the coaching message for the user.
Review the following metrics snapshot:
{prompt_snapshot_json}

{time_context}
Do NOT use markdown headers or greetings, just give the insight.
"""
    suggestion_text, json_str = _generate_with_retry(SYSTEM_PROMPT, prompt, session=session)
    if morning_opening and not suggestion_text.lower().startswith("short night"):
        suggestion_text = f"{morning_opening}\n\n{suggestion_text}"

    # Evening pushes should be actionable: either a schedulable workout proposal
    # for tomorrow, or silence. Morning remains the daily source of truth.
    if is_evening and (suggestion_text.strip().upper() == "NO_PUSH" or not json_str):
        logger.info("Skipping evening check-in because there is no actionable workout proposal.")
        return
    
    if _is_error_response(suggestion_text):
        from time_utils import get_local_date
        existing = session.query(CoachMessage).filter_by(role="suggestion").order_by(CoachMessage.created_at.desc()).first()
        if existing and existing.created_at and existing.created_at.date() == get_local_date() and not _is_error_response(existing.content):
            return  # Keep the existing valid suggestion for today
            
    from time_utils import get_local_now
    msg = CoachMessage(
        role="suggestion",
        content=suggestion_text,
        created_at=get_local_now(),
        data_snapshot=snapshot_json,
        pending_action_json=json_str
    )
    session.add(msg)
    session.commit()
    
    try:
        from notify.telegram import send_message
        now = get_local_now()
        is_morning = now.hour < 17
        greeting = "*Morning Briefing*" if is_morning else "*Evening Check-in*"
        
        # Check if we already generated a valid suggestion for this period today.
        # If so, we still save the fresh insight to the DB for the dashboard,
        # but we DO NOT spam the user's phone again.
        from time_utils import get_local_date
        today_date = get_local_date()
        recent = session.query(CoachMessage).filter_by(role="suggestion").order_by(CoachMessage.created_at.desc()).limit(10).all()
        
        already_pushed = False
        for s in recent:
            if s.id != msg.id and s.created_at and s.created_at.date() == today_date:
                if not _is_error_response(s.content):
                    was_morning = s.created_at.hour < 17
                    if was_morning == is_morning:
                        already_pushed = True
                        break
                        
        if not already_pushed:
            reply_markup = telegram_workout_reply_markup(msg.id) if msg.pending_action_json else None
            send_message(f"{greeting}\n\n{suggestion_text}", reply_markup=reply_markup)
    except Exception as e:
        import logging
        logging.error(f"Failed to send proactive Telegram notification: {e}")


def _decision_metric_line(result) -> str:
    parts = []
    if result.readiness_score is not None:
        parts.append(f"Garmin readiness {result.readiness_score} ({result.readiness_category})")
    values = {item["signal"]: item["value"] for item in result.observations}
    duration = values.get("sleep_duration_hours")
    sleep_score = values.get("sleep_score")
    if duration is not None:
        sleep_text = f"sleep {duration:g}h"
        if isinstance(sleep_score, dict):
            sleep_text += f", score {sleep_score['score']} ({sleep_score['category']})"
        parts.append(sleep_text)
    return "; ".join(parts) + ("." if parts else "")


def _render_typed_decision(result) -> str | None:
    metric_line = _decision_metric_line(result)
    if result.decision_type in {"WAITING_FOR_DATA", "SYNC_REQUIRED"}:
        return None
    if result.workout_outcome == "PROGRAM_REST_DAY":
        text = (
            f"Program rest day: {result.next_program_session_name} is next, "
            f"earliest {result.earliest_eligible_date}."
        )
        recovery = result.optional_recovery_activity
        if recovery:
            low, high = recovery["duration_min"]
            text += f" Optional: {low}-{high} minutes of easy walking at conversational effort."
        return text
    session_name = result.planned_session_name or result.next_program_session_name or "Workout"
    time_part = f" at {result.planned_start_time}" if result.planned_start_time else ""
    if result.decision_type == "ADVISE_SKIP_SESSION":
        return f"{metric_line}\nSkip {session_name}{time_part}. Garmin readiness is Poor. The original session remains available."
    if result.decision_type == "WARN_ORIGINAL_SESSION":
        return f"{metric_line}\n{session_name}{time_part} stays unchanged. Garmin readiness is Low; treat this as a warning, not a workout modification."
    if result.workout_outcome == "KEEP_PLANNED_SESSION":
        text = f"Planned: {session_name}{time_part}."
    elif result.workout_outcome == "PROPOSE_NEXT_SESSION":
        text = f"Today's program session: {session_name}."
    else:
        text = "No useful workout action is available today."
    if result.best_effort:
        omitted = ", ".join(
            item["signal"].replace("_", " ")
            for item in result.missing_observations if item["critical"]
        )
        text += f" Best effort; missing {omitted}."
    return f"{metric_line}\n{text}".strip()


def generate_daily_suggestion(session: Session, *, allow_incomplete: bool = False) -> None:
    """Evaluate and render the deterministic morning result; evening stays silent."""
    from time_utils import get_local_now
    if get_local_now().hour >= 17:
        logger.info("Evening workout proposals are disabled until next morning's overnight data.")
        return
    from coach.decision_engine import evaluate_morning_decision
    from coach.renderer import render_morning
    result = evaluate_morning_decision(session, allow_incomplete=allow_incomplete)
    text, reply_markup, interaction_ids = render_morning(session, result)
    if not text:
        return
    existing = (
        session.query(CoachMessage)
        .filter_by(role="suggestion")
        .order_by(CoachMessage.created_at.desc())
        .first()
    )
    today = get_local_now().date()
    if existing and existing.created_at and existing.created_at.date() == today:
        return
    msg = CoachMessage(
        role="suggestion",
        content=text,
        created_at=get_local_now(),
        data_snapshot=json.dumps(result.to_dict(), sort_keys=True),
        pending_action_json=(json.dumps({"interaction_ids": interaction_ids}) if interaction_ids else None),
    )
    session.add(msg)
    session.commit()
    try:
        from notify.telegram import send_message
        send_message(f"*Morning Briefing*\n\n{text}", reply_markup=reply_markup)
    except Exception:
        logger.exception("Failed to send deterministic morning briefing")


def handle_chat(session: Session, user_text: str) -> str:
    """Handle an interactive chat message from the user."""
    
    snapshot_json = build_snapshot(session)

    from coach.interactions import stage_free_text_change
    staged_change = stage_free_text_change(session, user_text)
    if staged_change is not None:
        response_text, interactions = staged_change
        user_msg = CoachMessage(role="user", content=user_text, created_at=datetime.now(timezone.utc))
        asst_msg = CoachMessage(
            role="assistant",
            content=response_text,
            created_at=datetime.now(timezone.utc),
            data_snapshot=snapshot_json,
            pending_action_json=(
                json.dumps({"interaction_ids": [item.interaction_id for item in interactions]})
                if interactions else None
            ),
        )
        session.add_all((user_msg, asst_msg))
        session.commit()
        return response_text, asst_msg
    
    # Load recent conversation history (last 10 messages, excluding daily suggestions)
    recent_msgs = session.query(CoachMessage).filter(
        CoachMessage.role.in_(["user", "assistant"])
    ).order_by(CoachMessage.created_at.desc()).limit(10).all()
    
    recent_msgs.reverse() # chronological order
    
    history = []
    for m in recent_msgs:
        history.append({"role": m.role, "content": m.content})
    history = _trim_history(history)
        
    # Inject the snapshot into the current user prompt invisibly
    prompt_with_context = f"""[SYSTEM: Current Data Snapshot]
{snapshot_json}
[END SYSTEM DATA]

User Message: {user_text}"""

    # Save user message
    user_msg = CoachMessage(
        role="user",
        content=user_text,
        created_at=datetime.now(timezone.utc)
    )
    session.add(user_msg)
    
    # Informational generation only. Any JSON/action-like output is discarded.
    chat_text = llm.generate(CHAT_SYSTEM_PROMPT, prompt_with_context, history)
    chat_text, _discarded_action = _extract_and_strip_json(chat_text)
    
    # Save assistant message
    asst_msg = CoachMessage(
        role="assistant",
        content=chat_text,
        created_at=datetime.now(timezone.utc),
        data_snapshot=snapshot_json,
        pending_action_json=None
    )
    session.add(asst_msg)
    session.commit()
    
    return chat_text, asst_msg
