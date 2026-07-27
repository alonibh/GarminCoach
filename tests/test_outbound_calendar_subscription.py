from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from icalendar import Calendar
from sqlalchemy.orm import sessionmaker

import app as app_module
import config
import time_utils
from control_db import (
    IntegrationRoute,
    User,
    calendar_feed_token_hash,
    generate_calendar_feed_token,
    init_control_db,
    create_control_engine,
)
from db import PlannedSession, SyncState
from tenant_store import get_user_session, provision_user_store


def _calendar_test_app(monkeypatch, tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    init_control_db(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def sessions():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(app_module, "get_control_session", sessions)
    monkeypatch.setattr("account_routes.get_control_session", sessions)
    monkeypatch.setattr(time_utils, "get_local_date", lambda: date(2026, 7, 27))
    return TestClient(app_module.app), Session, engine


def _user_with_feed(Session, tmp_path, *, email="athlete@example.com", status="active"):
    user = User(
        id=str(uuid4()), email=email, role="athlete", status=status,
        timezone="Asia/Jerusalem",
    )
    token = generate_calendar_feed_token()
    with Session.begin() as session:
        session.add(user)
        session.flush()
        session.add(IntegrationRoute(
            user_id=user.id,
            calendar_feed_token_hash=calendar_feed_token_hash(token),
        ))
    provision_user_store(user.id, tmp_path / "users")
    return user, token


def _add_session(user, tmp_path, *, title, target_day=date(2026, 7, 27), status="approved"):
    with get_user_session(user.id, tmp_path / "users") as session:
        stale = session.get(SyncState, "coach_calendar_events")
        if stale is None:
            session.add(SyncState(key="coach_calendar_events", value="not valid JSON"))
        else:
            stale.value = "not valid JSON"
        session.add(PlannedSession(
            title=title,
            target_date=target_day,
            suggested_time="18:00",
            duration_min=60,
            status=status,
            created_at=datetime(2026, 7, 1, 10, 0),
            updated_at=datetime(2026, 7, 2, 10, 0),
        ))


def test_multi_user_legacy_calendar_is_gone_not_a_login_redirect(monkeypatch, tmp_path):
    client, _Session, engine = _calendar_test_app(monkeypatch, tmp_path)
    response = client.get("/calendar/coach.ics", follow_redirects=False)
    assert response.status_code == 410
    assert response.headers["content-type"].startswith("text/plain")
    assert "private" in response.text.lower()
    engine.dispose()


def test_private_calendar_feed_is_public_tenant_scoped_and_stable(monkeypatch, tmp_path, caplog):
    client, Session, engine = _calendar_test_app(monkeypatch, tmp_path)
    first, first_token = _user_with_feed(Session, tmp_path)
    second, second_token = _user_with_feed(Session, tmp_path, email="other@example.com")
    _add_session(first, tmp_path, title="Monday strength")
    _add_session(second, tmp_path, title="Other athlete workout")
    _add_session(first, tmp_path, title="Not approved", status="draft")

    url = f"/calendar/feed/{first_token}.ics"
    first_response = client.get(url, follow_redirects=False)
    repeated_response = client.get(url, follow_redirects=False)

    assert first_response.status_code == 200
    assert first_response.headers["content-type"].startswith("text/calendar")
    assert first_response.text.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" in first_response.text
    assert "DTSTART:20260727T150000Z" in first_response.text
    assert "DTEND:20260727T160000Z" in first_response.text
    assert "SUMMARY:Monday strength" in first_response.text
    assert "STATUS:CONFIRMED" in first_response.text
    assert "Other athlete workout" not in first_response.text
    assert "Not approved" not in first_response.text
    assert first_response.text == repeated_response.text
    assert first_token not in "\n".join(record.getMessage() for record in caplog.records)
    assert first.email not in "\n".join(record.getMessage() for record in caplog.records)
    assert first.id not in "\n".join(record.getMessage() for record in caplog.records)

    calendar = Calendar.from_ical(first_response.content)
    event = next(component for component in calendar.walk() if component.name == "VEVENT")
    assert str(event["UID"]).endswith("@garmincoach")
    assert first.id not in str(event["UID"])
    assert first_token not in str(event["UID"])
    assert str(event["DTSTAMP"]) == "vDDDTypes(2026-07-02 10:00:00+00:00, Parameters({}))"

    # The second user's valid token is a separate tenant and exposes only them.
    second_response = client.get(f"/calendar/feed/{second_token}.ics", follow_redirects=False)
    assert "Other athlete workout" in second_response.text
    assert "Monday strength" not in second_response.text
    engine.dispose()


def test_private_calendar_token_rejects_invalid_regenerated_and_disabled_users(monkeypatch, tmp_path):
    client, Session, engine = _calendar_test_app(monkeypatch, tmp_path)
    active, old_token = _user_with_feed(Session, tmp_path)
    _add_session(active, tmp_path, title="Visible workout")
    assert client.get("/calendar/feed/not-a-token.ics", follow_redirects=False).status_code == 404

    replacement = generate_calendar_feed_token()
    with Session.begin() as session:
        route = session.get(IntegrationRoute, active.id)
        route.calendar_feed_token_hash = calendar_feed_token_hash(replacement)
    assert client.get(f"/calendar/feed/{old_token}.ics", follow_redirects=False).status_code == 404
    assert client.get(f"/calendar/feed/{replacement}.ics", follow_redirects=False).status_code == 200

    disabled, disabled_token = _user_with_feed(
        Session, tmp_path, email="disabled@example.com", status="disabled"
    )
    _add_session(disabled, tmp_path, title="Never visible")
    assert client.get(f"/calendar/feed/{disabled_token}.ics", follow_redirects=False).status_code == 404
    engine.dispose()
