"""Invitation-only Google identity and opaque web-session primitives."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
import secrets
from urllib.parse import urlencode

from sqlalchemy.orm import Session

import config
from control_db import (
    EnrollmentTicket,
    Invitation,
    OAuthAttempt,
    User,
    WebSession,
    utcnow,
)


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
INVITATION_LIFETIME = timedelta(days=7)
ENROLLMENT_LIFETIME = timedelta(minutes=10)
OAUTH_ATTEMPT_LIFETIME = timedelta(minutes=10)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AuthenticationError(ValueError):
    """A generic public-safe authentication failure."""


@dataclass(frozen=True, slots=True)
class OIDCStart:
    authorization_url: str
    state: str


def normalize_email(email: str) -> str:
    normalized = (email or "").strip().casefold()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("Enter a valid email address")
    return normalized


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _secret(byte_count: int = 32) -> str:
    return secrets.token_urlsafe(byte_count)


def _is_live_invitation(invitation: Invitation, now: datetime) -> bool:
    return (
        invitation.consumed_at is None
        and invitation.revoked_at is None
        and invitation.expires_at > now
    )


def issue_invitation(
    session: Session,
    *,
    owner: User,
    email: str,
    now: datetime | None = None,
) -> tuple[Invitation, str]:
    if owner.role != "owner" or owner.status != "active":
        raise PermissionError("Only the active owner may issue invitations")
    normalized = normalize_email(email)
    if session.query(User).filter(User.email == normalized).first():
        raise ValueError("That email already has an account")
    current = now or utcnow()
    existing_live = session.query(Invitation).filter(Invitation.email == normalized).all()
    replacing = any(_is_live_invitation(item, current) for item in existing_live)
    occupied = session.query(User).filter(User.role != "owner").count()
    live_invites = sum(
        1 for item in session.query(Invitation).all()
        if _is_live_invitation(item, current)
    )
    if not replacing and occupied + live_invites >= config.MAX_INVITED_USERS:
        raise ValueError("The invitation limit has been reached")
    for old in existing_live:
        if _is_live_invitation(old, current):
            old.revoked_at = current
    raw_token = _secret()
    invitation = Invitation(
        email=normalized,
        token_hash=token_hash(raw_token),
        created_by_user_id=owner.id,
        created_at=current,
        expires_at=current + INVITATION_LIFETIME,
    )
    session.add(invitation)
    session.flush()
    return invitation, raw_token


def exchange_invitation_for_enrollment(
    session: Session, raw_invitation_token: str, *, now: datetime | None = None
) -> str:
    current = now or utcnow()
    invitation = session.query(Invitation).filter_by(
        token_hash=token_hash(raw_invitation_token)
    ).first()
    if invitation is None or not _is_live_invitation(invitation, current):
        raise AuthenticationError("Invitation is invalid or expired")
    raw_ticket = _secret()
    session.add(EnrollmentTicket(
        token_hash=token_hash(raw_ticket),
        invitation_id=invitation.id,
        created_at=current,
        expires_at=current + ENROLLMENT_LIFETIME,
    ))
    return raw_ticket


def _consume_enrollment(
    session: Session, raw_ticket: str, now: datetime
) -> Invitation:
    ticket = session.get(EnrollmentTicket, token_hash(raw_ticket))
    if ticket is None or ticket.consumed_at is not None or ticket.expires_at <= now:
        raise AuthenticationError("Enrollment session is invalid or expired")
    invitation = session.get(Invitation, ticket.invitation_id)
    if invitation is None or not _is_live_invitation(invitation, now):
        raise AuthenticationError("Invitation is invalid or expired")
    ticket.consumed_at = now
    return invitation


def begin_google_oidc(
    session: Session,
    *,
    redirect_uri: str,
    enrollment_ticket: str | None = None,
    now: datetime | None = None,
) -> OIDCStart:
    if not config.GOOGLE_CLIENT_ID:
        raise RuntimeError("GOOGLE_CLIENT_ID is not configured")
    current = now or utcnow()
    invitation = (
        _consume_enrollment(session, enrollment_ticket, current)
        if enrollment_ticket else None
    )
    state = _secret()
    nonce = _secret()
    code_verifier = _secret(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    session.add(OAuthAttempt(
        state_hash=token_hash(state),
        invitation_id=invitation.id if invitation else None,
        nonce=nonce,
        code_verifier=code_verifier,
        created_at=current,
        expires_at=current + OAUTH_ATTEMPT_LIFETIME,
    ))
    params = {
        "response_type": "code",
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if invitation:
        params["login_hint"] = invitation.email
    return OIDCStart(
        authorization_url=f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}",
        state=state,
    )


def consume_oauth_attempt(
    session: Session, raw_state: str, *, now: datetime | None = None
) -> OAuthAttempt:
    current = now or utcnow()
    attempt = session.get(OAuthAttempt, token_hash(raw_state))
    if attempt is None or attempt.consumed_at is not None or attempt.expires_at <= current:
        raise AuthenticationError("Sign-in session is invalid or expired")
    attempt.consumed_at = current
    session.flush()
    return attempt


def validate_google_claims(claims: dict, *, expected_nonce: str) -> tuple[str, str]:
    subject = str(claims.get("sub") or "")
    if not subject or len(subject) > 255:
        raise AuthenticationError("Google identity is missing")
    if claims.get("email_verified") is not True:
        raise AuthenticationError("Google email is not verified")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
        raise AuthenticationError("Sign-in session is invalid")
    try:
        email = normalize_email(str(claims.get("email") or ""))
    except ValueError as exc:
        raise AuthenticationError("Google email is missing") from exc
    return subject, email


def bind_google_identity(
    session: Session,
    *,
    attempt: OAuthAttempt,
    claims: dict,
    owner_email: str,
    now: datetime | None = None,
) -> User:
    current = now or utcnow()
    subject, email = validate_google_claims(claims, expected_nonce=attempt.nonce)

    # 1. Subject match (already linked Google account)
    existing = session.query(User).filter(User.google_sub == subject).first()
    if existing:
        if existing.status == "deleting":
            raise AuthenticationError("Account is unavailable")
        existing.email = email
        existing.updated_at = current
        return existing

    normalized_owner = normalize_email(owner_email) if owner_email else ""

    # 2. Check if signing-in email matches configured OWNER_GOOGLE_EMAIL or owner account
    is_owner_email = bool(normalized_owner and email == normalized_owner)
    owner_user = (
        session.query(User).filter(User.role == "owner").first()
        if is_owner_email or session.query(User).count() <= 1
        else None
    )

    if is_owner_email or (owner_user and not owner_user.google_sub):
        target = owner_user or session.query(User).filter(User.email == email).first()
        if target:
            if target.status == "deleting":
                raise AuthenticationError("Account is unavailable")
            target.email = email
            target.google_sub = subject
            target.role = "owner"
            target.updated_at = current
            return target

        user = User(
            id="00000000-0000-0000-0000-000000000001",
            email=email,
            google_sub=subject,
            role="owner",
            status="onboarding",
            onboarding_step="consent",
            created_at=current,
            updated_at=current,
        )
        session.add(user)
        session.flush()
        return user

    # 3. Check existing user by email
    existing_by_email = session.query(User).filter(User.email == email).first()
    if existing_by_email:
        if existing_by_email.status == "deleting":
            raise AuthenticationError("Account is unavailable")
        existing_by_email.google_sub = subject
        existing_by_email.updated_at = current
        return existing_by_email

    # 4. Bootstrap as owner if DB is empty
    if session.query(User).count() == 0:
        if normalized_owner and email != normalized_owner:
            raise AuthenticationError("This Google account is not invited")
        user = User(
            id="00000000-0000-0000-0000-000000000001",
            email=email,
            google_sub=subject,
            role="owner",
            status="onboarding",
            onboarding_step="consent",
            created_at=current,
            updated_at=current,
        )
        session.add(user)
        session.flush()
        return user

    # 5. Require invitation for new non-owner accounts
    if attempt.invitation_id is None:
        raise AuthenticationError("This Google account is not invited")
    invitation = session.get(Invitation, attempt.invitation_id)
    if invitation is None or not _is_live_invitation(invitation, current):
        raise AuthenticationError("Invitation is invalid or expired")
    if email != invitation.email:
        raise AuthenticationError("Google account does not match the invitation")
    user = User(
        email=email,
        google_sub=subject,
        role="athlete",
        status="onboarding",
        onboarding_step="consent",
        created_at=current,
        updated_at=current,
    )
    session.add(user)
    session.flush()
    invitation.consumed_at = current
    invitation.consumed_by_user_id = user.id
    return user


def create_web_session(
    session: Session,
    user: User,
    *,
    now: datetime | None = None,
    lifetime: timedelta | None = None,
) -> str:
    if user.status == "deleting":
        raise AuthenticationError("Account is unavailable")
    current = now or utcnow()
    raw_token = _secret()
    session.add(WebSession(
        token_hash=token_hash(raw_token),
        user_id=user.id,
        created_at=current,
        last_seen_at=current,
        authenticated_at=current,
        expires_at=current + (lifetime or timedelta(days=config.SESSION_MAX_AGE_DAYS)),
    ))
    return raw_token


def resolve_web_session(
    session: Session, raw_token: str, *, now: datetime | None = None
) -> User | None:
    if not raw_token:
        return None
    current = now or utcnow()
    web_session = session.get(WebSession, token_hash(raw_token))
    if (
        web_session is None
        or web_session.revoked_at is not None
        or web_session.expires_at <= current
    ):
        return None
    user = session.get(User, web_session.user_id)
    if user is None or user.status == "deleting":
        return None
    web_session.last_seen_at = current
    return user


def revoke_web_session(
    session: Session, raw_token: str, *, now: datetime | None = None
) -> None:
    if not raw_token:
        return
    web_session = session.get(WebSession, token_hash(raw_token))
    if web_session and web_session.revoked_at is None:
        web_session.revoked_at = now or utcnow()
