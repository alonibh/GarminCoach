import json
from datetime import date, datetime
from types import SimpleNamespace

from coach.program_policy import PROGRAM_POLICIES, SOURCE_TRAINING_LEVELS, validate_program_policies
from coach.program_state import (
    initialize_program_cursor,
    program_state_facts,
    reconcile_active_program,
)
from coach.programs import PROGRAMS
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
import sync.sync_service as sync_service


def _add_program(session, key="full_body_2", *, active=True, exercise_keys=None):
    policy = PROGRAM_POLICIES[key]
    program = TrainingProgram(
        name=key,
        source_type="curated_archetype",
        source_url=policy.source_url,
        goal_tags=json.dumps([key]),
        active=active,
        status="active" if active else "archived",
        created_at=datetime(2026, 7, 1),
        activated_at=datetime(2026, 7, 1) if active else None,
        updated_at=datetime(2026, 7, 1),
    )
    session.add(program)
    session.flush()
    keys = exercise_keys or [f"ONLY_{index}" for index in range(len(policy.session_names))]
    program_sessions = []
    for index, (name, key_name) in enumerate(zip(policy.session_names, keys), start=1):
        item = ProgramSession(
            program_id=program.id,
            name=name,
            sport_type="strength_training",
            sequence_order=index,
            session_role="coach_strength",
            is_custom=False,
        )
        session.add(item)
        session.flush()
        session.add(SessionExercise(
            program_session_id=item.id,
            exercise_name=key_name,
            exercise_key=key_name,
            order_index=1,
        ))
        program_sessions.append(item)
    session.flush()
    return program, program_sessions


def _activity(session, activity_id, when, *, workout_id=None, exercise_name=None):
    activity = Activity(
        id=activity_id,
        activity_type="strength_training",
        start_time=when,
        source_workout_id=workout_id,
    )
    session.add(activity)
    if exercise_name:
        session.add(ExerciseSet(
            activity_id=activity_id,
            set_index=0,
            set_type="ACTIVE",
            exercise_name=exercise_name,
        ))
    session.flush()
    return activity


def test_all_twenty_five_curated_program_policies_match_catalog():
    validate_program_policies(PROGRAMS)
    assert len(PROGRAM_POLICIES) == 25


def test_powerbuilding_uses_published_level_and_intermediate_cadence():
    policy = PROGRAM_POLICIES["powerbuilding_ppl_6"]
    assert SOURCE_TRAINING_LEVELS["powerbuilding_ppl_6"] == "Intermediate"
    assert policy.minimum_rest_days_after == (0, 0, 1, 0, 0, 1)


def test_activity_sync_captures_provenance_once(session, monkeypatch):
    calls = {"full": 0}

    def full_activity(_activity_id):
        calls["full"] += 1
        return {
            "summaryDTO": {"directWorkoutRpe": 40, "directWorkoutFeel": 75},
            "metadataDTO": {"workoutId": 4321},
        }

    monkeypatch.setattr(sync_service.client, "_api", SimpleNamespace(get_activity=full_activity))
    monkeypatch.setattr(sync_service.client, "hr_zones", lambda _activity_id: [])
    raw = {
        "activityId": 90,
        "activityType": {"typeKey": "strength_training"},
        "startTimeLocal": "2026-07-02 09:00:00",
    }
    sync_service._upsert_activity(session, raw)
    session.flush()
    row = session.get(Activity, 90)
    assert row.source_workout_id == 4321
    assert row.provenance_checked is True

    sync_service._upsert_activity(session, raw)
    assert calls["full"] == 1


def test_exact_garmin_provenance_advances_cursor_and_completes_plan(session):
    program, source_sessions = _add_program(session)
    cursor = initialize_program_cursor(session, program, activated_at=datetime(2026, 7, 3, 8))
    planned = PlannedSession(
        program_session_id=source_sessions[0].id,
        target_date=date(2026, 7, 4),
        status="approved",
        garmin_workout_id=991,
    )
    session.add(planned)
    _activity(session, 100, datetime(2026, 7, 4, 9), workout_id=991)

    assert reconcile_active_program(session) == 1
    session.flush()

    assert cursor.next_program_session_id == source_sessions[1].id
    assert planned.status == "completed"
    assert planned.completed_activity_id == 100
    assert planned.completion_match_method == "garmin_workout_id"
    assert session.query(ActivityProgramMatch).one().program_id == program.id


def test_cursor_survives_week_boundary_and_uses_rolling_rest(session):
    program, source_sessions = _add_program(session)
    cursor = initialize_program_cursor(session, program, activated_at=datetime(2026, 7, 3, 8))
    _activity(session, 101, datetime(2026, 7, 4, 9), exercise_name="ONLY_0")
    assert reconcile_active_program(session) == 1

    sunday = program_state_facts(session, program, on_date=date(2026, 7, 5))
    monday = program_state_facts(session, program, on_date=date(2026, 7, 6))

    assert sunday["next_session_name"] == source_sessions[1].name
    assert sunday["is_program_rest_day"] is True
    assert sunday["earliest_allowed_date"] == "2026-07-05"
    assert sunday["earliest_recommended_date"] == "2026-07-06"
    assert monday["next_session_name"] == source_sessions[1].name
    assert monday["is_program_rest_day"] is False
    assert session.get(ProgramCursor, program.id).next_program_session_id == source_sessions[1].id


def test_existing_active_program_reconciles_history_since_activation(session):
    program, source_sessions = _add_program(session)
    _activity(session, 104, datetime(2026, 7, 2, 9), exercise_name="ONLY_0")

    assert reconcile_active_program(session) == 1

    cursor = session.get(ProgramCursor, program.id)
    assert cursor.created_at == program.activated_at
    assert cursor.next_program_session_id == source_sessions[1].id


def test_ambiguous_manual_fingerprint_does_not_advance(session):
    program, source_sessions = _add_program(
        session, exercise_keys=["SHARED", "SHARED"]
    )
    cursor = initialize_program_cursor(session, program, activated_at=datetime(2026, 7, 1))
    _activity(session, 102, datetime(2026, 7, 2, 9), exercise_name="SHARED")

    assert reconcile_active_program(session) == 0
    assert cursor.next_program_session_id == source_sessions[0].id
    assert session.query(ActivityProgramMatch).count() == 0


def test_other_program_garmin_workout_never_advances_active_program(session):
    active, active_sessions = _add_program(session, active=True)
    inactive, inactive_sessions = _add_program(session, active=False)
    cursor = initialize_program_cursor(session, active, activated_at=datetime(2026, 7, 1))
    session.add(PlannedSession(
        program_session_id=inactive_sessions[0].id,
        target_date=date(2026, 7, 2),
        status="approved",
        garmin_workout_id=777,
    ))
    _activity(
        session, 103, datetime(2026, 7, 2, 9),
        workout_id=777, exercise_name="ONLY_0",
    )

    assert reconcile_active_program(session) == 0
    assert cursor.next_program_session_id == active_sessions[0].id
    assert session.query(ActivityProgramMatch).count() == 0
