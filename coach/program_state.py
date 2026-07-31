"""Deterministic rolling program state and synced-activity reconciliation."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from coach.exercises import exercise_key
from coach.planned_session_status import INACTIVE_ORIGINAL_SESSION_STATUSES
from coach.program_policy import PROGRAM_POLICIES, ProgramPolicy
from db import (
    Activity,
    ActivityProgramMatch,
    ExerciseSet,
    PlannedSession,
    ProgramCursor,
    ProgramSession,
    SessionExercise,
    TrainingProgram,
)


def catalog_key_for_program(program: TrainingProgram) -> str | None:
    try:
        tags = json.loads(program.goal_tags or "[]")
    except (TypeError, ValueError):
        tags = []
    for tag in tags if isinstance(tags, list) else []:
        if tag in PROGRAM_POLICIES:
            return tag
    matches = [key for key, policy in PROGRAM_POLICIES.items() if policy.source_url == program.source_url]
    return matches[0] if len(matches) == 1 else None


def _source_sessions(session: Session, program: TrainingProgram) -> list[ProgramSession]:
    return (
        session.query(ProgramSession)
        .filter_by(program_id=program.id, session_role="coach_strength", is_custom=False)
        .order_by(ProgramSession.sequence_order, ProgramSession.id)
        .all()
    )


def _policy_and_sessions(
    session: Session, program: TrainingProgram
) -> tuple[ProgramPolicy | None, list[ProgramSession]]:
    key = catalog_key_for_program(program)
    policy = PROGRAM_POLICIES.get(key or "")
    sessions = _source_sessions(session, program)
    if not policy or tuple(item.name for item in sessions) != policy.session_names:
        return None, []
    return policy, sessions


def initialize_program_cursor(
    session: Session, program: TrainingProgram, *, activated_at: datetime | None = None
) -> ProgramCursor | None:
    """Create the first rolling cursor after a curated program is activated."""
    policy, sessions = _policy_and_sessions(session, program)
    if not policy or not sessions:
        return None
    now = activated_at or program.activated_at or program.created_at or datetime.now()
    cursor = session.get(ProgramCursor, program.id)
    if cursor is None:
        cursor = ProgramCursor(
            program_id=program.id,
            next_program_session_id=sessions[0].id,
            policy_version=policy.version,
            created_at=now,
            updated_at=now,
        )
        session.add(cursor)
    elif cursor.next_program_session_id not in {item.id for item in sessions}:
        # Only repair an empty/stale cursor when there is no completion history.
        if cursor.last_completed_activity_id is None:
            cursor.next_program_session_id = sessions[0].id
            cursor.updated_at = now
    cursor.policy_version = policy.version
    return cursor


def _expected_keys(session: Session, program_session_id: int) -> set[str]:
    rows = session.query(SessionExercise).filter_by(program_session_id=program_session_id).all()
    return {row.exercise_key or exercise_key(row.exercise_name) for row in rows}


def _observed_keys(session: Session, activity_id: int) -> set[str]:
    rows = session.query(ExerciseSet).filter_by(activity_id=activity_id).all()
    out: set[str] = set()
    for row in rows:
        if (row.set_type or "").upper() == "REST":
            continue
        composite = ":".join(value for value in (row.exercise_category, row.exercise_name) if value)
        if composite:
            out.add(exercise_key(composite))
        if row.exercise_name:
            out.add(exercise_key(row.exercise_name))
    return out


def _manual_fingerprint_match(
    session: Session,
    activity: Activity,
    next_session: ProgramSession,
    source_sessions: list[ProgramSession],
) -> bool:
    """Require complete, unique identity; there is intentionally no fuzzy threshold."""
    observed = _observed_keys(session, activity.id)
    expected = _expected_keys(session, next_session.id)
    if not observed or not expected or not expected.issubset(observed):
        return False
    for other in source_sessions:
        if other.id == next_session.id:
            continue
        other_expected = _expected_keys(session, other.id)
        if other_expected and other_expected.issubset(observed):
            return False
    return True


def _exact_planned_match(
    session: Session, activity: Activity, program: TrainingProgram, next_session_id: int
) -> PlannedSession | None:
    if activity.source_workout_id is None:
        return None
    return (
        session.query(PlannedSession)
        .join(ProgramSession, PlannedSession.program_session_id == ProgramSession.id)
        .filter(
            ProgramSession.program_id == program.id,
            PlannedSession.program_session_id == next_session_id,
            PlannedSession.garmin_workout_id == activity.source_workout_id,
            PlannedSession.status.notin_(tuple(INACTIVE_ORIGINAL_SESSION_STATUSES)),
        )
        .order_by(PlannedSession.target_date.desc(), PlannedSession.id.desc())
        .first()
    )


def _complete_planned_session(
    session: Session,
    activity: Activity,
    program_session_id: int,
    method: str,
    exact: PlannedSession | None,
) -> None:
    planned = exact
    if planned is None and activity.start_time:
        planned = (
            session.query(PlannedSession)
            .filter(
                PlannedSession.program_session_id == program_session_id,
                PlannedSession.target_date <= activity.start_time.date(),
                PlannedSession.status.notin_(tuple(INACTIVE_ORIGINAL_SESSION_STATUSES)),
            )
            .order_by(PlannedSession.target_date.desc(), PlannedSession.id.desc())
            .first()
        )
    if planned:
        planned.status = "completed"
        planned.completed_activity_id = activity.id
        planned.completion_match_method = method
        planned.completed_at = activity.start_time or datetime.now()
        planned.updated_at = datetime.now()


def reconcile_active_program(session: Session) -> int:
    """Advance only the active program's next session from confident sync evidence."""
    session.flush()
    program = (
        session.query(TrainingProgram)
        .filter(TrainingProgram.active.is_(True), TrainingProgram.status == "active")
        .order_by(TrainingProgram.updated_at.desc(), TrainingProgram.id.desc())
        .first()
    )
    if not program:
        return 0
    policy, source_sessions = _policy_and_sessions(session, program)
    if not policy:
        return 0
    cursor = initialize_program_cursor(session, program)
    if not cursor:
        return 0
    session.flush()

    known = session.query(ActivityProgramMatch.activity_id)
    activities = (
        session.query(Activity)
        .filter(
            Activity.start_time >= cursor.created_at,
            or_(
                Activity.activity_type.ilike("%strength%"),
                Activity.activity_type.ilike("%weight%"),
            ),
            ~Activity.id.in_(known),
        )
        .order_by(Activity.start_time, Activity.id)
        .all()
    )
    by_id = {item.id: item for item in source_sessions}
    order = [item.id for item in source_sessions]
    matched = 0
    for activity in activities:
        next_session = by_id.get(cursor.next_program_session_id)
        if not next_session:
            break
        exact = _exact_planned_match(session, activity, program, next_session.id)
        if exact:
            method = "garmin_workout_id"
        elif activity.source_workout_id is not None:
            # Explicit provenance for another Garmin workout must never be
            # reinterpreted as this program through an exercise resemblance.
            continue
        elif _manual_fingerprint_match(session, activity, next_session, source_sessions):
            method = "exercise_fingerprint"
        else:
            continue

        when = activity.start_time or datetime.now()
        session.add(ActivityProgramMatch(
            activity_id=activity.id,
            program_id=program.id,
            program_session_id=next_session.id,
            match_method=method,
            policy_version=policy.version,
            matched_at=when,
        ))
        _complete_planned_session(session, activity, next_session.id, method, exact)
        cursor.last_completed_program_session_id = next_session.id
        cursor.last_completed_activity_id = activity.id
        cursor.last_completed_at = when
        cursor.next_program_session_id = order[(order.index(next_session.id) + 1) % len(order)]
        cursor.policy_version = policy.version
        cursor.updated_at = datetime.now()
        matched += 1
        session.flush()
        # A match is an explicit local prerequisite event; the integration
        # handles incomplete details and owns a savepoint for derived writes.
        from coach.strength_progression_integration import RecalculationCause, process_activity_recalculation, request_activity_recalculation
        request_activity_recalculation(session, activity.id, cause=RecalculationCause.ACTIVITY_PROGRAM_MATCH_CREATED)
        process_activity_recalculation(session, activity.id, cause=RecalculationCause.ACTIVITY_PROGRAM_MATCH_CREATED)
    return matched


def program_state_facts(
    session: Session, program: TrainingProgram, *, on_date: date | None = None
) -> dict | None:
    """Return deterministic sequence/rest facts for renderers and decision logic."""
    policy, sessions = _policy_and_sessions(session, program)
    if not policy:
        return None
    cursor = initialize_program_cursor(session, program)
    if not cursor:
        return None
    session.flush()
    by_id = {item.id: item for item in sessions}
    next_session = by_id.get(cursor.next_program_session_id)
    if not next_session:
        return None

    target = on_date or date.today()
    earliest_allowed = target
    earliest_recommended = target
    if cursor.last_completed_at and cursor.last_completed_program_session_id in by_id:
        completed = by_id[cursor.last_completed_program_session_id]
        index = sessions.index(completed)
        completed_day = cursor.last_completed_at.date()
        earliest_allowed = completed_day + timedelta(days=policy.minimum_rest_days_after[index] + 1)
        earliest_recommended = completed_day + timedelta(days=policy.preferred_rest_days_after[index] + 1)
    is_rest_day = target < earliest_recommended

    recovery = None
    if is_rest_day and policy.recovery_activity:
        item = policy.recovery_activity
        recovery = {
            "label": item.label,
            "duration_min": list(item.duration_min),
            "instruction": item.instruction,
            "evidence_url": item.evidence_url,
            "evidence_status": item.evidence_status,
            "source_location": item.source_location,
        }
    return {
        "program_key": policy.program_key,
        "policy_version": policy.version,
        "next_session_id": next_session.id,
        "next_session_name": next_session.name,
        "last_completed_at": cursor.last_completed_at.isoformat() if cursor.last_completed_at else None,
        "earliest_allowed_date": earliest_allowed.isoformat(),
        "earliest_recommended_date": earliest_recommended.isoformat(),
        "is_program_rest_day": is_rest_day,
        "consecutive_day_override_allowed": policy.consecutive_day_override_allowed,
        "optional_recovery_activity": recovery,
    }
