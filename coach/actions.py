"""Validation schema for the AI coach's `schedule_workout` action.

The coach appends a JSON block at the end of its reply to push a workout to the
watch. That JSON is model-generated and therefore untrusted: fields can be the
wrong type, missing, or malformed. These Pydantic models validate and coerce it
once, so the compiler downstream can assume clean data.
"""
import re
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # strict HH:MM 24-hour


class KeepAndModify(BaseModel):
    type: Literal["keep_and_modify"]
    index: int = Field(ge=0)
    new_sets: Optional[int] = Field(default=None, ge=1)
    new_reps: Optional[float] = Field(default=None, ge=0)
    new_weight_kg: Optional[float] = Field(default=None, ge=0)


class AddNew(BaseModel):
    type: Literal["add_new"]
    description: str = "Custom Exercise"
    sets: int = Field(default=1, ge=1)
    reps: float = Field(default=10, ge=0)
    weight_kg: float = Field(default=0, ge=0)


class ScheduleWorkoutAction(BaseModel):
    action: Literal["schedule_workout"]
    base_workout_id: int
    suggested_time: Optional[str] = None
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


def parse_action(raw: dict) -> ScheduleWorkoutAction:
    """Validate a raw action dict. Raises pydantic.ValidationError on bad data."""
    return ScheduleWorkoutAction.model_validate(raw)
