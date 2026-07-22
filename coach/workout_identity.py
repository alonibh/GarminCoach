"""Identify coach-created Garmin workouts without relying on display names."""
from __future__ import annotations

from sqlalchemy.orm import Query, Session

from db import PlannedSession, SyncState, Workout


# Older coach-created workouts used this display prefix. Keep recognizing it so
# existing synced data remains hidden from the user's reusable workout templates.
LEGACY_COACH_WORKOUT_PREFIX = "\U0001f3cb\ufe0f "


def user_workouts_query(session: Session) -> Query:
    """Return Garmin workouts that were not created by GarminCoach."""
    query = session.query(Workout).filter(
        ~Workout.name.startswith(LEGACY_COACH_WORKOUT_PREFIX)
    )

    planned_ids = (
        session.query(PlannedSession.garmin_workout_id)
        .filter(PlannedSession.garmin_workout_id.isnot(None))
    )
    query = query.filter(~Workout.workout_id.in_(planned_ids))

    # The older base-workout scheduling path tracked only its latest generated
    # workout ID rather than creating a PlannedSession row.
    last_created = session.get(SyncState, "last_coach_workout_id")
    if last_created and last_created.value:
        try:
            query = query.filter(Workout.workout_id != int(last_created.value))
        except (TypeError, ValueError):
            pass

    return query
