"""Encrypted per-user integration secrets.

The server owner remains trusted, but a database mix-up or accidental file
read must not expose another athlete's Garmin tokens or calendar credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

import config
from tenant_store import user_root


class VaultError(RuntimeError):
    pass


class UserSecretVault:
    def __init__(self, key: str | bytes | None = None) -> None:
        raw_key = key or config.DATA_ENCRYPTION_KEY
        if isinstance(raw_key, str):
            raw_key = raw_key.encode("ascii")
        if not raw_key:
            raise VaultError("DATA_ENCRYPTION_KEY is not configured")
        try:
            self._fernet = Fernet(raw_key)
        except (ValueError, TypeError) as exc:
            raise VaultError("DATA_ENCRYPTION_KEY is invalid") from exc

    def path_for(self, user_id: str, *, root: Path | str | None = None) -> Path:
        return user_root(user_id, root or config.MULTI_USER_DATA_ROOT) / "secrets.vault"

    def read(self, user_id: str, *, root: Path | str | None = None) -> dict[str, Any]:
        path = self.path_for(user_id, root=root)
        if not path.exists():
            return {}
        try:
            plaintext = self._fernet.decrypt(path.read_bytes())
            parsed = json.loads(plaintext.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("vault root is not an object")
            return parsed
        except (InvalidToken, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise VaultError("User secret vault cannot be decrypted") from exc

    def write(
        self,
        user_id: str,
        values: dict[str, Any],
        *,
        root: Path | str | None = None,
    ) -> None:
        path = self.path_for(user_id, root=root)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        encrypted = self._fernet.encrypt(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        fd, temporary_name = tempfile.mkstemp(prefix=".vault-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()

    def update(
        self,
        user_id: str,
        *,
        root: Path | str | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        current = self.read(user_id, root=root)
        for key, value in values.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        self.write(user_id, current, root=root)
        return current
