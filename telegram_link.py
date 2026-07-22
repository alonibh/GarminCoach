"""Secure mapping between one shared Telegram bot and isolated tenants."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import secrets

import config
from auth_service import token_hash
from control_db import (
    IntegrationRoute,
    TelegramLinkTicket,
    User,
    get_control_session,
    utcnow,
)
from secret_vault import UserSecretVault
from tenant_context import TenantIdentity, require_tenant


LINK_LIFETIME = timedelta(minutes=10)


def _chat_hmac(chat_id: str) -> str:
    if not config.DATA_ENCRYPTION_KEY:
        raise RuntimeError("DATA_ENCRYPTION_KEY is not configured")
    key = hashlib.sha256(
        ("garmincoach:telegram-routing:" + config.DATA_ENCRYPTION_KEY).encode("utf-8")
    ).digest()
    return hmac.new(key, str(chat_id).encode("utf-8"), hashlib.sha256).hexdigest()


def issue_link_code(user_id: str) -> str:
    now = utcnow()
    raw = secrets.token_urlsafe(16)
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None or user.status != "active":
            raise PermissionError("Only active users can link Telegram")
        for ticket in session.query(TelegramLinkTicket).filter_by(user_id=user_id).all():
            if ticket.consumed_at is None:
                ticket.consumed_at = now
        session.add(TelegramLinkTicket(
            token_hash=token_hash(raw),
            user_id=user_id,
            created_at=now,
            expires_at=now + LINK_LIFETIME,
        ))
    return raw


def consume_link_code(raw_code: str, chat_id: str) -> TenantIdentity:
    now = utcnow()
    route_hash = _chat_hmac(chat_id)
    with get_control_session() as session:
        ticket = session.get(TelegramLinkTicket, token_hash(raw_code.strip()))
        if ticket is None or ticket.consumed_at is not None or ticket.expires_at <= now:
            raise ValueError("Link code is invalid or expired")
        user = session.get(User, ticket.user_id)
        if user is None or user.status != "active":
            raise ValueError("Account is unavailable")
        occupied = session.query(IntegrationRoute).filter_by(
            telegram_chat_hmac=route_hash
        ).first()
        if occupied is not None and occupied.user_id != user.id:
            raise ValueError("This Telegram chat is already linked to another account")
        route = session.get(IntegrationRoute, user.id)
        if route is None:
            route = IntegrationRoute(user_id=user.id)
            session.add(route)
        route.telegram_chat_hmac = route_hash
        route.updated_at = now
        ticket.consumed_at = now
        user.telegram_linked = True
        user.updated_at = now
        identity = TenantIdentity(user.id, role=user.role, timezone=user.timezone)
        UserSecretVault().update(user.id, telegram_chat_id=str(chat_id))
    return identity


def resolve_chat_tenant(chat_id: str) -> TenantIdentity | None:
    route_hash = _chat_hmac(chat_id)
    with get_control_session() as session:
        route = session.query(IntegrationRoute).filter_by(
            telegram_chat_hmac=route_hash
        ).first()
        user = session.get(User, route.user_id) if route else None
        if user is None or user.status != "active" or not user.telegram_linked:
            return None
        return TenantIdentity(user.id, role=user.role, timezone=user.timezone)


def chat_id_for_current_tenant() -> str | None:
    tenant = require_tenant()
    with get_control_session() as session:
        user = session.get(User, tenant.user_id)
        route = session.get(IntegrationRoute, tenant.user_id)
        if user is None or not user.telegram_linked or route is None:
            return None
    value = UserSecretVault().read(tenant.user_id).get("telegram_chat_id")
    if not value or not hmac.compare_digest(_chat_hmac(str(value)), route.telegram_chat_hmac or ""):
        return None
    return str(value)


def unlink_user(user_id: str) -> None:
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        route = session.get(IntegrationRoute, user_id)
        if route is not None:
            route.telegram_chat_hmac = None
            route.updated_at = utcnow()
        user.telegram_linked = False
        user.updated_at = utcnow()
    UserSecretVault().update(user_id, telegram_chat_id=None)
