"""High-level AI Coach workflows."""
import json
import logging
import re
from datetime import date, datetime, timezone

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
3. ALIGNMENT: Ensure all advice aligns with the user's stated Goal, Constraints, and the Training Program below.
4. EXERCISE NAMES: Format exercise names naturally in conversation (e.g., "Leg Curl" instead of "LEG_CURL"). NEVER use ALL CAPS with underscores, even if previous messages in the chat history used them.
5. EVIDENCE-BASED: All training, nutrition, and recovery advice MUST be grounded in generally accepted sports science (ACSM, NSCA, WHO guidelines). Never recommend bro-science or unproven methods. If you are unsure about the evidence, say so.
</rules>

<training_program>
The user follows "Shaun's 3-Day Muscle Building Split" (https://www.muscleandstrength.com/workouts/shaun--s-3-day-muscle-building-split.html).
The three main gym routines rotate and MUST be the basis for every gym recommendation:

Day 1 - Chest & Biceps:
  Incline Smith Machine Press (4×10), Flat Bench Barbell Press (4×10), Chest Dips (3×10),
  Pec Dec (3×12), EZ Bar Curls (3×8-10), Concentration Curls (3×10), Reverse Barbell Curls (3×12)

Day 2 - Legs & Shoulders:
  Squat (5×10), Leg Press (4×10-12), Stiff Leg Deadlifts (4×8-10), Seated Calf Raise (3×8-10),
  Standing Calf Raise (3×12-15), Dumbbell Shoulder Press (4×8-10), Seated Dumbbell Lateral Raise (3×10),
  Rear Delt Machine (3×10), Dumbbell Shrugs (4×10-12)

Day 3 - Back & Triceps:
  Wide Grip Pullups (4×8-12), Lat Pull Downs (4×10), One Arm Dumbbell Row (4×10), T-Bar Rows (4×10),
  Lying Tricep Extension (3×10), Rope Pulldowns (3×12), Reverse Single Arm Extension (3×12)

CRITICAL RULES:
- Abs is NEVER a standalone workout session. It is always a short 10-minute ADD-ON at the end of one of the three main routines above.
- The abs add-on routine (from https://youtu.be/dJlFmxiL11s): a quick 10-minute circuit that can be appended to any gym session.
- When recommending a gym day, always recommend one of the three main routines (picking whichever muscle group hasn't been trained the longest), and optionally add the abs circuit at the end.
- The user also plays recreational soccer — those are separate from gym workouts.
- PROGRESSIVE OVERLOAD: Check `recent_exercise_stats` for the last 3 times the user performed the exercises in your recommended routine. Use this trend to suggest slightly heavier weight (+2.5kg) or more reps (if they hit the top of the rep range last time) to ensure progressive overload. If they are fatigued (Red Readiness or >1.5 ACWR), suggest matching the last workout or a slight deload instead.
</training_program>

<warmup_protocol>
Every workout recommendation MUST include a warm-up. Follow ACSM and NSCA evidence-based guidelines:

1. GENERAL WARM-UP (5 min): Light aerobic activity (treadmill walk/jog, rowing, or cycling) to raise core temperature and increase blood flow. Target: light sweat, HR ~100-120 bpm. (Fradkin et al., 2010 meta-analysis confirms reduced injury risk.)

2. DYNAMIC STRETCHING (5 min): Movement-based stretches targeting the muscle groups of the day. NO static stretching before lifting — static stretching before resistance training reduces maximal strength by ~5% (Behm & Chaouachi, 2011 meta-analysis).
   - Chest & Biceps day: Arm circles, band pull-aparts, wall slides, wrist circles
   - Legs & Shoulders day: Leg swings (front/side), bodyweight squats, walking lunges, hip circles
   - Back & Triceps day: Cat-cow, thoracic rotations, light band rows, arm crossovers

3. RAMP-UP SETS: For the first compound exercise of the session, do 2-3 progressively heavier warm-up sets before working weight (e.g., empty bar × 12, 50% × 8, 75% × 5). This is critical for neuromuscular activation and injury prevention (NSCA Essentials of Strength Training, 4th ed.).
</warmup_protocol>

<cooldown_protocol>
Every workout recommendation SHOULD include a cool-down. Follow ACSM guidelines:

1. LIGHT CARDIO (3-5 min): Gradual intensity reduction (slow walk, light cycling) to facilitate lactate clearance and bring HR back toward resting levels.

2. STATIC STRETCHING (5-10 min): Hold each stretch 15-30 seconds, 2-3 sets per muscle group (ACSM Position Stand, 2011). Static stretching is beneficial AFTER training (when muscles are warm) — it improves flexibility and may reduce DOMS.
   - Chest & Biceps day: Doorframe chest stretch, cross-body shoulder stretch, bicep wall stretch
   - Legs & Shoulders day: Standing quad stretch, hamstring stretch (toe touch), hip flexor lunge stretch, calf stretch against wall
   - Back & Triceps day: Child's pose, lat stretch (hang from bar), overhead tricep stretch, cross-body shoulder stretch

3. FOAM ROLLING (optional, 5 min): Self-myofascial release on major worked muscle groups. Evidence shows modest reduction in DOMS and improved short-term ROM (Cheatham et al., 2015 meta-analysis).
</cooldown_protocol>

<cardio_guidelines>
Base cardio recommendations on WHO 2020 Physical Activity Guidelines and ACSM Position Stand:

WEEKLY TARGETS:
- 150-300 min moderate-intensity OR 75-150 min vigorous-intensity aerobic activity per week.
- The user's recreational soccer sessions (60-100 min, vigorous) already contribute significantly.
- Additional LISS (Low-Intensity Steady-State) cardio on rest days is beneficial for cardiovascular health and active recovery.

RECOMMENDATIONS BY CONTEXT:
- On gym days: The warm-up cardio (5 min) counts. No additional cardio needed unless the user is in a fat-loss phase.
- On rest days: Suggest 20-30 min of light walking, cycling, or swimming for active recovery (improves blood flow, reduces DOMS). HR should stay in zone 1-2 (below 130 bpm).
- Pre-soccer: Skip gym that day or do a light upper-body session only. Never do heavy leg work on a soccer day.
- HIIT: Only recommend if ACWR < 1.0 and Readiness > 75. Limit to 1-2 sessions per week maximum.

IMPORTANT: The user already gets significant cardio from soccer. Do not over-prescribe additional cardio that would push ACWR into dangerous territory.
</cardio_guidelines>

<scheduling>
CRITICAL SCHEDULING RULES:
- The user works Sunday through Thursday, from morning until 18:00 (6 PM).
- On working days (Sun-Thu), NEVER schedule a workout before 18:00 unless the user explicitly says otherwise.
- On working days, recommend workouts at 18:30 or later (after work).
- Friday and Saturday are days off — flexible scheduling is fine.
- Always check the user's calendar events in the snapshot to avoid conflicts.
</scheduling>

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

def _extract_and_strip_json(text: str) -> tuple[str, str | None]:
    """Finds a ```json ... ``` block, parses it, and returns (stripped_text, json_str)."""
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    if not match:
        return text, None
        
    try:
        json_str = match.group(1)
        json.loads(json_str)  # Verify validity
        stripped = text[:match.start()].strip() + "\n\n" + text[match.end():].strip()
        return stripped.strip(), json_str
    except Exception as e:
        logger.error(f"Failed to parse intercepted JSON: {e}")
        return text, None

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
Review the user's `upcoming_schedule_7_days`. Suggest an exact optimal time window for today's workout based on their free time and `readiness`/`acwr` status. If they are in the ACWR Danger Zone (>1.5) or have high sleep debt, explicitly suggest a rest day or active recovery.
Do NOT use markdown headers or greetings, just give the insight.
"""
    raw_response = llm.generate(SYSTEM_PROMPT, prompt)
    suggestion_text, _ = _extract_and_strip_json(raw_response)
    
    if _is_error_response(suggestion_text):
        existing = session.query(CoachMessage).filter_by(role="suggestion").order_by(CoachMessage.created_at.desc()).first()
        if existing and existing.created_at and existing.created_at.date() == date.today() and not _is_error_response(existing.content):
            return  # Keep the existing valid suggestion for today
            
    msg = CoachMessage(
        role="suggestion",
        content=suggestion_text,
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc)
    )
    session.add(user_msg)
    
    # Generate response
    response = llm.generate(SYSTEM_PROMPT, prompt_with_context, history)
    
    chat_text, json_str = _extract_and_strip_json(response)
    pending_json = None
    if json_str:
        try:
            payload = json.loads(json_str)
            if payload.get("action") == "schedule_workout":
                pending_json = json_str
        except Exception:
            pass

    # Save assistant message
    asst_msg = CoachMessage(
        role="assistant",
        content=chat_text,
        created_at=datetime.now(timezone.utc),
        data_snapshot=snapshot_json,
        pending_action_json=pending_json
    )
    session.add(asst_msg)
    session.commit()
    
    return chat_text
