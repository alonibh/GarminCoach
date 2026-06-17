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
5. Pay special attention to Readiness and ACWR (Acute:Chronic Workload Ratio). 
   - Readiness < 40 means prioritize recovery.
   - ACWR > 1.5 means high injury risk (spiking load).
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
Point out any alarming trends (e.g. ACWR spike, sleep debt) or give a green light if things look great.
Review the user's `today_schedule`. Suggest an exact optimal time window for today's workout based on their free time and their `readiness`/`acwr` status. If their schedule is completely packed and their readiness is low, suggest a rest day.
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
    response_text = llm.generate(SYSTEM_PROMPT, prompt_with_context, history)
    
    # Save assistant message
    asst_msg = CoachMessage(
        role="assistant",
        content=response_text,
        created_at=datetime.now(),
        data_snapshot=snapshot_json
    )
    session.add(asst_msg)
    session.commit()
    
    return response_text
