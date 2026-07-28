"""Per-user Garmin client registry and encrypted token checkpointing."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Callable, Iterator

import config
from secret_vault import UserSecretVault
from tenant_store import canonical_user_id, provision_user_store
from sync.garmin_client import GarminClient


class GarminClientRegistry:
    def __init__(
        self,
        *,
        vault: UserSecretVault | None = None,
        data_root: Path | str | None = None,
        client_factory: Callable[..., GarminClient] = GarminClient,
    ) -> None:
        self._vault = vault
        self._data_root = Path(data_root or config.MULTI_USER_DATA_ROOT)
        self._client_factory = client_factory
        self._clients: dict[str, GarminClient] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    @property
    def vault(self) -> UserSecretVault:
        if self._vault is None:
            self._vault = UserSecretVault()
        return self._vault

    def lock_for(self, user_id: str) -> threading.RLock:
        canonical = canonical_user_id(user_id)
        with self._guard:
            return self._locks.setdefault(canonical, threading.RLock())

    def get(self, user_id: str) -> GarminClient:
        canonical = canonical_user_id(user_id)
        with self._guard:
            existing = self._clients.get(canonical)
            if existing is not None:
                return existing
            provision_user_store(canonical, self._data_root)
            secrets = self.vault.read(canonical, root=self._data_root)
            token_json = secrets.get("garmin_tokens")
            client = self._client_factory(
                email=secrets.get("garmin_email") or "",
                token_store=config.GARMIN_TOKEN_STORE,
            )
            if isinstance(token_json, str) and token_json:
                client.restore_tokens(token_json)
            self._clients[canonical] = client
            return client

    def begin_login(self, user_id: str, email: str, password: str) -> str:
        with self.lock_for(user_id):
            client = self.get(user_id)
            result = client.begin_login(email, password)
            if result == "connected":
                self.checkpoint(user_id)
            return result

    def complete_mfa(self, user_id: str, code: str) -> None:
        with self.lock_for(user_id):
            client = self.get(user_id)
            client.complete_mfa(code)
            self.checkpoint(user_id)

    def checkpoint(self, user_id: str) -> None:
        canonical = canonical_user_id(user_id)
        with self.lock_for(canonical):
            client = self.get(canonical)
            if not client.is_authenticated():
                return
            token_json = client.serialized_tokens()
            self.vault.update(
                canonical,
                root=self._data_root,
                garmin_email=client.email,
                garmin_tokens=token_json,
            )

    def evict(self, user_id: str) -> None:
        canonical = canonical_user_id(user_id)
        with self._guard:
            self._clients.pop(canonical, None)
            self._locks.pop(canonical, None)


_registry: GarminClientRegistry | None = None


def get_garmin_registry() -> GarminClientRegistry:
    global _registry
    if _registry is None:
        _registry = GarminClientRegistry()
    return _registry


def set_garmin_registry_for_testing(registry: GarminClientRegistry | None) -> None:
    global _registry
    _registry = registry


@contextmanager
def current_garmin_client() -> Iterator[GarminClient]:
    """Yield the current tenant's client under its mutation/sync lock.

    Mutation code must use this boundary instead of the compatibility proxy so
    multi-user requests can never fall back to the legacy global client.
    """
    if not config.MULTI_USER_ENABLED:
        from sync.garmin_client import _legacy_client

        yield _legacy_client
        return

    from tenant_context import require_tenant

    tenant = require_tenant()
    registry = get_garmin_registry()
    with registry.lock_for(tenant.user_id):
        yield registry.get(tenant.user_id)
