import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from contextvars import ContextVar
import os
from typing import Sequence
from sqlalchemy.orm import Session
from garminconnect import GarminConnectAuthenticationError

from db import PlannedSession, ProgramSession, SessionExercise, SyncState, Workout
from sync.garmin_registry import current_garmin_client
from coach.actions import parse_action

logger = logging.getLogger(__name__)

WARMUP_REST_SECONDS = 60


class GarminFailureKind(str, Enum):
    RECONNECT_REQUIRED = "reconnect_required"
    VERIFY_REJECTED = "verify_rejected"
    SCHEDULE_FAILED = "schedule_failed"
    SERVICE_FAILED = "service_failed"


@dataclass(frozen=True)
class GarminScheduleResult:
    ok: bool
    failure: GarminFailureKind | None = None
    stage: str | None = None
    exception_type: str | None = None

    @property
    def user_message(self) -> str:
        if self.failure == GarminFailureKind.RECONNECT_REQUIRED:
            return "Garmin is no longer connected. Reconnect Garmin and try again."
        if self.failure == GarminFailureKind.VERIFY_REJECTED:
            return "Garmin could not verify the uploaded workout. Nothing was scheduled."
        if self.failure == GarminFailureKind.SCHEDULE_FAILED:
            return "Garmin could not schedule the workout. Nothing was changed locally."
        return "Garmin scheduling failed. No session was scheduled."


_last_schedule_result: ContextVar[GarminScheduleResult | None] = ContextVar(
    "last_garmin_schedule_result",
    default=None,
)


def _ensure_authenticated(garmin_client) -> None:
    ensure = getattr(garmin_client, "ensure_authenticated", None)
    if ensure is not None:
        ensure()
    else:
        # Compatibility for old single-user adapters and deterministic fakes.
        garmin_client.login()


def _failed_result(
    stage: str,
    exc: Exception,
    *,
    operation: str = "schedule_program_session",
) -> GarminScheduleResult:
    if isinstance(exc, GarminConnectAuthenticationError):
        failure = GarminFailureKind.RECONNECT_REQUIRED
    elif stage in {"read_back", "verify"}:
        failure = GarminFailureKind.VERIFY_REJECTED
    elif stage == "schedule":
        failure = GarminFailureKind.SCHEDULE_FAILED
    else:
        failure = GarminFailureKind.SERVICE_FAILED
    logger.error(
        "garmin_mutation_failed operation=%s stage=%s exception_type=%s",
        operation,
        stage,
        type(exc).__name__,
    )
    return GarminScheduleResult(
        ok=False,
        failure=failure,
        stage=stage,
        exception_type=type(exc).__name__,
    )

def build_generic_step(description: str, reps: int | None, weight_kg: float | None, exercise_name: str = None, category: str = None, duration_seconds: int | None = None, step_type: str = "interval") -> dict:
    """Build a generic interval step."""
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": 0,  # Will be re-indexed later
        "stepType": {
            "stepTypeId": 1 if step_type == "warmup" else 3,
            "stepTypeKey": step_type,
            "displayOrder": 1 if step_type == "warmup" else 3
        },
        "childStepId": 0,
        "description": description,
        "endCondition": {
            "conditionTypeId": 2 if duration_seconds else 10,
            "conditionTypeKey": "time" if duration_seconds else "reps",
            "displayOrder": 2 if duration_seconds else 10,
            "displayable": True
        },
        "endConditionValue": float(duration_seconds or reps or 1),
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


@dataclass(frozen=True)
class StraightExerciseBlock:
    exercise: SessionExercise


ExecutionBlock = StraightExerciseBlock


def build_execution_blocks(exercises: Sequence[SessionExercise]) -> tuple[ExecutionBlock, ...]:
    """Return validated, deterministic sequential execution blocks without mutating rows."""
    ordered = sorted(exercises, key=lambda exercise: (exercise.order_index, exercise.id or 0))
    return tuple(StraightExerciseBlock(exercise) for exercise in ordered)

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


def build_program_workout(
    session: Session,
    program_session_id: int,
    suggested_time: str = "",
    *,
    require_active: bool = True,
) -> dict:
    """Compile one program session into a standalone Garmin workout.

    Draft sessions use ``require_active=False`` for the same local preflight
    validation that is used before an upload is permitted.
    """
    planned = session.get(ProgramSession, program_session_id)
    if not planned or not planned.program or (require_active and not planned.program.active):
        raise ValueError("Program session is not active")
    exercises = (
        session.query(SessionExercise)
        .filter_by(program_session_id=program_session_id)
        .order_by(SessionExercise.order_index, SessionExercise.id)
        .all()
    )
    if not exercises:
        raise ValueError("Program session has no exercises")
    blocks = build_execution_blocks(exercises)
    steps = []
    for block_index, block in enumerate(blocks):
        exercise = block.exercise
        if exercise.warmup_enabled:
            steps.append(build_generic_step(
                f"Warm-up: {exercise.exercise_name}", exercise.warmup_reps,
                exercise.warmup_weight_kg, exercise.garmin_name, exercise.garmin_category,
                exercise.warmup_duration_seconds, "warmup",
            ))
            steps.append(build_rest_step(WARMUP_REST_SECONDS))
        group = build_repeat_group(
            exercise.sets or 1,
            build_generic_step(exercise.exercise_name, exercise.reps, exercise.weight_kg,
                               exercise.garmin_name, exercise.garmin_category, exercise.duration_seconds),
            build_rest_step(exercise.rest_seconds),
        )
        steps.append(group)
    nested_steps = sum(1 + len(step.get("workoutSteps", [])) for step in steps)
    if nested_steps > 50:
        raise ValueError(f"Workout has {nested_steps} steps; Garmin limit is 50")
    name = planned.name + (f" @ {suggested_time}" if suggested_time else "")
    sport = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}
    return {"workoutName": name, "sportType": sport, "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": reindex_steps(steps)}]}


def _verify_uploaded_workout(expected: dict, uploaded: dict) -> None:
    expected_steps = expected["workoutSegments"][0]["workoutSteps"]
    actual_segments = uploaded.get("workoutSegments") or []
    actual_steps = actual_segments[0].get("workoutSteps", []) if actual_segments else []
    if len(actual_steps) != len(expected_steps):
        raise ValueError("Garmin read-back step count does not match")
    def signature(step: dict) -> tuple:
        children = tuple(signature(child) for child in step.get("workoutSteps", []))
        condition = step.get("endCondition") or {}
        return (
            step.get("type"), step.get("numberOfIterations"),
            step.get("skipLastRestStep"),
            (step.get("stepType") or {}).get("stepTypeKey"), step.get("description"),
            condition.get("conditionTypeKey"), float(step.get("endConditionValue") or 0),
            float(step.get("weightValue") or -1), step.get("category"), step.get("exerciseName"),
            children,
        )
    if tuple(map(signature, actual_steps)) != tuple(map(signature, expected_steps)):
        raise ValueError("Garmin read-back workout details do not match the approved session")


def _schedule_program_session_result(
    session: Session, meta: dict
) -> GarminScheduleResult:
    target_day = date.fromisoformat(meta["target_date"])
    duplicate = session.query(PlannedSession).filter_by(
        program_session_id=meta["program_session_id"], target_date=target_day,
        status="approved",
    ).first()
    if duplicate and duplicate.garmin_workout_id:
        return GarminScheduleResult(ok=True)
    workout = build_program_workout(session, meta["program_session_id"], meta["suggested_time"])
    new_id = None
    garmin_client = None
    stage = "authenticate"
    try:
        with current_garmin_client() as garmin_client:
            _ensure_authenticated(garmin_client)
            stage = "upload"
            result = garmin_client.api.upload_workout(workout)
            new_id = result.get("workoutId")
            if not new_id:
                raise ValueError("Garmin upload returned no workout ID")
            stage = "read_back"
            reader = (
                getattr(garmin_client.api, "get_workout_by_id", None)
                or getattr(garmin_client.api, "get_workout", None)
            )
            if not reader:
                raise ValueError("Installed Garmin client cannot read a workout back")
            uploaded = reader(new_id)
            stage = "verify"
            _verify_uploaded_workout(workout, uploaded)
            stage = "schedule"
            garmin_client.api.schedule_workout(new_id, meta["target_date"])
            stage = "persist"
            session.add(PlannedSession(
                program_session_id=meta["program_session_id"], activity_type=meta["activity_type"],
                title=meta["title"], target_date=target_day, suggested_time=meta["suggested_time"],
                duration_min=meta["duration_min"], intensity=meta["intensity"], status="approved",
                garmin_workout_id=new_id, source="coach", created_at=datetime.now(), updated_at=datetime.now(),
            ))
            events_row = session.get(SyncState, "coach_calendar_events")
            events = json.loads(events_row.value) if events_row and events_row.value else []
            events.append({"title": meta["title"], "date": meta["target_date"], "start_time": meta["suggested_time"] or "18:30", "duration_min": meta["duration_min"]})
            session.merge(SyncState(key="coach_calendar_events", value=json.dumps(events)))
            session.commit()
        return GarminScheduleResult(ok=True)
    except Exception as exc:
        session.rollback()
        if isinstance(exc, GarminConnectAuthenticationError) and garmin_client is not None:
            marker = getattr(garmin_client, "mark_session_expired", None)
            if marker is not None:
                marker()
        if new_id and garmin_client is not None:
            try:
                with current_garmin_client() as cleanup_client:
                    cleanup_client.api.delete_workout(new_id)
            except Exception as cleanup_exc:
                logger.error(
                    "garmin_mutation_failed operation=schedule_program_session "
                    "stage=cleanup exception_type=%s",
                    type(cleanup_exc).__name__,
                )
        return _failed_result(stage, exc)


def _schedule_program_session(session: Session, meta: dict) -> bool:
    """Compatibility wrapper for callers that still consume a boolean."""
    result = _schedule_program_session_result(session, meta)
    _last_schedule_result.set(result)
    return result.ok

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


def compile_and_schedule_result(
    session: Session, payload: dict
) -> GarminScheduleResult:
    """Return a classified result for user-confirmed scheduling operations."""
    try:
        action = parse_action(payload)
    except Exception as exc:
        logger.error(
            "garmin_mutation_failed operation=compile_and_schedule stage=validate "
            "exception_type=%s",
            type(exc).__name__,
        )
        return GarminScheduleResult(
            ok=False,
            failure=GarminFailureKind.SERVICE_FAILED,
            stage="validate",
            exception_type=type(exc).__name__,
        )
    parsed = action.model_dump()
    if (
        parsed.get("action") == "schedule_session"
        and parsed.get("program_session_id")
        and not parsed.get("base_workout_id")
    ):
        return _schedule_program_session_result(
            session,
            {
                "program_session_id": parsed["program_session_id"],
                "activity_type": parsed.get("activity_type") or "general",
                "title": parsed.get("title") or "Workout",
                "target_date": parsed.get("target_date"),
                "suggested_time": parsed.get("suggested_time") or "",
                "duration_min": parsed.get("duration_min") or 60,
                "intensity": parsed.get("intensity") or "normal",
            },
        )
    if compile_and_schedule(session, parsed):
        return GarminScheduleResult(ok=True)
    return GarminScheduleResult(
        ok=False,
        failure=GarminFailureKind.SERVICE_FAILED,
        stage="service",
        exception_type="GarminOperationError",
    )


def compile_and_schedule_for_interaction(
    session: Session, payload: dict
) -> GarminScheduleResult:
    """Classify the legacy boolean API while keeping monkeypatch compatibility."""
    _last_schedule_result.set(None)
    if compile_and_schedule(session, payload):
        return GarminScheduleResult(ok=True)
    return _last_schedule_result.get() or GarminScheduleResult(
        ok=False,
        failure=GarminFailureKind.SERVICE_FAILED,
        stage="service",
        exception_type="GarminOperationError",
    )


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
    planned_meta = None

    if payload.get("action") == "schedule_session":
        planned_meta = {
            "program_session_id": payload.get("program_session_id"),
            "activity_type": payload.get("activity_type") or "general",
            "title": payload.get("title") or "Workout",
            "target_date": payload.get("target_date"),
            "suggested_time": payload.get("suggested_time") or "",
            "duration_min": payload.get("duration_min") or 60,
            "intensity": payload.get("intensity") or "normal",
        }
        if payload.get("program_session_id") and not payload.get("base_workout_id"):
            return _schedule_program_session(session, planned_meta)
        if not payload.get("base_workout_id"):
            try:
                target_day = date.fromisoformat(planned_meta["target_date"])
            except Exception:
                logger.error("Invalid target_date for calendar-only session: %s", planned_meta["target_date"])
                return False

            session.add(
                PlannedSession(
                    program_session_id=planned_meta["program_session_id"],
                    activity_type=planned_meta["activity_type"],
                    title=planned_meta["title"],
                    target_date=target_day,
                    suggested_time=planned_meta["suggested_time"],
                    duration_min=planned_meta["duration_min"],
                    intensity=planned_meta["intensity"],
                    status="approved",
                    source="coach",
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )

            existing_row = session.get(SyncState, "coach_calendar_events")
            existing_events = []
            if existing_row and existing_row.value:
                try:
                    existing_events = json.loads(existing_row.value)
                except Exception:
                    existing_events = []
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            existing_events = [e for e in existing_events if e.get("date", "") >= cutoff]
            existing_events.append({
                "title": planned_meta["title"],
                "date": planned_meta["target_date"],
                "start_time": planned_meta["suggested_time"] or "18:30",
                "duration_min": planned_meta["duration_min"],
            })
            session.merge(SyncState(key="coach_calendar_events", value=json.dumps(existing_events)))
            session.commit()
            return True

        payload = {
            "action": "schedule_workout",
            "base_workout_id": payload.get("base_workout_id"),
            "target_date": payload.get("target_date"),
            "suggested_time": payload.get("suggested_time"),
            "modifications": payload.get("modifications") or [],
        }

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

    # --- Insert cardio warm-up set (NSCA guidelines) -----------
    new_steps = [build_cardio_warmup_step()]
    new_steps.extend(working_steps)

    # Re-index everything perfectly
    new_steps = reindex_steps(new_steps)

    # Build a descriptive workout name from the base workout name.
    # Include the suggested time so the workout is recognizable in Garmin
    # calendar views. Coach-created workouts are identified by stored Garmin
    # workout IDs, not by altering the user-facing name.
    base_name = base_workout.name or "Workout"
    suggested_time = payload.get("suggested_time", "")
    if suggested_time:
        workout_name = f"{base_name} @ {suggested_time}"
    else:
        workout_name = base_name

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
    
    new_id = None
    garmin_client = None
    stage = "authenticate"
    try:
        with current_garmin_client() as garmin_client:
            _ensure_authenticated(garmin_client)
            last_workout_row = session.get(SyncState, "last_coach_workout_id")

            stage = "upload"
            res = garmin_client.api.upload_workout(garmin_payload)
            new_id = res.get("workoutId")
            if not new_id:
                raise ValueError("Garmin upload returned no workout ID")

            stage = "read_back"
            reader = (
                getattr(garmin_client.api, "get_workout_by_id", None)
                or getattr(garmin_client.api, "get_workout", None)
            )
            if not reader:
                raise ValueError("Installed Garmin client cannot read a workout back")
            uploaded = reader(new_id)
            stage = "verify"
            _verify_uploaded_workout(garmin_payload, uploaded)

            target_date_str = payload.get("target_date")
            if target_date_str:
                target_str = target_date_str
            else:
                from time_utils import get_local_now, get_local_date
                if get_local_now().hour >= 17:
                    target_date = get_local_date() + timedelta(days=1)
                else:
                    target_date = get_local_date()
                target_str = target_date.isoformat()

            stage = "schedule"
            garmin_client.api.schedule_workout(new_id, target_str)

            # Retire the previous coach workout only after its replacement is
            # verified and scheduled, so a failed replacement cannot erase it.
            if last_workout_row and last_workout_row.value:
                try:
                    old_id = int(last_workout_row.value)
                    if old_id != new_id:
                        garmin_client.api.delete_workout(old_id)
                except Exception as cleanup_exc:
                    logger.warning(
                        "garmin_mutation_failed operation=schedule_modified_workout "
                        "stage=cleanup_previous exception_type=%s",
                        type(cleanup_exc).__name__,
                    )

            stage = "persist"
            session.merge(SyncState(key="last_coach_workout_id", value=str(new_id)))

            existing_row = session.get(SyncState, "coach_calendar_events")
            existing_events = []
            if existing_row and existing_row.value:
                try:
                    existing_events = json.loads(existing_row.value)
                except Exception:
                    existing_events = []

            cutoff = (date.today() - timedelta(days=7)).isoformat()
            existing_events = [e for e in existing_events if e.get("date", "") >= cutoff]

            existing_events.append({
                "title": workout_name,
                "date": target_str,
                "start_time": suggested_time or "18:30",
                "duration_min": planned_meta["duration_min"] if planned_meta else 60,
            })
            session.merge(SyncState(key="coach_calendar_events", value=json.dumps(existing_events)))

            if planned_meta:
                session.add(
                    PlannedSession(
                        program_session_id=planned_meta["program_session_id"],
                        activity_type=planned_meta["activity_type"],
                        title=planned_meta["title"] or base_name,
                        target_date=date.fromisoformat(target_str),
                        suggested_time=suggested_time or "18:30",
                        duration_min=planned_meta["duration_min"],
                        intensity=planned_meta["intensity"],
                        status="approved",
                        garmin_workout_id=new_id,
                        source="coach",
                        created_at=datetime.now(),
                        updated_at=datetime.now(),
                    )
                )
            session.commit()
        return True

    except Exception as exc:
        session.rollback()
        if isinstance(exc, GarminConnectAuthenticationError) and garmin_client is not None:
            marker = getattr(garmin_client, "mark_session_expired", None)
            if marker is not None:
                marker()
        if new_id and garmin_client is not None:
            try:
                with current_garmin_client() as cleanup_client:
                    cleanup_client.api.delete_workout(new_id)
            except Exception as cleanup_exc:
                logger.error(
                    "garmin_mutation_failed operation=schedule_modified_workout "
                    "stage=cleanup exception_type=%s",
                    type(cleanup_exc).__name__,
                )
        _last_schedule_result.set(
            _failed_result(
                stage,
                exc,
                operation="schedule_modified_workout",
            )
        )
        return False
