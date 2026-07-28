from contextlib import contextmanager

import pytest
from garminconnect import GarminConnectAuthenticationError
from coach.garmin_compiler import (
    GarminFailureKind,
    _get_step_weight,
    build_generic_step,
    compile_and_schedule_result,
)
from db import PlannedSession, ProgramSession, SessionExercise, SyncState, TrainingProgram

def test_get_step_weight_with_none_weight():
    """Test that a step with weightValue = None (like bodyweight exercises) does not crash."""
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {
            "stepTypeKey": "interval"
        },
        "weightValue": None
    }
    # Should not throw TypeError and should return 0.0
    weight = _get_step_weight(step)
    assert weight == 0.0

def test_get_step_weight_with_valid_weight():
    """Test that a step with valid weightValue is parsed correctly."""
    step = {
        "type": "ExecutableStepDTO",
        "weightValue": 50.5
    }
    weight = _get_step_weight(step)
    assert weight == 50.5

def test_get_step_weight_in_repeat_group_with_none_weight():
    """Test RepeatGroupDTO with null weightValue does not crash."""
    step = {
        "type": "RepeatGroupDTO",
        "workoutSteps": [
            {
                "stepType": {"stepTypeKey": "interval"},
                "weightValue": None
            }
        ]
    }
    weight = _get_step_weight(step)
    assert weight == 0.0


def test_schedule_session_calendar_only_creates_planned_session(session):
    from coach.garmin_compiler import compile_and_schedule

    ok = compile_and_schedule(session, {
        "action": "schedule_session",
        "title": "Easy Run",
        "activity_type": "running",
        "target_date": "2026-07-03",
        "suggested_time": "07:00",
        "duration_min": 45,
        "intensity": "light",
    })

    assert ok is True
    planned = session.query(PlannedSession).one()
    assert planned.title == "Easy Run"
    assert planned.status == "approved"
    row = session.get(SyncState, "coach_calendar_events")
    assert row is not None
    assert "Easy Run" in row.value


def _active_session(session):
    program = TrainingProgram(name="A/B", active=True, status="active", days_per_week=2)
    session.add(program)
    session.flush()
    routine = ProgramSession(program_id=program.id, name="Workout A", sport_type="strength_training", sequence_order=1)
    session.add(routine)
    session.flush()
    session.add_all([
        SessionExercise(program_session_id=routine.id, exercise_name="Bench Press", exercise_key="BENCH_PRESS", garmin_category="BENCH_PRESS", garmin_name="BENCH_PRESS", movement_pattern="horizontal_push", sets=3, reps=12, weight_kg=40, rest_seconds=90, warmup_enabled=True, warmup_reps=12, warmup_weight_kg=20, order_index=0),
        SessionExercise(program_session_id=routine.id, exercise_name="Curated Source Move", exercise_key="CURATED_SOURCE_MOVE", movement_pattern="other", is_generic=True, sets=2, reps=10, rest_seconds=60, order_index=1),
    ])
    session.commit()
    return routine


def test_program_workout_has_structured_warmup_and_generic_fallback(session):
    from coach.garmin_compiler import build_program_workout
    routine = _active_session(session)
    payload = build_program_workout(session, routine.id, "18:00")
    assert payload["workoutName"] == "Workout A @ 18:00"
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["description"] == "Warm-up: Bench Press"
    assert steps[0]["endConditionValue"] == 12
    assert steps[0]["weightValue"] == 20
    generic = steps[-1]["workoutSteps"][0]
    assert generic["description"] == "Curated Source Move"
    assert generic["category"] is None and generic["exerciseName"] is None


def test_program_telegram_approval_uploads_verifies_schedules_and_is_idempotent(session, monkeypatch):
    import coach.garmin_compiler as compiler
    routine = _active_session(session)

    class FakeApi:
        def __init__(self):
            self.uploads = []
            self.scheduled = []
        def upload_workout(self, payload):
            self.uploads.append(payload)
            return {"workoutId": 77}
        def get_workout_by_id(self, workout_id):
            return self.uploads[-1]
        def schedule_workout(self, workout_id, day):
            self.scheduled.append((workout_id, day))
        def delete_workout(self, workout_id):
            pass

    api = FakeApi()
    class FakeGarminClient:
        def __init__(self):
            self.api = api
        def login(self):
            pass
    fake_garmin_client = FakeGarminClient()
    monkeypatch.setattr("sync.garmin_registry.GarminClientRegistry.get", lambda self, uid: fake_garmin_client)
    import tenant_context
    with tenant_context.tenant_scope(tenant_context.TenantIdentity("00000000-0000-0000-0000-000000000001")):
        action = {"action": "schedule_session", "program_session_id": routine.id, "title": routine.name, "activity_type": "strength_training", "target_date": "2026-07-20", "suggested_time": "18:00", "duration_min": 60, "intensity": "normal"}
        assert compiler.compile_and_schedule(session, action) is True
        assert compiler.compile_and_schedule(session, action) is True
        assert len(api.uploads) == 1
        assert api.uploads[0]["workoutName"] == "Workout A @ 18:00"
        assert api.scheduled == [(77, "2026-07-20")]
    assert session.query(PlannedSession).one().garmin_workout_id == 77


def _program_action(routine):
    return {
        "action": "schedule_session",
        "program_session_id": routine.id,
        "title": routine.name,
        "activity_type": "strength_training",
        "target_date": "2026-07-20",
        "suggested_time": "18:00",
        "duration_min": 60,
        "intensity": "normal",
    }


class _MutationApi:
    def __init__(self, *, verify=True, schedule_error=None):
        self.verify = verify
        self.schedule_error = schedule_error
        self.uploads = []
        self.reads = []
        self.scheduled = []
        self.deleted = []

    def upload_workout(self, payload):
        self.uploads.append(payload)
        return {"workoutId": 91}

    def get_workout_by_id(self, workout_id):
        self.reads.append(workout_id)
        if self.verify:
            return self.uploads[-1]
        return {"workoutSegments": []}

    def schedule_workout(self, workout_id, day):
        self.scheduled.append((workout_id, day))
        if self.schedule_error:
            raise self.schedule_error

    def delete_workout(self, workout_id):
        self.deleted.append(workout_id)


class _MutationClient:
    def __init__(self, api, auth_error=None):
        self.api = api
        self.auth_error = auth_error
        self.ensure_calls = 0
        self.login_calls = 0

    def ensure_authenticated(self):
        self.ensure_calls += 1
        if self.auth_error:
            raise self.auth_error

    def login(self):
        self.login_calls += 1
        raise AssertionError("destructive login must not be used")

    def mark_session_expired(self):
        pass


def _use_client(monkeypatch, fake_client):
    @contextmanager
    def current():
        yield fake_client

    monkeypatch.setattr(
        "coach.garmin_compiler.current_garmin_client", current
    )


def test_expired_client_returns_reconnect_required_without_local_changes(
    session, monkeypatch
):
    routine = _active_session(session)
    api = _MutationApi()
    fake = _MutationClient(
        api,
        GarminConnectAuthenticationError("expired"),
    )
    _use_client(monkeypatch, fake)

    result = compile_and_schedule_result(session, _program_action(routine))

    assert result.failure == GarminFailureKind.RECONNECT_REQUIRED
    assert result.stage == "authenticate"
    assert "Reconnect Garmin" in result.user_message
    assert fake.login_calls == 0
    assert api.uploads == []
    assert session.query(PlannedSession).count() == 0
    assert session.get(SyncState, "coach_calendar_events") is None


def test_verification_failure_deletes_upload_and_persists_nothing(
    session, monkeypatch
):
    routine = _active_session(session)
    api = _MutationApi(verify=False)
    fake = _MutationClient(api)
    _use_client(monkeypatch, fake)

    result = compile_and_schedule_result(session, _program_action(routine))

    assert result.failure == GarminFailureKind.VERIFY_REJECTED
    assert result.stage == "verify"
    assert api.deleted == [91]
    assert api.scheduled == []
    assert session.query(PlannedSession).count() == 0
    assert session.get(SyncState, "coach_calendar_events") is None


def test_schedule_failure_deletes_upload_and_persists_nothing(
    session, monkeypatch
):
    routine = _active_session(session)
    api = _MutationApi(schedule_error=RuntimeError("rejected"))
    fake = _MutationClient(api)
    _use_client(monkeypatch, fake)

    result = compile_and_schedule_result(session, _program_action(routine))

    assert result.failure == GarminFailureKind.SCHEDULE_FAILED
    assert result.stage == "schedule"
    assert api.deleted == [91]
    assert api.uploads and api.reads == [91]
    assert len(api.scheduled) == 1
    assert session.query(PlannedSession).count() == 0
    assert session.get(SyncState, "coach_calendar_events") is None
