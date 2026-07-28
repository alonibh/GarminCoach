from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

import sync.garmin_client as garmin_client_module
from secret_vault import UserSecretVault, VaultError
from sync.garmin_client import GarminClient
from sync.garmin_registry import (
    GarminClientRegistry,
    set_garmin_registry_for_testing,
)


class FakeGarminClient:
    def __init__(self, *, email: str, token_store: Path):
        self.email = email
        self.token_store = token_store
        self.authenticated = False

    def restore_tokens(self, token_json: str) -> None:
        assert token_json == '{"oauth1":"private-token"}'
        self.authenticated = True

    def begin_login(self, email: str, password: str) -> str:
        assert password
        self.email = email
        self.authenticated = True
        return "connected"

    def is_authenticated(self) -> bool:
        return self.authenticated

    def serialized_tokens(self) -> str:
        return '{"oauth1":"private-token"}'


def test_vault_encrypts_and_physically_separates_user_secrets(tmp_path):
    first, second = str(uuid4()), str(uuid4())
    vault = UserSecretVault(Fernet.generate_key())
    vault.write(first, {"garmin_email": "first@example.com"}, root=tmp_path)
    vault.write(second, {"garmin_email": "second@example.com"}, root=tmp_path)

    first_path = vault.path_for(first, root=tmp_path)
    second_path = vault.path_for(second, root=tmp_path)
    assert first_path.parent != second_path.parent
    assert b"first@example.com" not in first_path.read_bytes()
    assert b"second@example.com" not in second_path.read_bytes()
    assert vault.read(first, root=tmp_path)["garmin_email"] == "first@example.com"
    assert vault.read(second, root=tmp_path)["garmin_email"] == "second@example.com"


def test_vault_rejects_wrong_encryption_key(tmp_path):
    user_id = str(uuid4())
    UserSecretVault(Fernet.generate_key()).write(
        user_id, {"token": "secret"}, root=tmp_path
    )
    with pytest.raises(VaultError):
        UserSecretVault(Fernet.generate_key()).read(user_id, root=tmp_path)


def test_restore_tokens_raises_auth_error_when_validation_fails(monkeypatch):
    client = GarminClient(email="test@example.com", token_store=Path("/tmp"))
    from garminconnect import GarminConnectAuthenticationError

    class FakeBadGarmin:
        def __init__(self):
            self.client = self
        def loads(self, data):
            pass
        def get_full_name(self):
            raise Exception("Token expired 401")

    monkeypatch.setattr("sync.garmin_client.Garmin", FakeBadGarmin)

    with pytest.raises(GarminConnectAuthenticationError):
        client.restore_tokens('{"token":"expired"}')


def test_ensure_authenticated_reuses_live_in_memory_api(monkeypatch, tmp_path):
    client = GarminClient(email="test@example.com", token_store=tmp_path)
    live_api = object()
    client._api = live_api
    monkeypatch.setattr(
        client,
        "login",
        lambda *_args, **_kwargs: pytest.fail(
            "a live in-memory API must not be replaced"
        ),
    )

    client.ensure_authenticated()

    assert client.api is live_api



def test_registry_checkpoints_only_to_the_matching_user(tmp_path):
    data_root = tmp_path / "users"
    runtime_root = tmp_path / "runtime"
    vault = UserSecretVault(Fernet.generate_key())
    registry = GarminClientRegistry(
        vault=vault,
        data_root=data_root,
        client_factory=FakeGarminClient,
    )
    first, second = str(uuid4()), str(uuid4())

    assert registry.begin_login(first, "first@example.com", "password-one") == "connected"
    assert registry.begin_login(second, "second@example.com", "password-two") == "connected"

    first_values = vault.read(first, root=data_root)
    second_values = vault.read(second, root=data_root)
    assert first_values["garmin_email"] == "first@example.com"
    assert second_values["garmin_email"] == "second@example.com"
    assert "password" not in first_values
    assert "password" not in second_values
    assert b"private-token" not in vault.path_for(first, root=data_root).read_bytes()
    assert not runtime_root.exists()

    restored = GarminClientRegistry(
        vault=vault,
        data_root=data_root,
        client_factory=FakeGarminClient,
    )
    assert restored.get(first).is_authenticated()


def test_mfa_continuation_state_is_kept_in_memory_and_reused(monkeypatch, tmp_path):
    continuation = {"opaque": "state"}

    class FakeApi:
        def __init__(self, **_kwargs):
            self.resumed_with = None

        def login(self):
            return "needs_mfa", continuation

        def resume_login(self, state, code):
            self.resumed_with = (state, code)

        def get_full_name(self):
            return "Athlete"

    monkeypatch.setattr(garmin_client_module, "Garmin", FakeApi)
    client = GarminClient(token_store=tmp_path)
    assert client.begin_login("athlete@example.com", "password") == "mfa_required"
    pending_api = client._pending_api
    assert client._pending_state == continuation
    assert list(tmp_path.iterdir()) == []
    client.complete_mfa("123456")
    assert pending_api.resumed_with == (continuation, "123456")
    assert client.is_authenticated()
    assert client._pending_state is None


def test_registry_restored_client_schedules_without_destructive_login(
    session, tmp_path
):
    from coach.garmin_compiler import compile_and_schedule_result
    from db import PlannedSession, ProgramSession, SessionExercise, TrainingProgram
    from tenant_context import TenantIdentity, tenant_scope

    class MutationApi:
        def __init__(self):
            self.payload = None
            self.scheduled = []

        def upload_workout(self, payload):
            self.payload = payload
            return {"workoutId": 808}

        def get_workout_by_id(self, _workout_id):
            return self.payload

        def schedule_workout(self, workout_id, target_date):
            self.scheduled.append((workout_id, target_date))

        def delete_workout(self, _workout_id):
            pass

    class RestoredClient:
        instances = []

        def __init__(self, *, email, token_store):
            self.email = email
            self.token_store = token_store
            self.api = MutationApi()
            self.authenticated = False
            self.ensure_calls = 0
            self.login_calls = 0
            self.__class__.instances.append(self)

        def restore_tokens(self, token_json):
            assert token_json == '{"session":"encrypted-at-rest"}'
            self.authenticated = True

        def is_authenticated(self):
            return self.authenticated

        def ensure_authenticated(self):
            self.ensure_calls += 1
            assert self.authenticated

        def login(self):
            self.login_calls += 1
            raise AssertionError("registry-restored clients must not log in again")

    user_id = str(uuid4())
    data_root = tmp_path / "users"
    vault = UserSecretVault(Fernet.generate_key())
    vault.write(
        user_id,
        {
            "garmin_email": "athlete@example.com",
            "garmin_tokens": '{"session":"encrypted-at-rest"}',
        },
        root=data_root,
    )
    registry = GarminClientRegistry(
        vault=vault,
        data_root=data_root,
        client_factory=RestoredClient,
    )
    set_garmin_registry_for_testing(registry)
    program = TrainingProgram(name="Program", active=True, status="active")
    session.add(program)
    session.flush()
    routine = ProgramSession(
        program_id=program.id,
        name="Full Body 2",
        sport_type="strength_training",
        sequence_order=1,
        duration_min=60,
    )
    session.add(routine)
    session.flush()
    session.add(
        SessionExercise(
            program_session_id=routine.id,
            exercise_name="Squat",
            movement_pattern="squat",
            sets=3,
            reps=5,
            rest_seconds=90,
            order_index=0,
        )
    )
    session.commit()
    try:
        with tenant_scope(TenantIdentity(user_id)):
            result = compile_and_schedule_result(
                session,
                {
                    "action": "schedule_session",
                    "program_session_id": routine.id,
                    "title": routine.name,
                    "activity_type": "strength_training",
                    "target_date": "2026-07-30",
                    "suggested_time": "18:00",
                    "duration_min": 60,
                    "intensity": "normal",
                },
            )
    finally:
        set_garmin_registry_for_testing(None)

    restored = RestoredClient.instances[-1]
    assert result.ok
    assert restored.ensure_calls == 1
    assert restored.login_calls == 0
    assert restored.api.scheduled == [(808, "2026-07-30")]
    assert session.query(PlannedSession).count() == 1
