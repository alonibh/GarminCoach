import json
import logging
from datetime import date, datetime, timedelta
import os
from sqlalchemy.orm import Session

from db import Workout
from sync.garmin_client import client
from coach.actions import parse_action

logger = logging.getLogger(__name__)

def build_generic_step(description: str, reps: int, weight_kg: float, exercise_name: str = None, category: str = None) -> dict:
    """Build a generic interval step."""
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": 0,  # Will be re-indexed later
        "stepType": {
            "stepTypeId": 3,
            "stepTypeKey": "interval",
            "displayOrder": 3
        },
        "childStepId": 0,
        "description": description,
        "endCondition": {
            "conditionTypeId": 10,
            "conditionTypeKey": "reps",
            "displayOrder": 10,
            "displayable": True
        },
        "endConditionValue": reps,
        "preferredEndConditionUnit": None,
        "endConditionCompare": "",
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1
        },
        "targetValueOne": None,
        "targetValueTwo": None,
        "targetValueUnit": None,
        "zoneNumber": None,
        "secondaryTargetType": None,
        "secondaryTargetValueOne": None,
        "secondaryTargetValueTwo": None,
        "secondaryTargetValueUnit": None,
        "secondaryZoneNumber": None,
        "endConditionZone": None,
        "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
        "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0},
        "category": category,
        "exerciseName": exercise_name,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": weight_kg if weight_kg is not None and weight_kg > 0 else -1.0,
        "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    }

def build_cardio_warmup_step() -> dict:
    """Build a 5-minute generic cardio warm-up step."""
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": 0,
        "stepType": {
            "stepTypeId": 1,
            "stepTypeKey": "warmup",
            "displayOrder": 1
        },
        "childStepId": 0,
        "description": "5 Min Light Cardio (Treadmill, Bike, Rower)",
        "endCondition": {
            "conditionTypeId": 2,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True
        },
        "endConditionValue": 300.0,
        "preferredEndConditionUnit": None,
        "endConditionCompare": "",
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1
        },
        "targetValueOne": None,
        "targetValueTwo": None,
        "targetValueUnit": None,
        "zoneNumber": None,
        "secondaryTargetType": None,
        "secondaryTargetValueOne": None,
        "secondaryTargetValueTwo": None,
        "secondaryTargetValueUnit": None,
        "secondaryZoneNumber": None,
        "endConditionZone": None,
        "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
        "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0},
        "category": None,
        "exerciseName": None,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": -1.0,
        "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    }

def build_rest_step(time_sec: int = 60) -> dict:
    """Build a generic rest step."""
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": 0,
        "stepType": {
            "stepTypeId": 5,
            "stepTypeKey": "rest",
            "displayOrder": 5
        },
        "childStepId": 0,
        "description": None,
        "endCondition": {
            "conditionTypeId": 2,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True
        },
        "endConditionValue": float(time_sec),
        "preferredEndConditionUnit": None,
        "endConditionCompare": "",
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1
        },
        "targetValueOne": None,
        "targetValueTwo": None,
        "targetValueUnit": None,
        "zoneNumber": None,
        "secondaryTargetType": None,
        "secondaryTargetValueOne": None,
        "secondaryTargetValueTwo": None,
        "secondaryTargetValueUnit": None,
        "secondaryZoneNumber": None,
        "endConditionZone": None,
        "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
        "equipmentType": {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0},
        "category": None,
        "exerciseName": None,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": -1.0,
        "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    }

def build_repeat_group(sets: int, interval_step: dict, rest_step: dict) -> dict:
    """Wrap interval and rest into a RepeatGroupDTO."""
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": 0,
        "stepType": {
            "stepTypeId": 6,
            "stepTypeKey": "repeat",
            "displayOrder": 6
        },
        "childStepId": 0,
        "numberOfIterations": sets,
        "workoutSteps": [interval_step, rest_step],
        "endConditionValue": float(sets),
        "preferredEndConditionUnit": None,
        "endConditionCompare": None,
        "endCondition": {
            "conditionTypeId": 7,
            "conditionTypeKey": "iterations",
            "displayOrder": 7,
            "displayable": False
        },
        "skipLastRestStep": False,
        "smartRepeat": False
    }

def reindex_steps(workout_steps: list) -> list:
    """Re-index stepOrder and stepId continuously."""
    step_order = 1
    step_id = 1
    child_id = 1
    
    for block in workout_steps:
        block["stepOrder"] = step_order
        step_order += 1
        block["stepId"] = step_id
        step_id += 1
        block["childStepId"] = child_id
        
        if block.get("type") == "RepeatGroupDTO":
            for child in block.get("workoutSteps", []):
                child["stepOrder"] = step_order
                step_order += 1
                child["stepId"] = step_id
                step_id += 1
                child["childStepId"] = child_id
        child_id += 1
    return workout_steps

# Prefix used for all coach-created workouts, so we can find and delete them.
_COACH_PREFIX = "\U0001f3cb\ufe0f "  # 🏋️ emoji prefix

# Minimum working weight (kg) to trigger ramp-up sets.  Exercises below this
# threshold are typically light isolation movements that don't benefit from
# dedicated warm-up sets (NSCA Essentials of Strength Training, 4th ed.).
_RAMPUP_WEIGHT_THRESHOLD = 0.0


def _get_step_weight(step: dict) -> float:
    """Extract working weight from a step."""
    if step.get("type") == "RepeatGroupDTO":
        for child in step.get("workoutSteps", []):
            if child.get("stepType", {}).get("stepTypeKey") == "interval":
                w = child.get("weightValue")
                return float(w) if w is not None and w > 0 else 0.0
    elif step.get("type") == "ExecutableStepDTO":
        w = step.get("weightValue")
        return float(w) if w is not None and w > 0 else 0.0
    return 0.0


def _get_step_description(step: dict) -> str:
    """Extract the exercise description/name from a step."""
    if step.get("type") == "RepeatGroupDTO":
        for child in step.get("workoutSteps", []):
            if child.get("stepType", {}).get("stepTypeKey") == "interval":
                return child.get("description") or ""
    return step.get("description") or ""


def _build_rampup_steps(working_weight: float, description: str,
                        exercise_name: str = None, category: str = None) -> list[dict]:
    """Build a single ramp-up (warm-up) set for a compound exercise.

    User preference: 1 set at 50% of working weight for 8 reps.
    """
    rampup = []

    w = round(working_weight * 0.5 / 2.5) * 2.5  # round to nearest 2.5 kg
    w = max(w, 2.5)
    interval = build_generic_step(f"Warm-up: {description}", 8, w, exercise_name, category)
    rest = build_rest_step(60)
    rampup.append(build_repeat_group(1, interval, rest))

    return rampup


def compile_and_schedule(session: Session, payload: dict) -> bool:
    """Compile AI json modification into a real Garmin workout and push it."""
    # Validate + coerce the untrusted AI payload once. After this, all numeric
    # fields are real numbers and suggested_time is a valid HH:MM (or None).
    try:
        action = parse_action(payload)
    except Exception as e:
        logger.error("Invalid schedule_workout payload: %s", e)
        return False
    payload = action.model_dump()

    base_id = payload.get("base_workout_id")
    if not base_id:
        return False

    base_workout = session.query(Workout).filter_by(workout_id=base_id).first()
    if not base_workout:
        logger.error(f"Base workout {base_id} not found.")
        return False
        
    try:
        segments = json.loads(base_workout.steps_json)
        # Flatten all top level steps from segments into a single list
        base_steps = []
        for seg in segments:
            base_steps.extend(seg.get("workoutSteps", []))
    except Exception as e:
        logger.error(f"Failed to parse base workout JSON: {e}")
        return False
        
    working_steps = []
    
    # Map keep_and_modify modifications by index to preserve base workout order
    mod_map = {}
    add_new_mods = []
    
    for mod in payload.get("modifications", []):
        if mod.get("type") == "keep_and_modify":
            idx = mod.get("index")
            if idx is not None:
                mod_map[idx] = mod
        elif mod.get("type") == "add_new":
            add_new_mods.append(mod)

    # Iterate over base_steps so the original template order is strictly preserved
    for idx, base_step in enumerate(base_steps):
        if idx in mod_map:
            mod = mod_map[idx]
            step = json.loads(json.dumps(base_step))  # Deep copy
            
            # Values are pre-validated numbers or None (omitted -> keep base).
            new_sets = mod.get("new_sets")
            new_reps = mod.get("new_reps")
            new_weight = mod.get("new_weight_kg")

            # Update sets if RepeatGroup
            if step.get("type") == "RepeatGroupDTO" and new_sets is not None:
                step["numberOfIterations"] = new_sets
                step["endConditionValue"] = float(new_sets)

            # Find inner interval step and update reps/weight
            if step.get("type") == "RepeatGroupDTO":
                for child in step.get("workoutSteps", []):
                    if child.get("stepType", {}).get("stepTypeKey") == "interval":
                        if new_reps is not None:
                            child["endConditionValue"] = float(new_reps)
                        if new_weight is not None:
                            child["weightValue"] = float(new_weight)
            elif step.get("type") == "ExecutableStepDTO":
                if new_reps is not None:
                    step["endConditionValue"] = float(new_reps)
                if new_weight is not None:
                    step["weightValue"] = float(new_weight)
            working_steps.append(step)
            
    # Append any brand new exercises at the end
    for mod in add_new_mods:
        desc = mod.get("description", "Custom Exercise")
        sets = mod.get("sets", 1)
        reps = mod.get("reps", 10)
        weight = mod.get("weight_kg", 0)
        
        interval = build_generic_step(desc, reps, weight)
        rest = build_rest_step(60)
        working_steps.append(build_repeat_group(sets, interval, rest))

    # --- Insert ramp-up sets where warranted (NSCA guidelines) -----------
    # The first compound exercise (weight >= threshold) gets 1 ramp-up set;
    # subsequent heavy exercises get 1 ramp-up set too.
    new_steps = [build_cardio_warmup_step()]

    def _get_step_field(step: dict, field: str) -> str:
        if step.get("type") == "RepeatGroupDTO":
            for child in step.get("workoutSteps", []):
                if child.get("stepType", {}).get("stepTypeKey") == "interval":
                    return child.get(field)
        return step.get(field)

    seen_categories = set()

    for step in working_steps:
        weight = _get_step_weight(step)
        cat = _get_step_field(step, "category")
        
        # Only do a ramp-up if it's heavy AND we haven't warmed up this muscle group yet.
        if weight >= _RAMPUP_WEIGHT_THRESHOLD and cat and cat not in seen_categories:
            desc = _get_step_description(step)
            ex_name = _get_step_field(step, "exerciseName")
            rampups = _build_rampup_steps(weight, desc,
                                          exercise_name=ex_name, category=cat)
            new_steps.extend(rampups)
            seen_categories.add(cat)
            
        new_steps.append(step)

    # Re-index everything perfectly
    new_steps = reindex_steps(new_steps)

    # Build a descriptive workout name from the base workout name.
    # Format: "🏋️ Upper Body @ 15:30" — includes the suggested time so it
    # shows in calendar views, and the emoji prefix lets us identify and
    # delete previous coach-created workouts.
    base_name = base_workout.name or "Workout"
    suggested_time = payload.get("suggested_time", "")
    if suggested_time:
        workout_name = f"{_COACH_PREFIX}{base_name} @ {suggested_time}"
    else:
        workout_name = f"{_COACH_PREFIX}{base_name}"

    # Build the final payload wrapper
    garmin_payload = {
        "workoutName": workout_name,
        "sportType": {
            "sportTypeId": 5,
            "sportTypeKey": base_workout.sport_type,
            "displayOrder": 5
        },
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {
                    "sportTypeId": 5,
                    "sportTypeKey": base_workout.sport_type,
                    "displayOrder": 5
                },
                "workoutSteps": new_steps
            }
        ]
    }
    
    try:
        from db import SyncState
        client.login()
        
        # 1. Delete previous coach-created workout by ID directly (much faster).
        last_workout_row = session.get(SyncState, "last_coach_workout_id")
        if last_workout_row and last_workout_row.value:
            try:
                client.api.delete_workout(int(last_workout_row.value))
                logger.info("Deleted previous coach workout ID: %s", last_workout_row.value)
            except Exception as e:
                logger.warning("Failed to delete previous workout ID %s: %s", last_workout_row.value, e)
                
        # 2. Upload
        res = client.api.upload_workout(garmin_payload)
        new_id = res.get("workoutId")
        if not new_id:
            logger.error("Upload succeeded but no workoutId returned.")
            return False
            
        # 3. Schedule for today
        today_str = date.today().isoformat()
        client.api.schedule_workout(new_id, today_str)
        logger.info("Scheduled workout '%s' for %s (ID: %s)", workout_name, today_str, new_id)
        
        # 4. Save the new ID so we can delete it next time
        session.merge(SyncState(key="last_coach_workout_id", value=str(new_id)))

        # 5. Append event to the ICS calendar feed list so each workout
        #    appears on iCloud/Google calendar with the correct time.
        #    Estimate ~60 min for a typical strength session.
        existing_row = session.get(SyncState, "coach_calendar_events")
        existing_events = []
        if existing_row and existing_row.value:
            try:
                existing_events = json.loads(existing_row.value)
            except Exception:
                existing_events = []

        # Clean up events older than 7 days
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        existing_events = [e for e in existing_events if e.get("date", "") >= cutoff]

        # Add the new event
        existing_events.append({
            "title": workout_name,
            "date": today_str,
            "start_time": suggested_time or "18:30",
            "duration_min": 60,
        })
        session.merge(SyncState(key="coach_calendar_events", value=json.dumps(existing_events)))
        session.commit()
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to push workout to Garmin: {e}")
        return False
