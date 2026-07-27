from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from garminconnect import GarminConnectConnectionError

from secret_vault import UserSecretVault


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "garmin_tokens"
def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _pre_030_serialized() -> str:
    payload = json.dumps(_fixture("pre_030_decoded.json"), separators=(",", ":"))
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def test_pre_030_token_serialization_is_base64_oauth_pair():
    serialized = _pre_030_serialized()
    decoded = json.loads(base64.b64decode(serialized))

    assert isinstance(decoded, list)
    assert len(decoded) == 2
    assert set(decoded[0]) == {
        "oauth_token",
        "oauth_token_secret",
        "mfa_token",
        "mfa_expiration_timestamp",
        "domain",
    }
    assert set(decoded[1]) == {
        "scope",
        "jti",
        "token_type",
        "access_token",
        "refresh_token",
        "expires_in",
        "expires_at",
        "refresh_token_expires_in",
        "refresh_token_expires_at",
    }


def test_037_token_serialization_is_native_di_json_object():
    token_data = _fixture("garminconnect_037.json")

    assert set(token_data) == {"di_token", "di_refresh_token", "di_client_id"}
    assert all(value.startswith("synthetic-") for value in token_data.values())


def test_synthetic_token_round_trips_through_encrypted_vault(tmp_path):
    serialized = json.dumps(_fixture("garminconnect_037.json"), separators=(",", ":"))
    vault = UserSecretVault(Fernet.generate_key())
    user_id = "00000000-0000-0000-0000-000000000037"

    vault.update(user_id, root=tmp_path, garmin_tokens=serialized)

    encrypted = vault.path_for(user_id, root=tmp_path).read_bytes()
    assert b"synthetic-di-access-token" not in encrypted
    assert vault.read(user_id, root=tmp_path)["garmin_tokens"] == serialized


def test_037_accepts_its_synthetic_token_structure_without_network_calls():
    from garminconnect import Garmin

    api = Garmin()
    api.client.loads(json.dumps(_fixture("garminconnect_037.json")))

    assert api.client.is_authenticated
    assert json.loads(api.client.dumps()) == _fixture("garminconnect_037.json")


def test_037_rejects_pre_030_token_structure_and_requires_fresh_login():
    from garminconnect import Garmin

    api = Garmin()
    with pytest.raises(
        GarminConnectConnectionError,
        match=r"Token extraction loads\(\) structurally failed",
    ):
        api.client.loads(_pre_030_serialized())

    assert not api.client.is_authenticated


def test_token_fixtures_are_synthetic():
    serialized = json.dumps(
        [_fixture("pre_030_decoded.json"), _fixture("garminconnect_037.json")]
    )
    assert "synthetic-" in serialized
    assert "@" not in serialized
    assert "password" not in serialized.lower()
