"""Fact builder for the AI Coach. Gathers DB metrics into a JSON snapshot."""
import json
import yaml
import logging
import os
import pytz
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from db import (
    Activity,
    AthleteProfile,
    DailyHealth,
    DailyMetrics,
    ExerciseSet,
    Goal,
    MetricSnapshot,
    PlannedSession,
    ProgramSession,
    SessionExercise,
    Sleep,
    SyncState,
    TrainingProgram,
    Workout,
)
from coach.onboarding import activity_family, active_program, program_sessions_for
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


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}

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
                    ExerciseSet.weight_kg > 0,
                    ExerciseSet.reps.isnot(None)
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
                    ex_history.append(f"{best_set.weight_kg}kg x{best_set.reps}{e1rm_str} ({time_str})")
            
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
    from time_utils import get_local_now, get_local_tz
    
    try:
        local_time = get_local_now()
        local_tz = get_local_tz()
    except Exception:
        local_time = datetime.now()
        local_tz = "UTC"
        
    snapshot = {
        "current_local_time": f"{local_time.strftime('%A, %B %d, %Y %H:%M')} (Timezone: {local_tz})",
        "user_goal": goal_text,
        "user_constraints": constraints,
        "upcoming_schedule_7_days": get_upcoming_schedule(days=7)
    }

    profile = session.get(AthleteProfile, 1)
    if profile:
        snapshot["athlete_profile"] = {
            "training_type": profile.training_type,
            "primary_goal": profile.primary_goal,
            "goal_detail": profile.goal_detail,
            "preferred_activities": _json_list(profile.preferred_activities),
            "activity_preferences": _json_list(profile.activity_preferences),
            "equipment_access": _json_list(profile.equipment_access),
            "availability": profile.availability,
            "timing_preferences": _json_object(profile.timing_preferences),
            "injuries_limitations": profile.injuries_limitations,
            "sport_commitments": profile.sport_commitments,
            "scheduling_preferences": profile.scheduling_preferences,
            "onboarding_complete": profile.onboarding_complete,
        }

    current_program = active_program(session)
    if current_program:
        sessions = program_sessions_for(session, current_program.id)
        snapshot["active_program"] = {
            "id": current_program.id,
            "name": current_program.name,
            "mode": current_program.mode,
            "source_type": current_program.source_type,
            "goal_tags": _json_list(current_program.goal_tags),
            "days_per_week": current_program.days_per_week,
            "equipment": _json_list(current_program.equipment),
            "sessions": [
                {
                    "id": ps.id,
                    "name": ps.name,
                    "activity_type": ps.sport_type,
                    "activity_family": activity_family(ps.sport_type),
                    "sequence_order": ps.sequence_order,
                    "focus_tags": _json_list(ps.focus_tags),
                    "duration_min": ps.duration_min,
                    "session_role": ps.session_role,
                    "target_frequency": ps.target_frequency,
                    "notes": ps.notes,
                    "is_addon": bool(ps.is_addon),
                }
                for ps in sessions
            ],
        }

        # Inject user-configured base workout templates into the snapshot.
        # Sessions marked is_addon=true are finisher blocks to be appended
        # to another session, not scheduled as standalone workouts.
        base_templates = []
        for ps in sessions:
            exs = (
                session.query(SessionExercise)
                .filter_by(program_session_id=ps.id)
                .order_by(SessionExercise.order_index)
                .all()
            )
            if exs:
                base_templates.append({
                    "session": ps.name,
                    "is_addon": bool(ps.is_addon),
                    "exercises": [
                        {
                            "exercise": ex.exercise_name.replace("_", " ").title(),
                            "sets": ex.sets,
                            "reps": ex.reps,
                            "weight_kg": ex.weight_kg,
                            **(({"notes": ex.notes}) if ex.notes else {}),
                        }
                        for ex in exs
                    ],
                })
        if base_templates:
            snapshot["base_workout_templates"] = base_templates
    
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
        
    # Long-term Fitness Metrics (vo2max, fitness_age) omitted — not used
    # by prompt rules for workout decisions.
    
    today = date.today()

    def _staleness(day) -> str | None:
        """Label a block's date if it isn't today, so the model doesn't treat
        old data as current (and can honestly say it lacks today's numbers)."""
        if day is None or day == today:
            return None
        d = (today - day).days
        return f"{day.isoformat()} ({d} day{'s' if d != 1 else ''} ago)"

    # 2. Latest Metrics — fetch the last 3 days to provide a trend for ACWR.
    recent_metrics = (
        session.query(DailyMetrics)
        .filter((DailyMetrics.readiness.isnot(None)) | (DailyMetrics.acwr.isnot(None)))
        .order_by(DailyMetrics.day.desc())
        .limit(3)
        .all()
    )
    if not recent_metrics:
        recent_metrics = session.query(DailyMetrics).order_by(DailyMetrics.day.desc()).limit(3).all()
        
    if recent_metrics:
        latest_metrics = recent_metrics[0]
        acwr_val = latest_metrics.acwr
        
        block = {
            "date": latest_metrics.day.isoformat(),
            "data_as_of": _staleness(latest_metrics.day),
            "readiness_score_0_to_100": latest_metrics.readiness,
            "acute_load_7d": latest_metrics.acute_load,
            "chronic_load_28d": latest_metrics.chronic_load,
            "acwr_ratio": acwr_val,
            "acwr_status": acwr_label(acwr_val) if acwr_val is not None else None,
            "acwr_3_day_trend": [round(m.acwr, 2) for m in reversed(recent_metrics) if m.acwr is not None],
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
            "name": a.name,
            "type": a.activity_type,
            "start_time": a.start_time.isoformat() if a.start_time else None,
            "duration_minutes": round(a.duration_s / 60) if a.duration_s else 0,
            "training_load": getattr(a, "training_load", None),
            "calories": getattr(a, "calories", None),
            "rpe_0_to_100": getattr(a, "rpe", None),
            "feel_0_to_100": getattr(a, "feel", None)
        }
        if a.activity_type == "strength_training":
            sets = session.query(ExerciseSet).filter_by(activity_id=a.id).all()
            valid_sets = [s for s in sets if s.weight_kg and s.reps is not None]
            if valid_sets:
                # Group identical (exercise, weight, reps) into "3×10 @ 15.0kg"
                counts = Counter(
                    (_humanize_ex(s.exercise_category), s.weight_kg, s.reps)
                    for s in valid_sets
                )
                w["exercises"] = [
                    f"{ex}: {cnt}\u00d7{reps} @ {wt}kg"
                    for (ex, wt, reps), cnt in counts.items()
                ]
        workouts.append(w)
        
    if workouts:
        snapshot["recent_workouts"] = workouts

    # 5. User Pre-defined Workouts
    # Only inject days-since and progressive overload stats — the full template
    # list is no longer needed since the user defines base workouts in the app.
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

    from coach.garmin_compiler import _COACH_PREFIX
    saved_workouts = (
        session.query(Workout)
        .filter(~Workout.name.startswith(_COACH_PREFIX))
        .all()
    )
    if saved_workouts:
        routine_exercises = {}
        for w in saved_workouts:
            ex_names = _extract_exercises(w.steps_json)
            if w.sport_type == "strength_training":
                routine_exercises[w.name] = ex_names

        # Days since each routine was last trained — directly serves the
        # "pick the least-recently-trained muscle group" rule in the prompt.
        days_since = _days_since_last_trained(session, routine_exercises)
        if days_since:
            history_log = []
            for routine_name, days in days_since.items():
                if days is None:
                    history_log.append(f"'{routine_name}' has never been trained in recorded history.")
                elif days == 0:
                    history_log.append(f"'{routine_name}' was trained TODAY (0 days ago).")
                elif days == 1:
                    history_log.append(f"'{routine_name}' was trained YESTERDAY (1 day ago).")
                else:
                    history_log.append(f"'{routine_name}' was trained {days} days ago.")
            snapshot["workout_history_log"] = history_log

        # Inject the progressive-overload history map.
        unique_exercises: set[str] = set()
        for ex_set in routine_exercises.values():
            unique_exercises |= ex_set
        if unique_exercises:
            raw_stats = _get_recent_exercise_stats(session, unique_exercises)
            if raw_stats:
                snapshot["recent_exercise_stats"] = {_humanize_ex(k): v for k, v in raw_stats.items()}

    # 6. Scheduled (planned, NOT completed) workouts — coach-created workouts
    # that have been pushed to Garmin but haven't been performed yet.
    # We read this from the projected calendar events to only see future workouts.
    through = local_time.date() + timedelta(days=14)
    planned = (
        session.query(PlannedSession)
        .filter(PlannedSession.target_date >= local_time.date())
        .filter(PlannedSession.target_date <= through)
        .order_by(PlannedSession.target_date.asc(), PlannedSession.suggested_time.asc())
        .all()
    )
    if planned:
        snapshot["rolling_plan_14_days"] = [
            {
                "id": p.id,
                "title": p.title,
                "activity_type": p.activity_type,
                "scheduled_date": p.target_date.isoformat(),
                "scheduled_time": p.suggested_time,
                "duration_min": p.duration_min,
                "intensity": p.intensity,
                "status": p.status,
                "garmin_workout_id": p.garmin_workout_id,
            }
            for p in planned
        ]

    cal_row = session.get(SyncState, "coach_calendar_events")
    today_iso = local_time.date().isoformat()
    scheduled_future = []
    
    if cal_row and cal_row.value:
        try:
            events = json.loads(cal_row.value)
            for e in events:
                if e.get("date", "") >= today_iso:
                    scheduled_future.append(e)
        except Exception:
            pass
            
    if scheduled_future:
        snapshot["scheduled_workouts_NOT_completed"] = [
            {
                "name": e.get("title", "").replace(_COACH_PREFIX, "").strip(),
                "scheduled_date": e.get("date"),
                "scheduled_time": e.get("start_time")
            }
            for e in scheduled_future
        ]

    return _serialize_with_guard(snapshot)


def _serialize_with_guard(snapshot: dict) -> str:
    """Serialize the snapshot, shedding the lowest-value data if it exceeds the
    soft size limit. Never silently truncate — log what was dropped."""
    out = yaml.dump(snapshot, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if len(out) <= _SNAPSHOT_SOFT_LIMIT_CHARS:
        return out

    # Shed oldest exercise-history entries first (keep only the most recent per
    # exercise), then drop the map entirely if still too big.
    stats = snapshot.get("recent_exercise_stats")
    if isinstance(stats, dict):
        trimmed = {k: v[:1] for k, v in stats.items()}
        snapshot["recent_exercise_stats"] = trimmed
        out = yaml.dump(snapshot, default_flow_style=False, sort_keys=False, allow_unicode=True)
        logger.warning(
            "Snapshot exceeded %d chars; trimmed recent_exercise_stats to most-recent entry only.",
            _SNAPSHOT_SOFT_LIMIT_CHARS,
        )
    if len(out) > _SNAPSHOT_SOFT_LIMIT_CHARS and "recent_exercise_stats" in snapshot:
        snapshot.pop("recent_exercise_stats", None)
        out = yaml.dump(snapshot, default_flow_style=False, sort_keys=False, allow_unicode=True)
        logger.warning("Snapshot still oversized; dropped recent_exercise_stats entirely.")
    return out



