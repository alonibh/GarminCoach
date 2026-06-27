"""Fact builder for the AI Coach. Gathers DB metrics into a JSON snapshot."""
import json
import logging
import os
import pytz
from datetime import date, datetime

from sqlalchemy.orm import Session

from db import Goal, DailyMetrics, DailyHealth, Sleep, Activity, ExerciseSet, SyncState, MetricSnapshot
from metrics.engine import acwr_label

logger = logging.getLogger(__name__)

# Soft ceiling on the serialized snapshot. Well above a trimmed payload; if we
# blow past it we shed the lowest-value data (oldest exercise history) and log.
_SNAPSHOT_SOFT_LIMIT_CHARS = 24000


def _is_empty(value) -> bool:
    """True if a value carries no real signal (None, empty, or zero)."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.lower() == "unknown"
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (list, dict, set)):
        return len(value) == 0
    return False


def _prune_block(block: dict, keep_keys: tuple = ()) -> dict | None:
    """Drop empty fields from a metrics block. Returns None if nothing but the
    always-kept keys (e.g. "date") remains — i.e. the block has no real data."""
    cleaned = {k: v for k, v in block.items() if k in keep_keys or not _is_empty(v)}
    if all(k in keep_keys for k in cleaned):
        return None
    return cleaned

def _get_recent_exercise_stats(session: Session, unique_exercises: set) -> dict:
    """Find up to the 3 most recent performances for specific exercises to show progression."""
    stats = {}
    for ex in unique_exercises:
        if not ex or ex == "Activity":
            continue
            
        # Get all activities containing this exercise, ordered by newest first
        all_acts = session.query(Activity.id, Activity.start_time).join(ExerciseSet).filter(
            (ExerciseSet.exercise_category == ex) | (ExerciseSet.exercise_name == ex),
            ExerciseSet.weight_kg > 0
        ).order_by(Activity.start_time.desc()).all()
        
        # Deduplicate to get the 3 most recent distinct activities
        seen_ids = set()
        recent_acts = []
        for act in all_acts:
            if act.id not in seen_ids:
                seen_ids.add(act.id)
                recent_acts.append(act)
                if len(recent_acts) == 3:
                    break
        
        if recent_acts:
            ex_history = []
            for act in recent_acts:
                # Fetch all sets for this exercise from that specific activity
                sets = session.query(ExerciseSet).filter(
                    ExerciseSet.activity_id == act.id,
                    ((ExerciseSet.exercise_category == ex) | (ExerciseSet.exercise_name == ex)),
                    ExerciseSet.weight_kg > 0
                ).all()
                
                if sets:
                    # Find the best set by Epley 1RM, falling back to raw weight if reps > 12
                    def _score(s):
                        if s.reps and 1 <= s.reps <= 12:
                            if s.reps == 1:
                                return s.weight_kg
                            return s.weight_kg * (1 + s.reps / 30.0)
                        return s.weight_kg or 0
                        
                    best_set = max(sets, key=_score)
                    e1rm = _score(best_set)
                    
                    days_ago = (date.today() - act.start_time.date()).days if act.start_time else 0
                    time_str = "today" if days_ago == 0 else f"{days_ago} days ago"
                    
                    e1rm_str = f" (Est. 1RM: {round(e1rm, 1)}kg)" if best_set.reps and 1 <= best_set.reps <= 12 else ""
                    ex_history.append(f"{best_set.weight_kg}kg for {best_set.reps} reps{e1rm_str} ({time_str})")
            
            if ex_history:
                stats[ex] = ex_history

    return stats


def _days_since_last_trained(session: Session, routine_exercises: dict) -> dict:
    """For each strength routine, how many days since it was last performed.

    `routine_exercises` maps routine name -> set of raw exercise category/name
    strings belonging to that routine. We find the most recent strength
    Activity that contains any of those exercises. Serves the system-prompt
    rule about picking the least-recently-trained muscle group.
    """
    out = {}
    for routine_name, exercises in routine_exercises.items():
        exercises = {e for e in exercises if e and e != "Activity"}
        if not exercises:
            continue
        last_act = (
            session.query(Activity.start_time)
            .join(ExerciseSet)
            .filter(
                (ExerciseSet.exercise_category.in_(exercises))
                | (ExerciseSet.exercise_name.in_(exercises))
            )
            .order_by(Activity.start_time.desc())
            .first()
        )
        if last_act and last_act.start_time:
            out[routine_name] = (date.today() - last_act.start_time.date()).days
        else:
            out[routine_name] = None  # never recorded in synced history
    return out


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
        "upcoming_schedule_7_days": get_upcoming_schedule(days=7)
    }
    
    # User Profile (Weight & Gender & Age)
    gender = session.get(SyncState, "user_gender")
    weight = session.get(SyncState, "user_weight")
    birth_date = session.get(SyncState, "user_birth_date")
    
    age = "unknown"
    if birth_date and birth_date.value:
        try:
            bd = date.fromisoformat(birth_date.value[:10])
            today = date.today()
            age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        except ValueError:
            pass

    if gender or weight or age != "unknown":
        snapshot["user_profile"] = {
            "gender": gender.value if gender else "unknown",
            "age": age,
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
    
    today = date.today()

    def _staleness(day) -> str | None:
        """Label a block's date if it isn't today, so the model doesn't treat
        old data as current (and can honestly say it lacks today's numbers)."""
        if day is None or day == today:
            return None
        d = (today - day).days
        return f"{day.isoformat()} ({d} day{'s' if d != 1 else ''} ago)"

    # 2. Latest Metrics — prefer the most recent row that actually has a
    # readiness or ACWR value; an all-null "today" row is worse than a slightly
    # older row with real signal.
    latest_metrics = (
        session.query(DailyMetrics)
        .filter((DailyMetrics.readiness.isnot(None)) | (DailyMetrics.acwr.isnot(None)))
        .order_by(DailyMetrics.day.desc())
        .first()
    ) or session.query(DailyMetrics).order_by(DailyMetrics.day.desc()).first()
    if latest_metrics:
        acwr_val = latest_metrics.acwr
        block = {
            "date": latest_metrics.day.isoformat(),
            "data_as_of": _staleness(latest_metrics.day),
            "readiness_score_0_to_100": latest_metrics.readiness,
            "acute_load_7d": latest_metrics.acute_load,
            "chronic_load_28d": latest_metrics.chronic_load,
            "acwr_ratio": acwr_val,
            "acwr_status": acwr_label(acwr_val) if acwr_val is not None else None,
            "sleep_debt_hours": latest_metrics.sleep_debt_h,
        }
        pruned = _prune_block(block, keep_keys=("date",))
        if pruned:
            snapshot["daily_metrics"] = pruned
        else:
            snapshot["metrics_available"] = False

    # 3. Latest Health
    latest_health = session.query(DailyHealth).order_by(DailyHealth.day.desc()).first()
    if latest_health:
        block = {
            "date": latest_health.day.isoformat(),
            "data_as_of": _staleness(latest_health.day),
            "resting_hr": latest_health.resting_hr,
            "hrv_overnight": latest_health.hrv_overnight,
            "body_battery_high": latest_health.body_battery_high,
            "body_battery_low": latest_health.body_battery_low,
            "stress_avg": latest_health.stress_avg,
            "total_kcal": getattr(latest_health, "total_kcal", None),
            "active_kcal": getattr(latest_health, "active_kcal", None),
            "bmr_kcal": getattr(latest_health, "bmr_kcal", None),
            "garmin_training_readiness": getattr(latest_health, "training_readiness", None),
            "garmin_training_status": getattr(latest_health, "training_status", None),
        }
        pruned = _prune_block(block, keep_keys=("date",))
        if pruned:
            snapshot["latest_health"] = pruned

    # 3b. Latest Sleep
    latest_sleep = session.query(Sleep).order_by(Sleep.day.desc()).first()
    if latest_sleep:
        block = {
            "date": latest_sleep.day.isoformat(),
            "data_as_of": _staleness(latest_sleep.day),
            "total_hours": round((latest_sleep.total_s or 0) / 3600, 1),
            "sleep_score": latest_sleep.score,
            "respiration_avg": getattr(latest_sleep, "respiration_avg", None),
            "sleep_stress_avg": getattr(latest_sleep, "sleep_stress_avg", None),
        }
        pruned = _prune_block(block, keep_keys=("date",))
        if pruned:
            snapshot["latest_sleep"] = pruned

    # 4. Recent Workouts (Last 3)
    recent_activities = session.query(Activity).order_by(Activity.start_time.desc()).limit(3).all()
    workouts = []
    
    def _humanize_ex(name: str) -> str:
        if not name: return ""
        return name.replace("_", " ").title()
        
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
                    f"{_humanize_ex(s.exercise_category)}: {s.reps} reps @ {s.weight_kg}kg" for s in sets if s.weight_kg
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
                        cat = _humanize_ex(step.get("exerciseName") or step.get("category") or "Activity")
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
                            cat = _humanize_ex(child.get("exerciseName") or child.get("category") or "Activity")
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

    def _extract_exercises(steps_json: str) -> set:
        """Raw exercise category/name strings from a workout's step JSON."""
        names = set()
        try:
            segments = json.loads(steps_json)
            for seg in segments:
                for step in seg.get("workoutSteps", []):
                    if step.get("type") == "ExecutableStepDTO":
                        cat = step.get("exerciseName") or step.get("category")
                        if cat:
                            names.add(cat)
                    elif step.get("type") == "RepeatGroupDTO":
                        for child in step.get("workoutSteps", []):
                            cat = child.get("exerciseName") or child.get("category")
                            if cat:
                                names.add(cat)
        except Exception:
            pass
        return names

    # Only strength routines are relevant to gym coaching. Running templates
    # (cardio) are handled separately and were bloating the payload massively.
    # Exclude coach-created workouts (starting with the emoji prefix) to prevent
    # the AI from getting confused by its own scheduled workouts.
    from coach.garmin_compiler import _COACH_PREFIX
    saved_workouts = (
        session.query(Workout)
        .filter(Workout.sport_type == "strength_training")
        .filter(~Workout.name.startswith(_COACH_PREFIX))
        .all()
    )
    if saved_workouts:
        unique_exercises = set()
        routine_exercises = {}  # routine name -> raw exercise names (for "days since")
        user_workouts_data = []

        for w in saved_workouts:
            parsed = _parse_workout_steps(w.steps_json)
            user_workouts_data.append({
                "id": w.workout_id,
                "name": w.name,
                "sport": w.sport_type,
                "steps": parsed
            })

            ex_names = _extract_exercises(w.steps_json)
            unique_exercises |= ex_names
            routine_exercises[w.name] = ex_names

        snapshot["user_saved_workouts"] = user_workouts_data

        # Days since each routine was last trained — directly serves the
        # "pick the least-recently-trained muscle group" rule in the prompt.
        days_since = {
            k: v for k, v in _days_since_last_trained(session, routine_exercises).items()
            if v is not None
        }
        if days_since:
            snapshot["days_since_last_trained"] = days_since

        # Inject the progressive-overload history map.
        if unique_exercises:
            raw_stats = _get_recent_exercise_stats(session, unique_exercises)
            if raw_stats:
                snapshot["recent_exercise_stats"] = {_humanize_ex(k): v for k, v in raw_stats.items()}

    return _serialize_with_guard(snapshot)


def _serialize_with_guard(snapshot: dict) -> str:
    """Serialize the snapshot, shedding the lowest-value data if it exceeds the
    soft size limit. Never silently truncate — log what was dropped."""
    out = json.dumps(snapshot, indent=2)
    if len(out) <= _SNAPSHOT_SOFT_LIMIT_CHARS:
        return out

    # Shed oldest exercise-history entries first (keep only the most recent per
    # exercise), then drop the map entirely if still too big.
    stats = snapshot.get("recent_exercise_stats")
    if isinstance(stats, dict):
        trimmed = {k: v[:1] for k, v in stats.items()}
        snapshot["recent_exercise_stats"] = trimmed
        out = json.dumps(snapshot, indent=2)
        logger.warning(
            "Snapshot exceeded %d chars; trimmed recent_exercise_stats to most-recent entry only.",
            _SNAPSHOT_SOFT_LIMIT_CHARS,
        )
    if len(out) > _SNAPSHOT_SOFT_LIMIT_CHARS and "recent_exercise_stats" in snapshot:
        snapshot.pop("recent_exercise_stats", None)
        out = json.dumps(snapshot, indent=2)
        logger.warning("Snapshot still oversized; dropped recent_exercise_stats entirely.")
    return out



