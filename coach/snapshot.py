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
    
    snapshot = {
        "user_goal": goal_text,
        "user_constraints": constraints,
        "today_date": date.today().isoformat()
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
            "stress_avg": latest_health.stress_avg
        }
        
    # 4. Recent Workouts (Last 3)
    recent_activities = session.query(Activity).order_by(Activity.start_time.desc()).limit(3).all()
    workouts = []
    for a in recent_activities:
        w = {
            "type": a.activity_type,
            "start_time": a.start_time.isoformat() if a.start_time else None,
            "duration_minutes": round(a.duration_s / 60) if a.duration_s else 0,
            "training_load": getattr(a, "training_load", None)
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


def build_weekly_snapshot(session: Session, start_date: date, end_date: date) -> str:
    """Build a snapshot for a specific week."""
    from sqlalchemy import func
    
    goal_row = session.get(Goal, 1)
    
    snapshot = {
        "user_goal": goal_row.goal if goal_row else "None",
        "week_start": start_date.isoformat(),
        "week_end": end_date.isoformat(),
    }
    
    # 1. Activities in this week
    from datetime import datetime, time
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)
    
    activities = session.query(Activity).filter(
        Activity.start_time >= start_dt,
        Activity.start_time <= end_dt
    ).all()
    
    workouts = []
    for a in activities:
        workouts.append({
            "type": a.activity_type,
            "date": a.start_time.isoformat() if a.start_time else None,
            "duration_minutes": round(a.duration_s / 60) if a.duration_s else 0,
            "distance_km": round(a.distance_m / 1000, 2) if getattr(a, "distance_m", None) else None,
            "avg_hr": a.avg_hr
        })
    snapshot["workouts"] = workouts
    
    # 2. Average Readiness for the week
    readiness_rows = session.query(DailyMetrics).filter(
        DailyMetrics.day >= start_date,
        DailyMetrics.day <= end_date
    ).all()
    
    if readiness_rows:
        avg_readiness = sum(r.readiness for r in readiness_rows if r.readiness is not None) / len([r for r in readiness_rows if r.readiness is not None])
        snapshot["avg_readiness"] = round(avg_readiness)
        
        # Include end of week ACWR
        last_metric = max(readiness_rows, key=lambda r: r.day)
        snapshot["end_of_week_acwr"] = last_metric.acwr
        snapshot["end_of_week_acwr_status"] = acwr_label(last_metric.acwr) if last_metric.acwr is not None else None

    return json.dumps(snapshot, indent=2)
