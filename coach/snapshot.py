"""Fact builder for the AI Coach. Gathers DB metrics into a JSON snapshot."""
import json
import os
import pytz
from datetime import date, datetime

from sqlalchemy.orm import Session

from db import Goal, DailyMetrics, DailyHealth, Sleep, Activity, ExerciseSet, SyncState, MetricSnapshot
from metrics.engine import acwr_label

def _get_recent_exercise_stats(session: Session, unique_exercises: set) -> dict:
    """Find the most recent performance for specific exercises."""
    stats = {}
    for ex in unique_exercises:
        if not ex or ex == "Activity":
            continue
            
        # Find the most recent ExerciseSet that matches this category or name
        # We join with Activity to ensure we get the latest by start_time
        latest_set = session.query(ExerciseSet).join(Activity).filter(
            (ExerciseSet.exercise_category == ex) | (ExerciseSet.exercise_name == ex),
            ExerciseSet.weight_kg > 0
        ).order_by(Activity.start_time.desc()).first()
        
        if latest_set and latest_set.activity and latest_set.activity.start_time:
            days_ago = (date.today() - latest_set.activity.start_time.date()).days
            time_str = "today" if days_ago == 0 else f"{days_ago} days ago"
            stats[ex] = f"{latest_set.weight_kg}kg for {latest_set.reps} reps ({time_str})"
            
    return stats

def build_snapshot(session: Session) -> str:
    """Build a concise factual snapshot for the LLM prompt."""
    
    # 1. Goal & Basic Context
    goal_row = session.get(Goal, 1)
    goal_text = goal_row.goal if goal_row else "No specific goal set."
    constraints = goal_row.custom_input if goal_row else "None."
    
    from coach.calendar import get_upcoming_schedule
    
    try:
        local_tz = pytz.timezone(os.getenv("USER_TIMEZONE", "Asia/Jerusalem"))
        local_time = datetime.now(local_tz)
    except Exception:
        local_time = datetime.now()
        
    snapshot = {
        "current_local_time": local_time.strftime("%A, %B %d, %Y %H:%M"),
        "user_goal": goal_text,
        "user_constraints": constraints,
        "upcoming_schedule_14_days": get_upcoming_schedule(days=14)
    }
    
    # User Profile (Weight & Gender)
    gender = session.get(SyncState, "user_gender")
    weight = session.get(SyncState, "user_weight")
    if gender or weight:
        snapshot["user_profile"] = {
            "gender": gender.value if gender else "unknown",
            "weight_kg": float(weight.value) if weight and weight.value else "unknown"
        }
        
    # Long-term Fitness Metrics
    vo2max = session.get(MetricSnapshot, "vo2max")
    fitness_age = session.get(MetricSnapshot, "fitness_age")
    if vo2max or fitness_age:
        snapshot["long_term_fitness"] = {
            "vo2max": vo2max.value if vo2max else "unknown",
            "fitness_age": fitness_age.value if fitness_age else "unknown"
        }
    
    # 2. Latest Metrics
    latest_metrics = session.query(DailyMetrics).order_by(DailyMetrics.day.desc()).first()
    if latest_metrics:
        acwr_val = latest_metrics.acwr
        snapshot["daily_metrics"] = {
            "date": latest_metrics.day.isoformat(),
            "readiness_score_0_to_100": latest_metrics.readiness,
            "acute_load_7d": latest_metrics.acute_load,
            "chronic_load_28d": latest_metrics.chronic_load,
            "acwr_ratio": acwr_val,
            "acwr_status": acwr_label(acwr_val) if acwr_val is not None else None,
            "sleep_debt_hours": latest_metrics.sleep_debt_h
        }
        
    # 3. Latest Health
    latest_health = session.query(DailyHealth).order_by(DailyHealth.day.desc()).first()
    if latest_health:
        snapshot["latest_health"] = {
            "date": latest_health.day.isoformat(),
            "resting_hr": latest_health.resting_hr,
            "hrv_overnight": latest_health.hrv_overnight,
            "body_battery_high": latest_health.body_battery_high,
            "body_battery_low": latest_health.body_battery_low,
            "stress_avg": latest_health.stress_avg,
            "total_kcal": getattr(latest_health, "total_kcal", None),
            "active_kcal": getattr(latest_health, "active_kcal", None),
            "bmr_kcal": getattr(latest_health, "bmr_kcal", None),
            "garmin_training_readiness": getattr(latest_health, "training_readiness", None),
            "garmin_training_status": getattr(latest_health, "training_status", None)
        }
        
    # 3b. Latest Sleep
    latest_sleep = session.query(Sleep).order_by(Sleep.day.desc()).first()
    if latest_sleep:
        snapshot["latest_sleep"] = {
            "date": latest_sleep.day.isoformat(),
            "total_hours": round((latest_sleep.total_s or 0) / 3600, 1),
            "sleep_score": latest_sleep.score,
            "respiration_avg": getattr(latest_sleep, "respiration_avg", None),
            "sleep_stress_avg": getattr(latest_sleep, "sleep_stress_avg", None)
        }
        
    # 4. Recent Workouts (Last 3)
    recent_activities = session.query(Activity).order_by(Activity.start_time.desc()).limit(3).all()
    workouts = []
    for a in recent_activities:
        w = {
            "type": a.activity_type,
            "start_time": a.start_time.isoformat() if a.start_time else None,
            "duration_minutes": round(a.duration_s / 60) if a.duration_s else 0,
            "training_load": getattr(a, "training_load", None),
            "calories": getattr(a, "calories", None)
        }
        if a.activity_type == "strength_training":
            sets = session.query(ExerciseSet).filter_by(activity_id=a.id).all()
            if sets:
                w["exercises"] = [
                    f"{s.exercise_category}: {s.reps} reps @ {s.weight_kg}kg" for s in sets if s.weight_kg
                ]
        workouts.append(w)
        
    if workouts:
        snapshot["recent_workouts"] = workouts

    # 5. User Pre-defined Workouts
    from db import Workout
    def _parse_workout_steps(steps_json: str) -> list[str]:
        try:
            segments = json.loads(steps_json)
            out = []
            for seg in segments:
                for step in seg.get("workoutSteps", []):
                    if step.get("type") == "ExecutableStepDTO":
                        if step.get("stepType", {}).get("stepTypeKey") == "rest":
                            continue
                        cat = step.get("category") or step.get("exerciseName") or "Activity"
                        reps = step.get("endConditionValue", "")
                        weight = step.get("weightValue")
                        cond = step.get("endCondition", {}).get("conditionTypeKey")
                        w_str = f" @ {weight}kg" if weight and weight > 0 else ""
                        rep_str = f"{reps} {cond}" if cond else f"{reps} reps"
                        out.append(f"{len(out)}: {cat}: {rep_str}{w_str}")
                    elif step.get("type") == "RepeatGroupDTO":
                        iters = step.get("numberOfIterations", 1)
                        sub = []
                        for child in step.get("workoutSteps", []):
                            if child.get("stepType", {}).get("stepTypeKey") == "rest":
                                continue
                            cat = child.get("category") or child.get("exerciseName") or "Activity"
                            reps = child.get("endConditionValue", "")
                            cond = child.get("endCondition", {}).get("conditionTypeKey")
                            weight = child.get("weightValue")
                            w_str = f" @ {weight}kg" if weight and weight > 0 else ""
                            rep_str = f"{reps} {cond}" if cond else f"{reps} reps"
                            sub.append(f"{cat} ({rep_str}{w_str})")
                        if sub:
                            out.append(f"{len(out)}: {iters}x [ {', '.join(sub)} ]")
            return out
        except Exception:
            return []

    saved_workouts = session.query(Workout).all()
    if saved_workouts:
        unique_exercises = set()
        user_workouts_data = []
        
        for w in saved_workouts:
            parsed = _parse_workout_steps(w.steps_json)
            user_workouts_data.append({
                "id": w.workout_id,
                "name": w.name, 
                "sport": w.sport_type, 
                "steps": parsed
            })
            # Extract exercise names to build our history map
            for step_str in parsed:
                # Format is "0: SQUAT: 10 reps @ 50kg" or "0: 3x [ SQUAT (10 reps @ 50kg) ]"
                # For simplicity, if it's a simple step, category is the second part
                parts = step_str.split(": ")
                if len(parts) >= 3 and parts[1] != "Activity":
                    unique_exercises.add(parts[1])
                elif len(parts) >= 2 and "[" in parts[1]:
                    # Extract from "3x [ SQUAT (10 reps), BENCH (5 reps) ]"
                    bracket_content = parts[1][parts[1].find("[")+1 : parts[1].rfind("]")]
                    for ex_part in bracket_content.split(","):
                        ex_name = ex_part.split("(")[0].strip()
                        if ex_name and ex_name != "Activity":
                            unique_exercises.add(ex_name)
                            
        snapshot["user_saved_workouts"] = user_workouts_data
        
        # Inject the history map!
        if unique_exercises:
            snapshot["recent_exercise_stats"] = _get_recent_exercise_stats(session, unique_exercises)

    return json.dumps(snapshot, indent=2)



