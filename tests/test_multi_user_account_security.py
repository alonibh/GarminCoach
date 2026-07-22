from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker

import account_routes
import config
from auth_service import issue_invitation
from coach.calendar import validate_ics_url
from control_db import AuditEvent, ControlBase, User, create_control_engine, utcnow
from secret_vault import UserSecretVault


def _control_sessions(tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def sessions():
        with Session.begin() as session:
            yield session

    return engine, Session, sessions


@pytest.mark.parametrize("url", [
    "http://calendar.google.com/calendar/ical/a/basic.ics",
    "https://calendar.google.com.evil.test/a.ics",
    "https://127.0.0.1/a.ics",
    "https://user:password@calendar.google.com/a.ics",
])
def test_calendar_url_rejects_non_provider_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_ics_url(url)


def test_calendar_url_allows_google_and_icloud():
    assert validate_ics_url("https://calendar.google.com/calendar/ical/x/basic.ics")
    assert validate_ics_url("https://p123-caldav.icloud.com/published/2/example")


def test_invitation_limit_counts_accounts_and_pending_invites(monkeypatch, tmp_path):
    engine, Session, _sessions = _control_sessions(tmp_path)
    monkeypatch.setattr(config, "MAX_INVITED_USERS", 2)
    with Session.begin() as session:
        owner = User(id=str(uuid4()), email="owner@example.com", role="owner", status="active")
        session.add(owner)
        session.flush()
        issue_invitation(session, owner=owner, email="one@example.com")
        issue_invitation(session, owner=owner, email="two@example.com")
        with pytest.raises(ValueError, match="limit"):
            issue_invitation(session, owner=owner, email="three@example.com")
        # Reissuing an existing address replaces its token without consuming a slot.
        issue_invitation(session, owner=owner, email="one@example.com")
    engine.dispose()


def test_destructive_deletion_removes_store_identity_and_sessions(monkeypatch, tmp_path):
    engine, Session, sessions = _control_sessions(tmp_path)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(id=user_id, email="athlete@example.com", status="active"))
    athlete_root = tmp_path / user_id
    athlete_root.mkdir()
    (athlete_root / "athlete.db").write_text("private health data", encoding="utf-8")

    class Registry:
        def __init__(self): self.evicted = []
        def evict(self, value): self.evicted.append(value)

    registry = Registry()
    monkeypatch.setattr(account_routes, "get_control_session", sessions)
    monkeypatch.setattr(account_routes, "refresh_user_jobs", lambda _value: None)
    monkeypatch.setattr(account_routes, "get_garmin_registry", lambda: registry)
    monkeypatch.setattr(account_routes, "dispose_user_engine", lambda _value: None)
    monkeypatch.setattr(account_routes, "user_root", lambda _value: athlete_root)

    account_routes._destroy_user(user_id, actor_user_id=None)

    assert not athlete_root.exists()
    assert registry.evicted == [user_id]
    with Session() as session:
        assert session.get(User, user_id) is None
        event = session.query(AuditEvent).one()
        assert event.event_type == "account_deleted"
        assert "athlete@example.com" not in (event.subject_ref or "")
    engine.dispose()
