"""High-level AI Coach workflows."""
import json
import logging
import re
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from db import CoachMessage
from coach import llm
from coach.actions import parse_action
from coach.snapshot import build_snapshot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the GarminCoach AI, a world-class, data-driven personal trainer.
Your job is to analyze the user's Garmin metrics and provide proactive, personalized, and actionable advice.

<rules>
1. NO HALLUCINATIONS: ONLY use the exact metrics provided in the data snapshot. Do not infer completed workouts from chat history (a scheduled workout doesn't mean it was completed). Always trust the workout_history_log JSON over previous messages. If data is missing, honestly state that you don't have it.
2. TONE: Be concise, encouraging, and highly specific to the numbers. Do not use generic AI filler like "Based on the data you provided...".
3. ALIGNMENT: Ensure all advice aligns with the user's stated Goal, Constraints, and the Training Program below.
4. EXERCISE NAMES: Format exercise names naturally in conversation (e.g., "Leg Curl" instead of "LEG_CURL"). NEVER use ALL CAPS with underscores, even if previous messages in the chat history used them.
5. EVIDENCE-BASED: All training, nutrition, and recovery advice MUST be grounded in generally accepted sports science (ACSM, NSCA, WHO guidelines). Never recommend bro-science or unproven methods. If you are unsure about the evidence, say so.
6. FORMATTING: Keep your answers simple, direct, and conversational. 
   - Only answer exactly what the user explicitly asks for. 
   - Do NOT proactively summarize readiness, sleep debt, calendar events, or suggest a new workout UNLESS the user explicitly asks for a workout suggestion or a summary of their metrics.
   - If the user asks a simple question (e.g., 'Do I have any scheduled workouts?'), answer it in one brief sentence.
7. WORKOUT SUGGESTIONS: Only when the user explicitly asks for a new workout suggestion or when it is a natural continuation of the conversation, format your response in short paragraphs: 
   - Briefly summarize Readiness/ACWR/sleep.
   - Mention conflicting calendar events and suggest a time.
   - State the routine you recommend.
   - (Optional) Briefly explain progressive overload choices.
8. ACTIONABLE: If you recommend a workout or a specific time, you MUST append the scheduling JSON block.
9. LANGUAGE: Always respond in English. However, when mentioning calendar events with Hebrew names, use the original Hebrew names as-is without translating them.
</rules>

<training_program>
The user follows a modified "Shaun's 3-Day Muscle Building Split".
The three main gym routines rotate and MUST be the basis for every gym recommendation.
Below is the EXACT structure of the base templates. The numbers (0:, 1:, etc.) correspond to the step index.
When suggesting modifications for progressive overload, you MUST use the correct index for the working sets (do NOT modify the warm-up sets).

Day 1 - Chest & Biceps:
  0: Incline Smith Machine Bench Press (1×8 warm-up)
  1: Incline Smith Machine Bench Press (4×10)
  2: Barbell Bench Press (4×10)
  3: Triceps Extension (3×10)
  4: Flye (3×12)
  5: Curl (1×8 warm-up)
  6: Curl (3×10)
  7: One Arm Concentration Curl (3×10)
  8: Reverse Grip Barbell Biceps Curl (3×12)

Day 2 - Legs & Shoulders:
  0: Weighted Squat (1×8 warm-up)
  1: Weighted Squat (5×10)
  2: Leg Press (4×12)
  3: Leg Curl (1×8 warm-up)
  4: Leg Curl (4×10)
  5: Weighted Seated Calf Raise (3×10)
  6: Standing Calf Raise (3×12)
  7: Dumbbell Shoulder Press (1×8 warm-up)
  8: Dumbbell Shoulder Press (4×10)
  9: Seated Lateral Raise (3×10)
  10: Seated Rear Lateral Raise (3×10)
  11: Dumbbell Shrug (4×12)

Day 3 - Back & Triceps:
  0: Lat Pulldown (1×8 warm-up)
  1: Wide Grip Pull Up (4×12)
  2: Lat Pulldown (4×10)
  3: Single Arm Neutral Grip Dumbbell Row (4×10)
  4: T Bar Row (4×10)
  5: Dumbbell Lying Triceps Extension (1×8 warm-up)
  6: Dumbbell Lying Triceps Extension (3×10)
  7: Rope Pressdown (3×12)
  8: Reverse Grip Triceps Pressdown (3×12)

CRITICAL RULES:
- NEVER recommend an Abs workout on its own, NO EXCEPTIONS. Abs is strictly a 10-minute ADD-ON at the end of Day 1, Day 2, or Day 3. 
- If the user is too fatigued or doesn't have time for a main routine, DO NOT recommend just Abs. Recommend a rest day or light cardio instead.
- The Abs add-on is Pamela Reif's 10-minute bodyweight circuit (https://youtu.be/dJlFmxiL11s) consisting of continuous floor exercises (Crunches, Leg Raises, Russian Twists, Plank, etc.). Do not include equipment exercises like cable crunches or hanging knee raises in the abs add-on.
- When recommending a gym day, always recommend one of the three main routines (picking whichever muscle group hasn't been trained the longest according to `workout_history_log`), and optionally add the abs circuit at the end. If `workout_history_log` is missing or empty, DO NOT invent or hallucinate past dates; just recommend Day 1 (Chest & Biceps) to start the cycle.
- The user also plays recreational soccer — those are separate from gym workouts.
- PROGRESSIVE OVERLOAD: Check `recent_exercise_stats` for the last 3 times the user performed the exercises in your recommended routine. Use this trend to suggest slightly heavier weight (+2.5kg) or more reps (if they hit the top of the rep range last time) to ensure progressive overload. If they are fatigued (Red Readiness or >1.5 ACWR), suggest matching the last workout or a slight deload instead. Do NOT explicitly list out the user's "last recorded" history in your response text; just use it internally to compute the new target.
- EXACT TARGETS: When building the JSON payload, you MUST give one exact, definitive target for sets, reps, and weight.
</training_program>

<warmup_protocol>
Every workout recommendation MUST include a warm-up in the JSON block, but DO NOT mention it in the text response. Follow ACSM and NSCA evidence-based guidelines:

1. GENERAL WARM-UP (5 min): Light aerobic activity (treadmill walk/jog, rowing, or cycling) to raise core temperature and increase blood flow. Target: light sweat, HR ~100-120 bpm.

2. DYNAMIC STRETCHING (5 min): Movement-based stretches targeting the muscle groups of the day. NO static stretching before lifting.
   - Chest & Biceps day: Arm circles, band pull-aparts, wall slides, wrist circles
   - Legs & Shoulders day: Leg swings (front/side), bodyweight squats, walking lunges, hip circles
   - Back & Triceps day: Cat-cow, thoracic rotations, light band rows, arm crossovers
</warmup_protocol>

<cooldown_protocol>
Every workout recommendation SHOULD include a cool-down in the JSON block, but DO NOT mention it in the text response. Follow ACSM guidelines:

1. LIGHT CARDIO (3-5 min): Gradual intensity reduction (slow walk, light cycling) to facilitate lactate clearance and bring HR back toward resting levels.

2. STATIC STRETCHING (5-10 min): Hold each stretch 15-30 seconds, 2-3 sets per muscle group. Static stretching is beneficial AFTER training (when muscles are warm) — it improves flexibility and may reduce DOMS.
   - Chest & Biceps day: Doorframe chest stretch, cross-body shoulder stretch, bicep wall stretch
   - Legs & Shoulders day: Standing quad stretch, hamstring stretch (toe touch), hip flexor lunge stretch, calf stretch against wall
   - Back & Triceps day: Child's pose, lat stretch (hang from bar), overhead tricep stretch, cross-body shoulder stretch

3. FOAM ROLLING (optional, 5 min): Self-myofascial release on major worked muscle groups.
</cooldown_protocol>

<cardio_guidelines>
RECOMMENDATIONS BY CONTEXT:
- On gym days: The warm-up cardio (5 min) counts. No additional cardio needed unless the user is in a fat-loss phase.
- On rest days: Suggest 20-30 min of light walking, cycling, or swimming for active recovery (improves blood flow, reduces DOMS). HR should stay in zone 1-2 (below 130 bpm).
- Pre-soccer: Skip gym that day or do a light upper-body session only. Never do heavy leg work on a soccer day.
- HIIT: Only recommend if ACWR < 1.0 and Readiness > 75. Limit to 1-2 sessions per week maximum.
- IMPORTANT: The user already gets significant cardio from soccer. Do not over-prescribe additional cardio that would push ACWR into dangerous territory.
</cardio_guidelines>

<scheduling>
CRITICAL SCHEDULING RULES:
- The user works Sunday through Thursday, from morning until 17:30 (5:30 PM).
- On working days (Sun-Thu), NEVER schedule a workout before 17:30 unless the user explicitly says otherwise.
- On working days, recommend workouts at 18:00 or later (after work).
- Friday and Saturday are days off — flexible scheduling is fine.
- Always check the user's calendar events in the snapshot to avoid conflicts.
- IMPORTANT: Carefully distinguish between calendar events where the user is physically PLAYING sports (e.g., 'playing soccer') vs. WATCHING sports on TV. Any event formatted as "Team A Vs Team B" (e.g., "פורטוגל Vs אוזבקיסטן", "הפועל תל אביב Vs מכבי תל אביב") is a televised professional match. Watching matches occupies time but does NOT cause physical fatigue, so it MUST NOT prevent a heavy gym session beforehand.
</scheduling>

<metric_thresholds>
Pay special attention to these critical fatigue markers:
- ACWR: <0.8 Detraining | 0.8-1.3 Optimal | 1.3-1.5 Ramping (caution) | >1.5 Danger Zone (high injury risk).
- Sleep Debt: > 5.0 hours of accumulated exponential debt requires immediate correction (nap/early bedtime).
- Readiness (0-100): < 60 prioritize recovery | > 85 prime condition to push hard.
</metric_thresholds>

<workout_modifications>
When modifying a workout:
1. Apply progressive overload checking `recent_exercise_stats`.
2. Check `recent_workouts` for metabolic fatigue (did they do heavy squats yesterday?) even if systemic Readiness is good. Reduce intensity if fatigued.
</workout_modifications>

<scheduling_json>
To automatically push a workout to their watch, append a JSON block formatted EXACTLY like the example below at the absolute end of your response.
   - `base_workout_id` MUST be an exact ID from `available_routines`.
   - `suggested_time` MUST be an exact HH:MM (24-hour) time you recommend for the workout today.
   - ALWAYS include warm-up set indices as `keep_and_modify` with no other fields (this preserves them). Only add `new_sets`, `new_reps`, or `new_weight_kg` to working-set indices. Omitted indices are deleted.

```json
{
  "action": "schedule_workout",
  "base_workout_id": 12345,
  "suggested_time": "18:00",
  "modifications": [
    { "type": "keep_and_modify", "index": 0 },
    { "type": "keep_and_modify", "index": 1, "new_sets": 2, "new_weight_kg": 15 },
    { "type": "add_new", "description": "Spiderman Pushups", "sets": 3, "reps": 10, "weight_kg": 0 }
  ]
}
```
</scheduling_json>
"""

def _is_error_response(text: str) -> bool:
    return text.startswith("Coach is currently") or text.startswith("Coach encountered")

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

def _generate_with_retry(system_prompt: str, user_prompt: str, history: list = None, session: Session = None, max_retries: int = 1) -> tuple[str, str | None]:
    for attempt in range(max_retries + 1):
        raw_response = llm.generate(system_prompt, user_prompt, history or [])
        chat_text, json_str = _extract_and_strip_json(raw_response)
        
        if not json_str:
            return chat_text, None
            
        try:
            payload = json.loads(json_str)
            if payload.get("action") == "schedule_workout":
                parse_action(payload)
                from db import Workout
                if session.query(Workout).filter_by(workout_id=payload.get("base_workout_id")).first() is None:
                    raise ValueError(f"base_workout_id {payload.get('base_workout_id')} does not exist in available_routines.")
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

def generate_daily_suggestion(session: Session) -> None:
    """Generate a daily proactive coaching suggestion if one doesn't exist for today."""
    
    # We generate a fresh suggestion every time this is called (both on the
    # automated 4am sync, and whenever the user clicks Manual Sync).
    # The dashboard always shows the most recent suggestion for today.
        
    snapshot_json = build_snapshot(session)
    
    from time_utils import get_local_now
    hour = get_local_now().hour
    is_evening = hour >= 17

    if is_evening:
        time_context = """
This is an EVENING CHECK-IN.
Your job is to decide whether tomorrow needs an actionable workout proposal.

If a useful workout proposal for tomorrow is NOT needed, output exactly:
NO_PUSH

Use NO_PUSH when tomorrow should simply be rest, a workout is already scheduled for tomorrow, data is too stale or incomplete to choose well, or there is no meaningful change/action for the user.

If a useful workout proposal IS needed, write exactly 2 short paragraphs in plain English:
1. Context sentence: Mention only the most relevant signals for tomorrow's plan (today's workout/load, readiness, sleep, HRV, ACWR/load, or tomorrow's calendar). Keep it simple and factual. Use numbers only when they make the recommendation clearer. Do not use jargon like "Zone 2". Do not mention a metric that is missing from the snapshot.
2. Proposal: State "Tomorrow's recommendation: [push session / normal session / light session] [routine] at [HH:MM]." Choose the time from tomorrow's calendar and the scheduling constraints.

Only explain "why" when it is directly supported by the snapshot. Do not say things like avoiding legs, workout fatigue, or poor recovery unless the relevant data is present.
CRITICAL: If you propose a workout, you MUST output the scheduling JSON block for tomorrow. If you do not output a valid scheduling JSON block, the evening check-in will not be sent.
"""
    else:
        time_context = """
This is a MORNING BRIEFING.
Write exactly 2 or 3 short paragraphs in plain English:
1. Metrics sentence: Mention only the most relevant signals for today's decision (readiness, sleep, HRV, ACWR/load, or yesterday's workout). Keep it simple and factual. Use numbers only when they make the recommendation clearer. Do not use jargon like "Zone 2". Do not mention a metric that is missing from the snapshot.
2. Recommendation: Give one clear recommendation for today. If a workout is already listed in `scheduled_workouts_NOT_completed` for today, reference that workout instead of recommending a new one. If training is recommended, state the intensity as exactly one of: "push session", "normal session", or "light session", and name the routine (for example, "Chest & Biceps"). If recovery is the right call, simply recommend resting and say no workout is needed.
3. Timing: Only if training is recommended, give the best exact time based on the calendar and scheduling constraints. If a workout is already scheduled for today, use its scheduled time.

Only explain "why" when it is directly supported by the snapshot. Do not say things like avoiding legs, workout fatigue, or poor recovery unless the relevant data is present.
CRITICAL: Do NOT output any JSON blocks and do NOT attempt to schedule a workout from the morning briefing. Ignore the scheduling JSON rule in the system prompt.
"""

    prompt = f"""Generate the coaching message for the user.
Review the following metrics snapshot:
{snapshot_json}

{time_context}
Do NOT use markdown headers or greetings, just give the insight.
"""
    suggestion_text, json_str = _generate_with_retry(SYSTEM_PROMPT, prompt, session=session)

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
        greeting = "🌅 *Morning Briefing*" if is_morning else "🌙 *Evening Check-in*"
        
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
            send_message(f"{greeting}\n\n{suggestion_text}")
    except Exception as e:
        import logging
        logging.error(f"Failed to send proactive Telegram notification: {e}")


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
    chat_text, json_str = _generate_with_retry(SYSTEM_PROMPT, prompt_with_context, history, session=session)
    
    # Save assistant message
    asst_msg = CoachMessage(
        role="assistant",
        content=chat_text,
        created_at=datetime.now(timezone.utc),
        data_snapshot=snapshot_json,
        pending_action_json=json_str
    )
    session.add(asst_msg)
    session.commit()
    
    return chat_text, asst_msg
