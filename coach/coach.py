"""High-level AI Coach workflows."""
import json
import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from db import CoachMessage
from coach import llm
from coach.snapshot import build_snapshot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the GarminCoach AI, a world-class, data-driven personal trainer.
Your job is to analyze the user's Garmin metrics and provide proactive, personalized, and actionable advice.

<rules>
1. NO HALLUCINATIONS: ONLY use the exact metrics provided in the data snapshot. If data is missing, honestly state that you don't have it.
2. TONE: Be concise, encouraging, and highly specific to the numbers. Do not use generic AI filler like "Based on the data you provided...".
3. ALIGNMENT: Ensure all advice aligns with the user's stated Goal and Constraints.
4. EXERCISE NAMES: Format exercise names naturally in conversation (e.g., "Leg Curl" instead of "LEG_CURL"). NEVER use ALL CAPS with underscores, even if previous messages in the chat history used them.
</rules>

<metric_thresholds>
Pay special attention to these critical fatigue markers:
- ACWR: <0.8 Detraining | 0.8-1.3 Optimal | 1.3-1.5 Ramping (caution) | >1.5 Danger Zone (high injury risk).
- Sleep Debt: > 5.0 hours of accumulated exponential debt requires immediate correction (nap/early bedtime).
- Readiness (0-100): < 60 prioritize recovery | > 85 prime condition to push hard.
</metric_thresholds>

<workout_modifications>
When the user asks to modify a workout, you MUST holistically balance their progressive overload history against their current fatigue:
1. Use `recent_exercise_stats` to apply progressive overload (increase weight/reps from their last baseline).
2. Evaluate systemic fatigue (Readiness, ACWR, Sleep) and metabolic fatigue (`recent_workouts`). 
3. If they have high fatigue or recent heavy activity, REDUCE volume/intensity, even if their history suggests an increase.
4. Explicitly output the modified routine in the chat.
</workout_modifications>

<interactive_ui>
To automatically push a workout to their watch, append a JSON block formatted EXACTLY like the example below at the absolute end of your response.
   - `base_workout_id` MUST be an exact ID from `user_saved_workouts`.
   - `suggested_time` MUST be an exact HH:MM (24-hour) time you recommend for the workout today.
   - Omitted indices are deleted. `new_sets`, `new_reps`, `new_weight_kg` are optional (keeps original if omitted).
</interactive_ui>

<json_format_example>
```json
{
  "action": "schedule_workout",
  "base_workout_id": 12345,
  "suggested_time": "18:00",
  "modifications": [
    { "type": "keep_and_modify", "index": 0, "new_sets": 2 },
    { "type": "add_new", "description": "Spiderman Pushups", "sets": 3, "reps": 10, "weight_kg": 0 }
  ]
}
```
</json_format_example>
"""

def _is_error_response(text: str) -> bool:
    return text.startswith("Coach is currently") or text.startswith("Coach encountered")



def generate_daily_suggestion(session: Session) -> None:
    """Generate a daily proactive coaching suggestion if one doesn't exist for today."""
    
    # We generate a fresh suggestion every time this is called (both on the
    # automated 4am sync, and whenever the user clicks Manual Sync).
    # The dashboard always shows the most recent suggestion for today.
        
    snapshot_json = build_snapshot(session)
    
    prompt = f"""Generate today's daily coaching suggestion.
Review the following metrics snapshot:
{snapshot_json}

Provide exactly 1-2 short, punchy paragraphs. 
Analyze their exponential sleep debt and EWMA ACWR. Point out any alarming trends or give a green light if their Readiness is primed.
Review the user's `today_schedule`. Suggest an exact optimal time window for today's workout based on their free time and `readiness`/`acwr` status. If they are in the ACWR Danger Zone (>1.5) or have high sleep debt, explicitly suggest a rest day or active recovery.
Do NOT use markdown headers or greetings, just give the insight.
"""
    
    suggestion_text = llm.generate(SYSTEM_PROMPT, prompt)
    
    if _is_error_response(suggestion_text):
        existing = session.query(CoachMessage).filter_by(role="suggestion").order_by(CoachMessage.created_at.desc()).first()
        if existing and existing.created_at and existing.created_at.date() == date.today() and not _is_error_response(existing.content):
            return  # Keep the existing valid suggestion for today
            
    msg = CoachMessage(
        role="suggestion",
        content=suggestion_text,
        created_at=datetime.now(),
        data_snapshot=snapshot_json
    )
    session.add(msg)
    session.commit()

def generate_nutrition_suggestion(session: Session) -> None:
    """Generate daily dietary recommendations and macro targets."""
    snapshot_json = build_snapshot(session)
    
    prompt = f"""Generate today's daily nutrition coach recommendation.
Review the following metrics snapshot:
{snapshot_json}

Provide exactly 1 short paragraph. 
Recommend daily macro targets (Protein/Carbs/Fat in grams or percentages) based on today's calorie burn (`total_kcal` and `active_kcal`) and workouts.
Also suggest a healthy, actionable post-workout meal idea or a rest-day meal idea depending on the day's activity level.
Do NOT use markdown headers or greetings, just give the insight.
"""
    
    suggestion_text = llm.generate(SYSTEM_PROMPT, prompt)
    
    if _is_error_response(suggestion_text):
        existing = session.query(CoachMessage).filter_by(role="nutrition").order_by(CoachMessage.created_at.desc()).first()
        if existing and existing.created_at and existing.created_at.date() == date.today() and not _is_error_response(existing.content):
            return  # Keep the existing valid nutrition for today
            
    msg = CoachMessage(
        role="nutrition",
        content=suggestion_text,
        created_at=datetime.now(),
        data_snapshot=snapshot_json
    )
    session.add(msg)
    session.commit()


def handle_chat(session: Session, user_text: str) -> str:
    """Handle an interactive chat message from the user."""
    
    snapshot_json = build_snapshot(session)
    
    # Load recent conversation history (last 10 messages, excluding daily suggestions)
    recent_msgs = session.query(CoachMessage).filter(
        CoachMessage.role.in_(["user", "assistant"])
    ).order_by(CoachMessage.created_at.desc()).limit(10).all()
    
    recent_msgs.reverse() # chronological order
    
    history = []
    for m in recent_msgs:
        history.append({"role": m.role, "content": m.content})
        
    # Inject the snapshot into the current user prompt invisibly
    prompt_with_context = f"""[SYSTEM: Current Data Snapshot]
{snapshot_json}
[END SYSTEM DATA]

User Message: {user_text}"""

    # Save user message
    user_msg = CoachMessage(
        role="user",
        content=user_text,
        created_at=datetime.now()
    )
    session.add(user_msg)
    
    # Generate response
    response = llm.generate(SYSTEM_PROMPT, prompt_with_context, history)
    
    chat_text = response
    pending_json = None
    try:
        if "```json\n{" in response and "}\n```" in response:
            start = response.rfind("```json\n{") + 8
            end = response.rfind("}\n```") + 1
            json_str = response[start:end]
            payload = json.loads(json_str)
            
            if payload.get("action") == "schedule_workout":
                # STAGE the payload, do NOT execute it!
                pending_json = json_str
                chat_text = response[:response.rfind("```json\n{")].strip()
    except Exception as e:
        logger.error(f"Failed to intercept workout JSON: {e}")

    # Save assistant message
    asst_msg = CoachMessage(
        role="assistant",
        content=chat_text,
        created_at=datetime.now(),
        data_snapshot=snapshot_json,
        pending_action_json=pending_json
    )
    session.add(asst_msg)
    session.commit()
    
    return chat_text
