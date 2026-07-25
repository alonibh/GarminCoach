from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

import sync.garmin_client as garmin_client_module
from secret_vault import UserSecretVault, VaultError
from sync.garmin_client import GarminClient
from sync.garmin_registry import GarminClientRegistry


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
    client.complete_mfa("123456")
    assert pending_api.resumed_with == (continuation, "123456")
    assert client.is_authenticated()
    assert client._pending_state is None
