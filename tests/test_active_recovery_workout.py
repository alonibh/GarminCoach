from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.workout import WalkingWorkout

from coach.active_recovery import (
    ACTIVE_RECOVERY_DURATION_SECONDS,
    ACTIVE_RECOVERY_SYNC_STATE_KEY,
    ACTIVE_RECOVERY_TEMPLATE_VERSION,
    ACTIVE_RECOVERY_WORKOUT_NAME,
    ActiveRecoveryFailureKind,
    GarminConnectNotFoundError,
    build_active_recovery_workout,
    ensure_active_recovery_workout,
    verify_active_recovery_workout,
)
from db import DecisionRecord, PendingInteraction, PlannedSession, ProgramCursor, SyncState


def _payload():
    return build_active_recovery_workout().to_dict()


def _step(payload):
    return payload["workoutSegments"][0]["workoutSteps"][0]


def test_builder_is_exact_typed_and_deterministic():
    first = build_active_recovery_workout()
    second = build_active_recovery_workout()

    assert ACTIVE_RECOVERY_TEMPLATE_VERSION == "v1"
    assert ACTIVE_RECOVERY_SYNC_STATE_KEY == "active_recovery_workout_id_v1"
    assert isinstance(first, WalkingWorkout)
    assert first.workoutName == ACTIVE_RECOVERY_WORKOUT_NAME
    assert first.sportType["sportTypeKey"] == "walking"
    assert first.estimatedDurationInSecs == ACTIVE_RECOVERY_DURATION_SECONDS
    assert first.to_dict() == second.to_dict()
    segment = first.workoutSegments[0]
    step = segment.workoutSteps[0]
    assert len(first.workoutSegments) == len(segment.workoutSteps) == 1
    assert segment.sportType["sportTypeKey"] == "walking"
    assert step.type == "ExecutableStepDTO"
    assert step.stepType["stepTypeKey"] == "interval"
    assert step.endCondition["conditionTypeKey"] == "time"
    assert step.endConditionValue == ACTIVE_RECOVERY_DURATION_SECONDS
    assert step.targetType["workoutTargetTypeKey"] == "no.target"
    serialized = first.to_dict()
    assert "workoutSteps" not in _step(serialized)
    assert all(key not in _step(serialized) for key in ("targetValueOne", "targetValueTwo", "secondaryTargetType", "zoneNumber"))


def test_verifier_accepts_canonical_read_back_and_harmless_metadata():
    canonical = _payload()
    canonical.update({"workoutId": 42, "ownerId": 9, "updatedDate": "2026-07-29T10:00:00"})
    canonical["workoutSegments"][0].update({"segmentId": 3, "displayOrder": 99})
    _step(canonical).update({"stepId": 7, "childStepId": 6, "displayOrder": 1, "provider": "garmin"})

    verify_active_recovery_workout(canonical)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(workoutName="Different"),
        lambda p: p["sportType"].update(sportTypeKey="running", sportTypeId=1),
        lambda p: p["workoutSegments"][0]["sportType"].update(sportTypeKey="running", sportTypeId=1),
        lambda p: p.update(workoutSegments=[]),
        lambda p: p["workoutSegments"].append(deepcopy(p["workoutSegments"][0])),
        lambda p: p["workoutSegments"][0].update(workoutSteps=[]),
        lambda p: p["workoutSegments"][0]["workoutSteps"].append(deepcopy(_step(p))),
        lambda p: _step(p).update(type="RepeatGroupDTO", numberOfIterations=1, workoutSteps=[]),
        lambda p: _step(p).update(endConditionValue=1799),
        lambda p: _step(p).update(endConditionValue=1801),
        lambda p: _step(p)["endCondition"].update(conditionTypeKey="lap.button", conditionTypeId=1),
        lambda p: _step(p).update(targetType={"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}),
        lambda p: _step(p).update(targetValueOne=100),
        lambda p: _step(p).update(secondaryTargetType={"workoutTargetTypeId": 5, "workoutTargetTypeKey": "pace.zone"}),
        lambda p: _step(p).update(zoneNumber=2),
        lambda p: _step(p).update(workoutSteps=[deepcopy(_step(p))]),
        lambda p: p.update(estimatedDurationInSecs=1799),
        lambda p: p.update(estimatedDurationInSecs=None),
        lambda p: _step(p).update(targetSpeed=2.5),
    ],
)
def test_verifier_rejects_noncanonical_execution_semantics(mutate):
    payload = _payload()
    mutate(payload)
    with pytest.raises(ValueError):
        verify_active_recovery_workout(payload)


class _ForbiddenApi:
    def schedule_workout(self, *_args, **_kwargs):
        raise AssertionError("scheduling is forbidden for the template primitive")


class _FakeApi(_ForbiddenApi):
    def __init__(self, *, read_error=None, upload_error=None, upload_result=None, read_payload=None, delete_error=None):
        self.read_error = read_error
        self.upload_error = upload_error
        self.upload_result = upload_result if upload_result is not None else {"workoutId": 77}
        self.read_payload = read_payload
        self.delete_error = delete_error
        self.uploads, self.reads, self.deleted = [], [], []
        self.boundary = None
        self.delete_depths = []

    def upload_walking_workout(self, workout):
        self.uploads.append(workout)
        if self.upload_error:
            raise self.upload_error
        return self.upload_result

    def get_workout_by_id(self, workout_id):
        self.reads.append(workout_id)
        if self.read_error:
            if isinstance(self.read_error, dict):
                error = self.read_error.get(workout_id)
                if error:
                    raise error
            else:
                raise self.read_error
        return deepcopy(self.read_payload if self.read_payload is not None else _payload())

    def delete_workout(self, workout_id):
        if self.boundary is None or self.boundary.depth <= 0:
            raise AssertionError("delete_workout must run inside current_garmin_client")
        self.delete_depths.append(self.boundary.depth)
        self.deleted.append(workout_id)
        if self.delete_error:
            raise self.delete_error


class _FakeClient:
    def __init__(self, api, *, auth_error=None):
        self.api, self.auth_error = api, auth_error
        self.auth_calls, self.expired = 0, False

    def ensure_authenticated(self):
        self.auth_calls += 1
        if self.auth_error:
            raise self.auth_error

    def login(self):
        raise AssertionError("the current client must use ensure_authenticated")

    def mark_session_expired(self):
        self.expired = True


class _Boundary:
    def __init__(self, client):
        self.client = client
        self.depth = 0
        self.events = []

    @contextmanager
    def current(self):
        self.depth += 1
        self.events.append("enter")
        try:
            yield self.client
        finally:
            self.events.append("exit")
            self.depth -= 1


def _use_client(monkeypatch, client):
    boundary = _Boundary(client)
    client.api.boundary = boundary

    @contextmanager
    def current():
        with boundary.current() as current_client:
            yield current_client
    monkeypatch.setattr("coach.active_recovery.current_garmin_client", current)
    return boundary


def _assert_no_unrelated_mutation(session):
    assert session.query(PlannedSession).count() == 0
    assert session.query(ProgramCursor).count() == 0
    assert session.query(PendingInteraction).count() == 0
    assert session.query(DecisionRecord).count() == 0
    assert session.get(SyncState, "coach_calendar_events") is None


def test_service_creates_once_verifies_persists_and_reuses(session, monkeypatch):
    api = _FakeApi()
    client = _FakeClient(api)
    _use_client(monkeypatch, client)

    created = ensure_active_recovery_workout(session)
    reused = ensure_active_recovery_workout(session)

    assert created.ok and created.created and created.workout_id == 77
    assert reused.ok and not reused.created and reused.workout_id == 77
    assert client.auth_calls == 2
    assert len(api.uploads) == 1 and api.reads == [77, 77]
    assert api.deleted == []
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY).value == "77"
    _assert_no_unrelated_mutation(session)


def test_service_reuses_existing_id_without_upload_or_delete(session, monkeypatch):
    session.add(SyncState(key=ACTIVE_RECOVERY_SYNC_STATE_KEY, value="41"))
    session.commit()
    api = _FakeApi()
    boundary = _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert result.ok and not result.created and result.workout_id == 41
    assert api.uploads == [] and api.reads == [41] and api.deleted == []
    assert boundary.events == ["enter", "exit"]
    _assert_no_unrelated_mutation(session)


@pytest.mark.parametrize("stored", ["", "0", "-1", "1.0", "abc", " 1"])
def test_service_rejects_malformed_stored_id_without_upload(session, monkeypatch, stored):
    session.add(SyncState(key=ACTIVE_RECOVERY_SYNC_STATE_KEY, value=stored))
    session.commit()
    api = _FakeApi()
    _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert result.failure == ActiveRecoveryFailureKind.INVALID_STORED_ID
    assert api.uploads == api.reads == api.deleted == []


def test_service_stored_id_404_creates_one_verified_replacement(session, monkeypatch):
    session.add(SyncState(key=ACTIVE_RECOVERY_SYNC_STATE_KEY, value="41"))
    session.commit()
    api = _FakeApi(read_error={41: GarminConnectNotFoundError("not found")})
    boundary = _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert result.ok and result.created and result.workout_id == 77
    assert api.reads == [41, 77] and len(api.uploads) == 1 and api.deleted == []
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY).value == "77"
    assert boundary.events == ["enter", "exit"]


@pytest.mark.parametrize("exc", [GarminConnectConnectionError("network"), GarminConnectTooManyRequestsError("429")])
def test_service_existing_non404_read_failure_fails_closed(session, monkeypatch, exc):
    session.add(SyncState(key=ACTIVE_RECOVERY_SYNC_STATE_KEY, value="41"))
    session.commit()
    api = _FakeApi(read_error=exc)
    _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert not result.ok and result.stage == "read_back"
    assert api.uploads == api.deleted == [] and api.reads == [41]
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY).value == "41"


def test_service_invalid_existing_template_is_neither_deleted_nor_replaced(session, monkeypatch):
    session.add(SyncState(key=ACTIVE_RECOVERY_SYNC_STATE_KEY, value="41"))
    session.commit()
    invalid = _payload()
    invalid["workoutName"] = "Wrong"
    api = _FakeApi(read_payload=invalid)
    boundary = _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert result.failure == ActiveRecoveryFailureKind.VERIFY_REJECTED
    assert api.uploads == api.deleted == [] and api.reads == [41]
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY).value == "41"
    assert boundary.events == ["enter", "exit"]


def test_service_new_upload_failures_roll_back_and_delete_only_new_id(session, monkeypatch):
    invalid = _payload()
    invalid["workoutName"] = "Wrong"
    api = _FakeApi(read_payload=invalid)
    boundary = _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert result.failure == ActiveRecoveryFailureKind.VERIFY_REJECTED
    assert api.deleted == [77]
    assert api.delete_depths == [1]
    assert boundary.events == ["enter", "exit", "enter", "exit"]
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY) is None
    _assert_no_unrelated_mutation(session)


def test_service_read_back_failure_reacquires_boundary_for_cleanup(session, monkeypatch):
    api = _FakeApi(read_error={77: GarminConnectConnectionError("read back failed")})
    boundary = _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert result.failure == ActiveRecoveryFailureKind.SERVICE_FAILED
    assert result.stage == "read_back"
    assert api.deleted == [77] and api.delete_depths == [1]
    assert boundary.events == ["enter", "exit", "enter", "exit"]
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY) is None


def test_service_upload_id_missing_and_cleanup_failure_preserve_original_result(session, monkeypatch, caplog):
    api = _FakeApi(upload_result={})
    missing_boundary = _use_client(monkeypatch, _FakeClient(api))
    missing = ensure_active_recovery_workout(session)
    assert not missing.ok and missing.stage == "upload" and api.deleted == []
    assert missing_boundary.events == ["enter", "exit"]

    secret = "cleanup-message-must-not-be-logged"
    api = _FakeApi(read_payload={"workoutName": "wrong"}, delete_error=RuntimeError(secret))
    boundary = _use_client(monkeypatch, _FakeClient(api))
    failed_verify = ensure_active_recovery_workout(session)
    assert failed_verify.failure == ActiveRecoveryFailureKind.VERIFY_REJECTED
    assert failed_verify.stage == "verify" and api.deleted == [77]
    assert api.delete_depths == [1]
    assert boundary.events == ["enter", "exit", "enter", "exit"]
    assert "exception_type=RuntimeError" in caplog.text and secret not in caplog.text


def test_service_persistence_failure_rolls_back_and_deletes_new_upload(session, monkeypatch):
    api = _FakeApi()
    boundary = _use_client(monkeypatch, _FakeClient(api))
    monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")))

    result = ensure_active_recovery_workout(session)

    assert not result.ok and result.stage == "persist"
    assert api.deleted == [77]
    assert api.delete_depths == [1]
    assert boundary.events == ["enter", "exit", "enter", "exit"]
    assert session.get(SyncState, ACTIVE_RECOVERY_SYNC_STATE_KEY) is None


def test_service_rate_limit_is_not_retried_and_logs_no_payload(session, monkeypatch, caplog):
    secret = "secret-token-should-never-appear"
    api = _FakeApi(upload_error=GarminConnectTooManyRequestsError(secret))
    _use_client(monkeypatch, _FakeClient(api))

    result = ensure_active_recovery_workout(session)

    assert not result.ok and result.stage == "upload"
    assert len(api.uploads) == 1 and api.reads == api.deleted == []
    assert secret not in caplog.text


def test_service_auth_failure_marks_expired_and_does_not_mutate(session, monkeypatch):
    api = _FakeApi()
    client = _FakeClient(api, auth_error=GarminConnectAuthenticationError("expired"))
    boundary = _use_client(monkeypatch, client)

    result = ensure_active_recovery_workout(session)

    assert result.failure == ActiveRecoveryFailureKind.RECONNECT_REQUIRED
    assert client.expired and api.uploads == api.reads == api.deleted == []
    assert boundary.events == ["enter", "exit"]
    _assert_no_unrelated_mutation(session)
