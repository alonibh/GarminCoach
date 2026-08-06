"""Safe, reusable Garmin template for the fixed Active Recovery walk.

This module deliberately creates a library template only. It never schedules
the template or changes program, decision, calendar, or interaction state.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import threading
from typing import Any, Mapping

from garminconnect import GarminConnectAuthenticationError
try:
    from garminconnect import GarminConnectNotFoundError
except ImportError:
    # garminconnect<0.3 does not export this error; provide a shim so the
    # except-clause in sync logic compiles.  In a pre-0.3 runtime the except
    # block simply never matches (the error is never raised by the older
    # library), which is the same safe behaviour as "not found → upload new".
    class GarminConnectNotFoundError(Exception):  # type: ignore[no-redef]
        """Compatibility shim for environments that lack garminconnect>=0.3."""
try:
    from garminconnect.workout import ExecutableStep, WalkingWorkout, WorkoutSegment
except ImportError as _gc_import_err:
    raise ImportError(
        "garminconnect.workout is required; install garminconnect[typed]>=0.3"
    ) from _gc_import_err
from sqlalchemy.orm import Session

from db import SyncState
from sync.garmin_registry import current_garmin_client


logger = logging.getLogger(__name__)

ACTIVE_RECOVERY_WORKOUT_NAME = "Active Recovery — 30 Minute Walk"
ACTIVE_RECOVERY_DURATION_SECONDS = 1_800
ACTIVE_RECOVERY_TEMPLATE_VERSION = "v1"
ACTIVE_RECOVERY_SYNC_STATE_KEY = "active_recovery_workout_id_v1"

_WALKING_SPORT = {"sportTypeId": 17, "sportTypeKey": "walking", "displayOrder": 17}
_INTERVAL_STEP_TYPE = {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}
_TIME_END_CONDITION = {
    "conditionTypeId": 2,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}
_NO_TARGET = {
    "workoutTargetTypeId": 1,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}
_TEMPLATE_LOCK = threading.RLock()


class ActiveRecoveryFailureKind(str, Enum):
    """Failure categories for the template-only Garmin mutation."""

    RECONNECT_REQUIRED = "reconnect_required"
    INVALID_STORED_ID = "invalid_stored_id"
    VERIFY_REJECTED = "verify_rejected"
    SERVICE_FAILED = "service_failed"


@dataclass(frozen=True)
class ActiveRecoveryTemplateResult:
    """The outcome of ensuring the canonical remote template exists."""

    ok: bool
    workout_id: int | None = None
    created: bool = False
    failure: ActiveRecoveryFailureKind | None = None
    stage: str | None = None
    exception_type: str | None = None


def build_active_recovery_workout() -> WalkingWorkout:
    """Build the canonical 30-minute, untargeted walking workout.

    The installed Garmin DTO calls its only executable step an ``interval``;
    it is nevertheless one uninterrupted time-ended walking step.
    """
    step = ExecutableStep(
        stepOrder=1,
        stepType=dict(_INTERVAL_STEP_TYPE),
        endCondition=dict(_TIME_END_CONDITION),
        endConditionValue=float(ACTIVE_RECOVERY_DURATION_SECONDS),
        targetType=dict(_NO_TARGET),
    )
    return WalkingWorkout(
        workoutName=ACTIVE_RECOVERY_WORKOUT_NAME,
        estimatedDurationInSecs=ACTIVE_RECOVERY_DURATION_SECONDS,
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=dict(_WALKING_SPORT),
                workoutSteps=[step],
            )
        ],
    )


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, Mapping):
            return dumped
    raise ValueError(f"Active Recovery {label} is not a mapping")


def _is_exact_walking_sport(value: Any) -> bool:
    return isinstance(value, Mapping) and (
        value.get("sportTypeId") == 17 and value.get("sportTypeKey") == "walking"
    )


def _is_no_target(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, Mapping) and (
        value.get("workoutTargetTypeId") == 1
        and value.get("workoutTargetTypeKey") == "no.target"
    )


def _is_exact_duration(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and value == ACTIVE_RECOVERY_DURATION_SECONDS


def verify_active_recovery_workout(uploaded: Any) -> None:
    """Reject every read-back shape except the safe canonical semantics.

    Garmin-owned IDs, ordering metadata, ownership/provider fields, and
    timestamps are ignored. All workout execution semantics are checked.
    """
    workout = _as_mapping(uploaded, "workout")
    if workout.get("workoutName") != ACTIVE_RECOVERY_WORKOUT_NAME:
        raise ValueError("Active Recovery workout name is not canonical")
    if not _is_exact_walking_sport(workout.get("sportType")):
        raise ValueError("Active Recovery workout sport is not walking")

    if "estimatedDurationInSecs" in workout and not _is_exact_duration(workout["estimatedDurationInSecs"]):
        raise ValueError("Active Recovery estimated duration is not 1800 seconds")
    segments = workout.get("workoutSegments")
    if not isinstance(segments, list) or len(segments) != 1:
        raise ValueError("Active Recovery must contain exactly one segment")
    segment = _as_mapping(segments[0], "segment")
    if not _is_exact_walking_sport(segment.get("sportType")):
        raise ValueError("Active Recovery segment sport is not walking")
    steps = segment.get("workoutSteps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise ValueError("Active Recovery must contain exactly one executable step")

    step = _as_mapping(steps[0], "step")
    if step.get("type") != "ExecutableStepDTO":
        raise ValueError("Active Recovery step is not executable")
    if step.get("workoutSteps"):
        raise ValueError("Active Recovery must not contain nested steps")
    if any(step.get(key) is not None for key in ("numberOfIterations", "repeatCount")):
        raise ValueError("Active Recovery must not contain a repeat group")
    step_type = step.get("stepType")
    if not isinstance(step_type, Mapping) or (
        step_type.get("stepTypeId") != 3 or step_type.get("stepTypeKey") != "interval"
    ):
        raise ValueError("Active Recovery step must be one continuous interval")
    end_condition = step.get("endCondition")
    if not isinstance(end_condition, Mapping) or (
        end_condition.get("conditionTypeId") != 2
        or end_condition.get("conditionTypeKey") != "time"
    ):
        raise ValueError("Active Recovery step must be time-ended")
    if not _is_exact_duration(step.get("endConditionValue")):
        raise ValueError("Active Recovery step duration is not 1800 seconds")
    if not _is_no_target(step.get("targetType")):
        raise ValueError("Active Recovery step has a target")
    target_value_fields = (
        "targetValueOne", "targetValueTwo", "targetValueUnit", "zoneNumber",
        "secondaryTargetType", "secondaryTargetValueOne", "secondaryTargetValueTwo",
        "secondaryTargetValueUnit", "secondaryZoneNumber", "endConditionZone",
    )
    if any(step.get(key) is not None for key in target_value_fields):
        raise ValueError("Active Recovery step has target values or zones")
    forbidden_target_fragments = (
        "heart", "pace", "speed", "distance", "cadence", "power", "calorie",
    )
    if any(
        value is not None
        and any(fragment in key.lower() for fragment in forbidden_target_fragments)
        for key, value in step.items()
    ):
        raise ValueError("Active Recovery step has a prohibited performance target")


def _positive_workout_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _stored_workout_id(session: Session) -> tuple[int | None, bool]:
    row = session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY)
    if row is None:
        return None, False
    return _positive_workout_id(row.value), True


def _failure_for(stage: str, exc: Exception) -> ActiveRecoveryFailureKind:
    if isinstance(exc, GarminConnectAuthenticationError):
        return ActiveRecoveryFailureKind.RECONNECT_REQUIRED
    if stage == "verify":
        return ActiveRecoveryFailureKind.VERIFY_REJECTED
    return ActiveRecoveryFailureKind.SERVICE_FAILED


def _failed_result(stage: str, exc: Exception) -> ActiveRecoveryTemplateResult:
    logger.error(
        "garmin_mutation_failed operation=ensure_active_recovery_workout stage=%s exception_type=%s",
        stage,
        type(exc).__name__,
    )
    return ActiveRecoveryTemplateResult(
        ok=False,
        failure=_failure_for(stage, exc),
        stage=stage,
        exception_type=type(exc).__name__,
    )


def _mark_session_expired(garmin_client: Any, exc: Exception) -> None:
    if isinstance(exc, GarminConnectAuthenticationError):
        marker = getattr(garmin_client, "mark_session_expired", None)
        if callable(marker):
            marker()


def ensure_active_recovery_workout(session: Session) -> ActiveRecoveryTemplateResult:
    """Create or safely reuse the canonical remote walking template.

    The process-local lock matches the supported single-worker deployment.
    This function never schedules the template and changes only its versioned
    ``SyncState`` row after read-back verification succeeds.
    """
    with _TEMPLATE_LOCK:
        stored_id, has_stored_key = _stored_workout_id(session)
        if has_stored_key and stored_id is None:
            exc = ValueError("Active Recovery stored workout ID is invalid")
            logger.error(
                "garmin_mutation_failed operation=ensure_active_recovery_workout "
                "stage=stored_id exception_type=%s",
                type(exc).__name__,
            )
            return ActiveRecoveryTemplateResult(
                ok=False,
                failure=ActiveRecoveryFailureKind.INVALID_STORED_ID,
                stage="stored_id",
                exception_type=type(exc).__name__,
            )

        garmin_client: Any = None
        new_id: int | None = None
        stage = "authenticate"
        try:
            with current_garmin_client() as garmin_client:
                ensure = getattr(garmin_client, "ensure_authenticated", None)
                if callable(ensure):
                    ensure()
                else:
                    garmin_client.login()
                api = garmin_client.api

                if stored_id is not None:
                    stage = "read_back"
                    try:
                        uploaded = api.get_workout_by_id(stored_id)
                    except GarminConnectNotFoundError:
                        # The only existing-ID failure that may create a replacement.
                        pass
                    else:
                        stage = "verify"
                        verify_active_recovery_workout(uploaded)
                        return ActiveRecoveryTemplateResult(ok=True, workout_id=stored_id, created=False)

                stage = "upload"
                upload_result = api.upload_walking_workout(build_active_recovery_workout())
                if not isinstance(upload_result, Mapping):
                    raise ValueError("Garmin upload returned a non-mapping result")
                new_id = _positive_workout_id(upload_result.get("workoutId"))
                if new_id is None:
                    raise ValueError("Garmin upload returned no positive workout ID")
                stage = "read_back"
                uploaded = api.get_workout_by_id(new_id)
                stage = "verify"
                verify_active_recovery_workout(uploaded)
                stage = "persist"
                session.merge(SyncState(key=ACTIVE_RECOVERY_SYNC_STATE_KEY, value=str(new_id)))
                session.commit()
                return ActiveRecoveryTemplateResult(ok=True, workout_id=new_id, created=True)
        except Exception as exc:
            if new_id is not None:
                session.rollback()
            _mark_session_expired(garmin_client, exc)
            if new_id is not None:
                try:
                    with current_garmin_client() as cleanup_client:
                        cleanup_client.api.delete_workout(new_id)
                except Exception as cleanup_exc:
                    logger.error(
                        "garmin_mutation_failed operation=ensure_active_recovery_workout "
                        "stage=cleanup exception_type=%s",
                        type(cleanup_exc).__name__,
                    )
            return _failed_result(stage, exc)
