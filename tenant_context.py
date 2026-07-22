"""Request/job tenant identity.

Tenant selection is a trusted server-side operation. Web routes derive it from
the authenticated session; background jobs receive it from the control DB. A
caller must never build tenant context from a request path or form field.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantIdentity:
    user_id: str
    role: str = "athlete"
    timezone: str | None = None

    def __post_init__(self) -> None:
        try:
            canonical = str(UUID(self.user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Tenant user_id must be a canonical UUID") from exc
        if canonical != self.user_id:
            raise ValueError("Tenant user_id must be a canonical UUID")
        if self.role not in {"athlete", "owner"}:
            raise ValueError("Unknown tenant role")


_current_tenant: ContextVar[TenantIdentity | None] = ContextVar(
    "garmincoach_current_tenant", default=None
)


def current_tenant() -> TenantIdentity | None:
    return _current_tenant.get()


def require_tenant() -> TenantIdentity:
    tenant = current_tenant()
    if tenant is None:
        raise RuntimeError("No authenticated tenant is bound to this operation")
    return tenant


@contextmanager
def tenant_scope(tenant: TenantIdentity) -> Iterator[TenantIdentity]:
    token: Token = _current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        _current_tenant.reset(token)
