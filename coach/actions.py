"""Validation schema for the AI coach's `schedule_workout` action.

The coach appends a JSON block at the end of its reply to push a workout to the
watch. That JSON is model-generated and therefore untrusted: fields can be the
wrong type, missing, or malformed. These Pydantic models validate and coerce it
once, so the compiler downstream can assume clean data.
"""
import re
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # strict HH:MM 24-hour
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class KeepAndModify(BaseModel):
    type: Literal["keep_and_modify"]
    index: int = Field(ge=0)
    new_sets: Optional[int] = Field(default=None, ge=1, le=10)
    new_reps: Optional[float] = Field(default=None, ge=0, le=100)
    new_weight_kg: Optional[float] = Field(default=None, ge=0, le=300)


class AddNew(BaseModel):
    type: Literal["add_new"]
    description: str = "Custom Exercise"
    sets: int = Field(default=1, ge=1, le=10)
    reps: float = Field(default=10, ge=0, le=100)
    weight_kg: float = Field(default=0, ge=0, le=300)


class ScheduleWorkoutAction(BaseModel):
    action: Literal["schedule_workout"]
    base_workout_id: int
    target_date: Optional[str] = Field(default=None, description="The date to schedule the workout (YYYY-MM-DD). Use this if the user asks to reschedule to a specific day.")
    suggested_time: Optional[str] = Field(default=None, description="The time of day in HH:MM format.")
    # Discriminated by `type`; unknown shapes raise rather than silently passing.
    modifications: List[Union[KeepAndModify, AddNew]] = Field(default_factory=list)

    @field_validator("suggested_time")
    @classmethod
    def _valid_time(cls, v):
        if v is None or v == "":
            return None
        if not _TIME_RE.match(v.strip()):
            raise ValueError(f"suggested_time must be HH:MM (24-hour), got {v!r}")
        return v.strip()


class ScheduleSessionAction(BaseModel):
    action: Literal["schedule_session"]
    program_session_id: Optional[int] = Field(default=None, ge=1)
    activity_type: str = Field(default="general", min_length=1, max_length=64)
    title: str = Field(default="Workout", min_length=1, max_length=255)
    base_workout_id: Optional[int] = Field(default=None, ge=1)
    target_date: str = Field(description="The date to schedule the session (YYYY-MM-DD).")
    suggested_time: Optional[str] = Field(default=None, description="The time of day in HH:MM format.")
    duration_min: int = Field(default=60, ge=5, le=480)
    intensity: Literal["recovery", "light", "normal", "hard", "race"] = "normal"
    modifications: List[Union[KeepAndModify, AddNew]] = Field(default_factory=list)

    @field_validator("target_date")
    @classmethod
    def _valid_date(cls, v):
        if not _DATE_RE.match((v or "").strip()):
            raise ValueError(f"target_date must be YYYY-MM-DD, got {v!r}")
        return v.strip()

    @field_validator("suggested_time")
    @classmethod
    def _valid_time(cls, v):
        if v is None or v == "":
            return None
        if not _TIME_RE.match(v.strip()):
            raise ValueError(f"suggested_time must be HH:MM (24-hour), got {v!r}")
        return v.strip()


ActionPayload = Union[ScheduleWorkoutAction, ScheduleSessionAction]
_ACTION_ADAPTER = TypeAdapter(ActionPayload)


def parse_action(raw: dict) -> ActionPayload:
    """Validate a raw action dict. Raises pydantic.ValidationError on bad data."""
    return _ACTION_ADAPTER.validate_python(raw)
