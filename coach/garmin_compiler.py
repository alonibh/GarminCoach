import json
import logging
from datetime import date
from sqlalchemy.orm import Session

from db import Workout
from sync.garmin_client import client

logger = logging.getLogger(__name__)

def build_generic_step(description: str, reps: int, weight_kg: float) -> dict:
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
        "category": None,
        "exerciseName": None,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": weight_kg if weight_kg is not None and weight_kg > 0 else -1.0,
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

def compile_and_schedule(session: Session, payload: dict) -> bool:
    """Compile AI json modification into a real Garmin workout and push it."""
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
        
    new_steps = []
    
    for mod in payload.get("modifications", []):
        mod_type = mod.get("type")
        
        if mod_type == "keep_and_modify":
            idx = mod.get("index")
            if idx is not None and 0 <= idx < len(base_steps):
                step = json.loads(json.dumps(base_steps[idx]))  # Deep copy
                
                # Update sets if RepeatGroup
                if step.get("type") == "RepeatGroupDTO" and "new_sets" in mod:
                    sets = mod["new_sets"]
                    step["numberOfIterations"] = sets
                    step["endConditionValue"] = float(sets)
                    
                # Find inner interval step and update reps/weight
                if step.get("type") == "RepeatGroupDTO":
                    for child in step.get("workoutSteps", []):
                        if child.get("stepType", {}).get("stepTypeKey") == "interval":
                            if "new_reps" in mod:
                                child["endConditionValue"] = float(mod["new_reps"])
                            if "new_weight_kg" in mod:
                                child["weightValue"] = float(mod["new_weight_kg"])
                elif step.get("type") == "ExecutableStepDTO":
                    if "new_reps" in mod:
                        step["endConditionValue"] = float(mod["new_reps"])
                    if "new_weight_kg" in mod:
                        step["weightValue"] = float(mod["new_weight_kg"])
                new_steps.append(step)
                
        elif mod_type == "add_new":
            desc = mod.get("description", "Custom Exercise")
            sets = mod.get("sets", 1)
            reps = mod.get("reps", 10)
            weight = mod.get("weight_kg", 0)
            
            interval = build_generic_step(desc, reps, weight)
            rest = build_rest_step(60)
            new_steps.append(build_repeat_group(sets, interval, rest))

    # Re-index everything perfectly
    new_steps = reindex_steps(new_steps)

    # Build the final payload wrapper
    garmin_payload = {
        "workoutName": "Today's Workout",
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
        client.login()
        
        # 1. Delete existing "Today's Workout"
        workouts = client.api.get_workouts()
        for w in workouts:
            if w.get("workoutName") == "Today's Workout":
                client.api.delete_workout(w.get("workoutId"))
                
        # 2. Upload
        res = client.api.upload_workout(garmin_payload)
        new_id = res.get("workoutId")
        if not new_id:
            logger.error("Upload succeeded but no workoutId returned.")
            return False
            
        # 3. Schedule
        today_str = date.today().isoformat()
        client.api.schedule_workout(new_id, today_str)
        return True
        
    except Exception as e:
        logger.error(f"Failed to push workout to Garmin: {e}")
        return False
