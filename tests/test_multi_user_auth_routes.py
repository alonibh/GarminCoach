from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import auth_routes
import config
from auth_service import issue_invitation, token_hash
from control_db import ControlBase, User, WebSession, create_control_engine


def _test_app(monkeypatch, tmp_path: Path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
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

    monkeypatch.setattr(auth_routes, "get_control_session", sessions)
    monkeypatch.setattr("app.get_control_session", sessions)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "https://example.test/auth/google/callback")
    monkeypatch.setattr(config, "OWNER_GOOGLE_EMAIL", "owner@example.com")
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app), Session, engine


def test_auth_routes_are_hidden_until_multi_user_mode(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)
    app = FastAPI()
    app.include_router(auth_routes.router)
    assert TestClient(app).get("/auth/login").status_code == 404


def test_invitation_token_moves_from_fragment_to_http_only_ticket(monkeypatch, tmp_path):
    client, Session, engine = _test_app(monkeypatch, tmp_path)
    with Session.begin() as session:
        owner = User(
            id=str(uuid4()),
            google_sub="owner-sub",
            email="owner@example.com",
            role="owner",
            status="active",
        )
        session.add(owner)
        session.flush()
        _invitation, raw_invitation = issue_invitation(
            session, owner=owner, email="invitee@example.com"
        )

    response = client.post(
        "/auth/enrollment", data={"invitation_token": raw_invitation}
    )
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "__Host-gc_enroll=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert raw_invitation not in cookie
    engine.dispose()


def test_owner_google_callback_sets_only_hashed_server_session(monkeypatch, tmp_path):
    client, Session, engine = _test_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        auth_routes, "provision_user_store", lambda _user_id, **_kwargs: None
    )

    start = client.get("/auth/google/start", follow_redirects=False)
    assert start.status_code == 303
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    def fake_exchange(_code, attempt, *, redirect_uri):
        assert redirect_uri == config.GOOGLE_REDIRECT_URI
        return {
            "sub": "owner-google-sub",
            "email": "owner@example.com",
            "email_verified": True,
            "nonce": attempt.nonce,
        }

    monkeypatch.setattr(auth_routes, "exchange_code", fake_exchange)
    callback = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "one-use-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/onboarding"
    cookie = callback.headers["set-cookie"]
    assert "__Host-gc_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie

    raw = cookie.split("__Host-gc_session=", 1)[1].split(";", 1)[0]
    with Session() as session:
        user = session.query(User).one()
        stored = session.query(WebSession).one()
        assert user.role == "owner"
        assert stored.token_hash == token_hash(raw)
        assert raw not in stored.token_hash
    engine.dispose()


def test_cookie_auth_middleware_allows_navigation_during_onboarding(monkeypatch, tmp_path):
    from app import CookieAuthMiddleware
    from auth_service import create_web_session
    client, Session, engine = _test_app(monkeypatch, tmp_path)
    app = FastAPI()
    app.add_middleware(CookieAuthMiddleware)

    @app.get("/")
    def index():
        return {"ok": True}

    with Session.begin() as session:
        user = User(
            id=str(uuid4()),
            email="onboarding_user@example.com",
            status="onboarding",
            role="owner",
        )
        session.add(user)
        raw_token = create_web_session(session, user)

    test_client = TestClient(app)
    test_client.cookies.set("__Host-gc_session", raw_token)
    res = test_client.get("/", follow_redirects=False)
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    engine.dispose()

