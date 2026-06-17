"""Fact builder for the AI Coach. Gathers DB metrics into a JSON snapshot."""
import json
from datetime import date

from sqlalchemy.orm import Session

from db import Goal, DailyMetrics, DailyHealth, Sleep, Activity, ExerciseSet
from metrics.engine import acwr_label

def build_snapshot(session: Session) -> str:
    """Build a concise factual snapshot for the LLM prompt."""
    
    # 1. Goal
    goal_row = session.get(Goal, 1)
    goal_text = goal_row.goal if goal_row else "No specific goal set."
    constraints = goal_row.custom_input if goal_row else "None."
    
    from coach.calendar import get_todays_schedule
    
    snapshot = {
        "user_goal": goal_text,
        "user_constraints": constraints,
        "today_date": date.today().isoformat(),
        "today_schedule": get_todays_schedule()
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

    return json.dumps(snapshot, indent=2)



