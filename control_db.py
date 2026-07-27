"""Minimal cross-user control plane.

This database intentionally contains identity and routing metadata only. Garmin
health data, coaching records, secrets, and calendar contents belong in each
user's isolated athlete store.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

import config


class ControlBase(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    """Naive UTC for SQLite while avoiding deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(ControlBase):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="athlete")
    status: Mapped[str] = mapped_column(String(24), default="onboarding", index=True)
    onboarding_step: Mapped[str] = mapped_column(String(32), default="consent")
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    consent_version: Mapped[Optional[str]] = mapped_column(String(32))
    consented_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    garmin_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    inbound_calendar_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Invitation(ControlBase):
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    consumed_by_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (Index("ix_invitations_email_expires", "email", "expires_at"),)


class WebSession(ControlBase):
    __tablename__ = "web_sessions"

    # Only the SHA-256 hash of the opaque browser token is persisted.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class EnrollmentTicket(ControlBase):
    """Short-lived bridge from a URL-fragment invitation to the OIDC flow."""

    __tablename__ = "enrollment_tickets"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    invitation_id: Mapped[str] = mapped_column(
        ForeignKey("invitations.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class OAuthAttempt(ControlBase):
    """One-use server-side OIDC state, nonce, and PKCE verifier."""

    __tablename__ = "oauth_attempts"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    invitation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("invitations.id", ondelete="CASCADE"), index=True
    )
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class IntegrationRoute(ControlBase):
    __tablename__ = "integration_routes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    telegram_chat_hmac: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, index=True
    )
    calendar_feed_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


_CALENDAR_FEED_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def generate_calendar_feed_token() -> str:
    """Return an opaque 256-bit token suitable for an outbound ICS URL."""
    return secrets.token_urlsafe(32)


def valid_calendar_feed_token(token: str) -> bool:
    """Accept only the exact URL-safe encoding generated for feed tokens."""
    return bool(isinstance(token, str) and _CALENDAR_FEED_TOKEN_RE.fullmatch(token))


def calendar_feed_token_hash(token: str) -> str:
    """Return the persisted representation of an outbound ICS token."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class TelegramLinkTicket(ControlBase):
    __tablename__ = "telegram_link_tickets"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AskCoachConsent(ControlBase):
    __tablename__ = "ask_coach_consents"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    consent_version: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    data_categories_version: Mapped[str] = mapped_column(String(32))
    data_categories_json: Mapped[str] = mapped_column(Text)
    category_hash: Mapped[str] = mapped_column(String(64))
    consented_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)


class AuditEvent(ControlBase):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    subject_ref: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def create_control_engine(path: Path | str | None = None) -> Engine:
    target_path = path if path is not None else config.CONTROL_DB_PATH
    db_path = Path(target_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from tenant_store import verify_and_repair_sqlite
    verify_and_repair_sqlite(db_path)
    engine = create_engine(
        f"sqlite:///{db_path}", future=True, connect_args={"timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


_control_engine: Engine | None = None
_control_session_factory = None


def get_control_engine() -> Engine:
    global _control_engine, _control_session_factory
    if _control_engine is None:
        _control_engine = create_control_engine()
        _control_session_factory = sessionmaker(
            bind=_control_engine, expire_on_commit=False, future=True
        )
    return _control_engine


def init_control_db(engine: Engine | None = None) -> None:
    target = engine or get_control_engine()
    ControlBase.metadata.create_all(target)
    from control_db_migration import run_control_migrations

    run_control_migrations(target)


def dispose_control_engine() -> None:
    global _control_engine, _control_session_factory
    engine = _control_engine
    _control_engine = None
    _control_session_factory = None
    if engine is not None:
        engine.dispose()


def canonicalize_categories(
    categories: tuple[str, ...] | list[str],
) -> tuple[str, str]:
    """Return canonical JSON and SHA-256 for category identifiers."""
    cleaned: list[str] = []
    for category in categories:
        if not isinstance(category, str):
            raise TypeError("Category identifiers must be strings")
        stripped = category.strip().lower()
        if not stripped:
            raise ValueError("Empty category identifier")
        cleaned.append(stripped)
    canonical_json = json.dumps(
        sorted(set(cleaned)), separators=(",", ":"), sort_keys=True
    )
    category_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return canonical_json, category_hash


def get_ask_coach_consent(user_id: str) -> AskCoachConsent | None:
    with get_control_session() as session:
        return session.get(AskCoachConsent, user_id)


def record_ask_coach_consent(
    user_id: str,
    version: str,
    provider: str,
    categories_version: str,
    categories: tuple[str, ...] | list[str],
) -> AskCoachConsent:
    canonical_json, category_hash = canonicalize_categories(categories)
    with get_control_session() as session:
        consent = session.get(AskCoachConsent, user_id)
        if consent is None:
            consent = AskCoachConsent(user_id=user_id)
            session.add(consent)
        consent.consent_version = version
        consent.provider = provider
        consent.data_categories_version = categories_version
        consent.data_categories_json = canonical_json
        consent.category_hash = category_hash
        consent.consented_at = utcnow()
        consent.revoked_at = None
        session.flush()
        session.expunge(consent)
        return consent


def revoke_ask_coach_consent(user_id: str) -> AskCoachConsent | None:
    with get_control_session() as session:
        consent = session.get(AskCoachConsent, user_id)
        if consent is not None:
            consent.revoked_at = utcnow()
            session.flush()
            session.expunge(consent)
        return consent


def is_consent_valid(consent: AskCoachConsent | None) -> bool:
    if consent is None or consent.revoked_at is not None:
        return False
    if consent.consent_version != config.ASK_COACH_CONSENT_VERSION:
        return False
    if consent.provider != config.ASK_COACH_PROVIDER:
        return False
    if (
        consent.data_categories_version
        != config.ASK_COACH_DATA_CATEGORIES_VERSION
    ):
        return False
    try:
        stored = json.loads(consent.data_categories_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(stored, list) or not all(
        isinstance(item, str) for item in stored
    ):
        return False
    try:
        _, stored_hash = canonicalize_categories(stored)
        _, expected_hash = canonicalize_categories(
            config.CURRENT_ASK_COACH_DATA_CATEGORIES
        )
    except (TypeError, ValueError):
        return False
    return (
        hmac.compare_digest(stored_hash, consent.category_hash)
        and hmac.compare_digest(stored_hash, expected_hash)
    )


@contextmanager
def get_control_session() -> Iterator:
    global _control_session_factory
    get_control_engine()
    session = _control_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
