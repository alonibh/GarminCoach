from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

import config
from auth_service import (
    AuthenticationError,
    begin_google_oidc,
    bind_google_identity,
    consume_oauth_attempt,
    create_web_session,
    exchange_invitation_for_enrollment,
    issue_invitation,
    normalize_email,
    resolve_web_session,
    revoke_web_session,
    token_hash,
)
from control_db import ControlBase, Invitation, User, WebSession, create_control_engine, utcnow


@pytest.fixture
def control_session(tmp_path: Path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _owner(session) -> User:
    owner = User(
        id=str(uuid4()),
        google_sub="owner-sub",
        email="owner@example.com",
        role="owner",
        status="active",
        onboarding_step="complete",
    )
    session.add(owner)
    session.flush()
    return owner


def _claims(attempt, *, sub="new-sub", email="invitee@example.com"):
    return {
        "sub": sub,
        "email": email,
        "email_verified": True,
        "nonce": attempt.nonce,
    }


def test_invitation_is_hashed_single_use_and_seven_days(control_session):
    owner = _owner(control_session)
    now = utcnow()
    invitation, raw = issue_invitation(
        control_session, owner=owner, email=" Invitee@Example.com ", now=now
    )
    assert invitation.email == "invitee@example.com"
    assert invitation.token_hash == token_hash(raw)
    assert raw not in invitation.token_hash
    assert invitation.expires_at == now + timedelta(days=7)


def test_new_invitation_revokes_previous_live_one(control_session):
    owner = _owner(control_session)
    first, _ = issue_invitation(control_session, owner=owner, email="a@example.com")
    second, _ = issue_invitation(control_session, owner=owner, email="a@example.com")
    assert first.revoked_at is not None
    assert second.revoked_at is None


def test_non_owner_cannot_issue_invitation(control_session):
    athlete = User(
        id=str(uuid4()), email="athlete@example.com", role="athlete", status="active"
    )
    control_session.add(athlete)
    control_session.flush()
    with pytest.raises(PermissionError):
        issue_invitation(control_session, owner=athlete, email="a@example.com")


def test_fragment_invitation_exchanges_to_short_lived_enrollment(control_session, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id")
    owner = _owner(control_session)
    invitation, raw = issue_invitation(control_session, owner=owner, email="a@example.com")
    ticket = exchange_invitation_for_enrollment(control_session, raw)
    start = begin_google_oidc(
        control_session,
        redirect_uri="https://example.test/auth/google/callback",
        enrollment_ticket=ticket,
    )
    query = parse_qs(urlparse(start.authorization_url).query)
    assert query["scope"] == ["openid email"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["login_hint"] == ["a@example.com"]
    attempt = consume_oauth_attempt(control_session, start.state)
    assert attempt.invitation_id == invitation.id
    with pytest.raises(AuthenticationError):
        consume_oauth_attempt(control_session, start.state)


def test_invited_google_email_must_match_exactly(control_session, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id")
    owner = _owner(control_session)
    invitation, raw = issue_invitation(
        control_session, owner=owner, email="invitee@example.com"
    )
    ticket = exchange_invitation_for_enrollment(control_session, raw)
    start = begin_google_oidc(
        control_session, redirect_uri="https://example.test/callback", enrollment_ticket=ticket
    )
    attempt = consume_oauth_attempt(control_session, start.state)
    with pytest.raises(AuthenticationError, match="does not match"):
        bind_google_identity(
            control_session,
            attempt=attempt,
            claims=_claims(attempt, email="other@example.com"),
            owner_email="owner@example.com",
        )
    assert invitation.consumed_at is None


def test_invited_identity_binds_google_sub_and_consumes_invitation(control_session, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id")
    owner = _owner(control_session)
    invitation, raw = issue_invitation(
        control_session, owner=owner, email="invitee@example.com"
    )
    ticket = exchange_invitation_for_enrollment(control_session, raw)
    start = begin_google_oidc(
        control_session, redirect_uri="https://example.test/callback", enrollment_ticket=ticket
    )
    attempt = consume_oauth_attempt(control_session, start.state)
    user = bind_google_identity(
        control_session,
        attempt=attempt,
        claims=_claims(attempt),
        owner_email="owner@example.com",
    )
    assert user.google_sub == "new-sub"
    assert user.role == "athlete"
    assert invitation.consumed_by_user_id == user.id


def test_first_verified_matching_identity_bootstraps_owner(control_session, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id")
    start = begin_google_oidc(
        control_session, redirect_uri="https://example.test/callback"
    )
    attempt = consume_oauth_attempt(control_session, start.state)
    user = bind_google_identity(
        control_session,
        attempt=attempt,
        claims=_claims(attempt, sub="owner-sub", email="owner@example.com"),
        owner_email="owner@example.com",
    )
    assert user.role == "owner"
    assert user.status == "onboarding"


def test_first_non_owner_email_is_rejected(control_session, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id")
    start = begin_google_oidc(control_session, redirect_uri="https://example.test/callback")
    attempt = consume_oauth_attempt(control_session, start.state)
    with pytest.raises(AuthenticationError, match="not invited"):
        bind_google_identity(
            control_session,
            attempt=attempt,
            claims=_claims(attempt, email="other@example.com"),
            owner_email="owner@example.com",
        )


def test_unverified_email_and_wrong_nonce_are_rejected(control_session):
    from control_db import OAuthAttempt

    attempt = OAuthAttempt(
        state_hash="s" * 64,
        nonce="expected",
        code_verifier="v" * 64,
        expires_at=utcnow() + timedelta(minutes=10),
    )
    claims = {"sub": "sub", "email": "a@example.com", "email_verified": False, "nonce": "expected"}
    with pytest.raises(AuthenticationError, match="not verified"):
        bind_google_identity(
            control_session, attempt=attempt, claims=claims, owner_email="a@example.com"
        )
    claims["email_verified"] = True
    claims["nonce"] = "wrong"
    with pytest.raises(AuthenticationError, match="invalid"):
        bind_google_identity(
            control_session, attempt=attempt, claims=claims, owner_email="a@example.com"
        )


def test_opaque_web_session_is_hashed_expiring_and_revocable(control_session):
    owner = _owner(control_session)
    now = utcnow()
    raw = create_web_session(
        control_session, owner, now=now, lifetime=timedelta(minutes=5)
    )
    stored = control_session.get(WebSession, token_hash(raw))
    assert stored is not None
    assert raw != stored.token_hash
    assert resolve_web_session(control_session, raw, now=now) == owner
    assert resolve_web_session(
        control_session, raw, now=now + timedelta(minutes=6)
    ) is None
    revoke_web_session(control_session, raw, now=now)
    assert resolve_web_session(control_session, raw, now=now) is None


@pytest.mark.parametrize("email", ["", "missing-at", "a@", "@example.com", "a b@example.com"])
def test_invalid_invitation_emails_fail_closed(email):
    with pytest.raises(ValueError):
        normalize_email(email)
