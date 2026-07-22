"""Minimal cross-user control plane.

This database intentionally contains identity and routing metadata only. Garmin
health data, coaching records, secrets, and calendar contents belong in each
user's isolated athlete store.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, create_engine, event
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


class AuditEvent(ControlBase):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    subject_ref: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def create_control_engine(path: Path | str = config.CONTROL_DB_PATH) -> Engine:
    db_path = Path(path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
    ControlBase.metadata.create_all(engine or get_control_engine())


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
