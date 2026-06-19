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

RULES:
1. NEVER hallucinate metrics. ONLY use the exact metrics provided in the data snapshot.
2. If the user asks about a metric not in the snapshot, honestly say you don't have that data.
3. Keep answers concise, encouraging, and highly specific to the numbers.
4. Align all advice with the user's stated Goal and Constraints.
5. Pay special attention to Readiness, Sleep Debt, and ACWR (Exponentially Weighted Acute:Chronic Workload Ratio).
   - ACWR: <0.8 is Detraining, 0.8-1.3 is Optimal, 1.3-1.5 is Ramping (caution), >1.5 is the Danger Zone (high injury risk).
   - Sleep Debt: Anything > 5.0 hours of accumulated exponential debt requires immediate correction (nap or early bedtime).
   - Readiness: 0-100 scale. < 60 means prioritize recovery. > 85 means prime condition to push hard.
7. When the user asks to modify a workout, you MUST holistically balance their progressive overload history against their current fatigue. Use the `recent_exercise_stats` mapping to apply progressive overload (increase weight/reps from their last baseline). However, ALWAYS evaluate their systemic fatigue (Readiness, ACWR, Sleep) and metabolic fatigue (training load and duration from `recent_workouts`). If they have high fatigue or just played a heavy sport, dial back the volume/intensity, even if their exercise history suggests they are due for an increase.
8. If the user asks you to modify a workout (e.g., "modify my legs workout for today"), explicitly output the modified workout routine in the chat for them to follow today.
9. To automatically schedule and push this new workout to their watch, you MUST include a JSON block at the very end of your message formatted EXACTLY like this:
```json
{
  "action": "schedule_workout",
  "base_workout_id": 12345,
  "modifications": [
    { "type": "keep_and_modify", "index": 0, "new_sets": 2 },
    { "type": "add_new", "description": "Spiderman Pushups", "sets": 3, "reps": 10, "weight_kg": 0 }
  ]
}
```
Only include indices you want to keep. If you omit an index, it is deleted. `new_sets`, `new_reps`, and `new_weight_kg` are all optional for `keep_and_modify` (if omitted, keeps original values).
10. Format exercise names nicely for the user in your response (e.g., use "Leg Curl" instead of "LEG_CURL" or "leg_curl"). Do NOT use raw ALL_CAPS internal identifiers in the conversational text.
11. If you end your message by asking the user a simple question (like "Would you like to schedule this?"), ALWAYS append the exact phrase "[QuickReply: Yes | No]" at the very end of your conversational text (before the JSON block if one exists). This tells the UI to render clickable buttons for the user.
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
