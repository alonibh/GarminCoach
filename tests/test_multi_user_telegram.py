from contextlib import contextmanager
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

import app as app_module
import config
from control_db import ControlBase, IntegrationRoute, User, create_control_engine
from notify import telegram
from secret_vault import UserSecretVault
from tenant_context import current_tenant, require_tenant, tenant_scope
import telegram_link


def _environment(monkeypatch, tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def sessions():
        with Session.begin() as session:
            yield session

    monkeypatch.setattr(telegram_link, "get_control_session", sessions)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(config, "DATA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tmp_path / "users")
    return engine, Session


def test_two_chats_resolve_to_only_their_linked_tenants(monkeypatch, tmp_path):
    engine, Session = _environment(monkeypatch, tmp_path)
    first, second = str(uuid4()), str(uuid4())
    with Session.begin() as session:
        session.add_all([
            User(id=first, email="first@example.com", status="active", timezone="UTC"),
            User(id=second, email="second@example.com", status="active", timezone="Asia/Jerusalem"),
        ])

    first_code = telegram_link.issue_link_code(first)
    second_code = telegram_link.issue_link_code(second)
    first_identity = telegram_link.consume_link_code(first_code, "111111")
    second_identity = telegram_link.consume_link_code(second_code, "222222")

    assert telegram_link.resolve_chat_tenant("111111").user_id == first
    assert telegram_link.resolve_chat_tenant("222222").user_id == second
    assert first_identity.user_id != second_identity.user_id
    with Session() as session:
        routes = session.query(IntegrationRoute).order_by(IntegrationRoute.user_id).all()
        assert len(routes) == 2
        assert all(route.telegram_chat_hmac not in {"111111", "222222"} for route in routes)
    for user_id, raw_chat in ((first, b"111111"), (second, b"222222")):
        vault_path = UserSecretVault().path_for(user_id)
        assert raw_chat not in vault_path.read_bytes()
    with pytest.raises(ValueError, match="invalid or expired"):
        telegram_link.consume_link_code(first_code, "333333")
    engine.dispose()


def test_chat_cannot_be_claimed_by_a_second_user(monkeypatch, tmp_path):
    engine, Session = _environment(monkeypatch, tmp_path)
    first, second = str(uuid4()), str(uuid4())
    with Session.begin() as session:
        session.add_all([
            User(id=first, email="first@example.com", status="active"),
            User(id=second, email="second@example.com", status="active"),
        ])
    telegram_link.consume_link_code(telegram_link.issue_link_code(first), "111111")
    with pytest.raises(ValueError, match="already linked"):
        telegram_link.consume_link_code(telegram_link.issue_link_code(second), "111111")
    assert telegram_link.resolve_chat_tenant("111111").user_id == first
    engine.dispose()


def test_outbound_delivery_cannot_override_current_tenants_chat(monkeypatch, tmp_path):
    engine, Session = _environment(monkeypatch, tmp_path)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(id=user_id, email="first@example.com", status="active"))
    identity = telegram_link.consume_link_code(
        telegram_link.issue_link_code(user_id), "111111"
    )
    sent = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(
        telegram,
        "_make_request",
        lambda url, payload: sent.append((url, payload)) or True,
    )
    with tenant_scope(identity):
        assert telegram.send_message("own message") is True
        assert telegram.send_message("cross-user message", chat_id="222222") is False
    assert [item[1]["chat_id"] for item in sent] == ["111111"]
    engine.dispose()


def test_unlink_removes_route_and_encrypted_chat_value(monkeypatch, tmp_path):
    engine, Session = _environment(monkeypatch, tmp_path)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(id=user_id, email="first@example.com", status="active"))
    telegram_link.consume_link_code(telegram_link.issue_link_code(user_id), "111111")
    telegram_link.unlink_user(user_id)
    assert telegram_link.resolve_chat_tenant("111111") is None
    assert "telegram_chat_id" not in UserSecretVault().read(user_id)
    with Session() as session:
        assert session.get(User, user_id).telegram_linked is False
    engine.dispose()


def test_webhook_binds_resolved_tenant_before_handling_command(monkeypatch, tmp_path):
    engine, Session = _environment(monkeypatch, tmp_path)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(
            id=user_id,
            email="first@example.com",
            status="active",
            timezone="UTC",
        ))
    code = telegram_link.issue_link_code(user_id)
    sent = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setattr(
        telegram, "_make_request", lambda _url, payload: sent.append(payload) or True
    )
    client = TestClient(app_module.app)
    linked = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        json={"message": {
            "chat": {"id": 111111, "type": "private"},
            "text": f"/start link_{code}",
        }},
    )
    assert linked.status_code == 200
    assert telegram_link.resolve_chat_tenant("111111").user_id == user_id

    seen = []
    monkeypatch.setattr(app_module, "get_session", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "coach.coach.handle_chat",
        lambda *_args: (
            seen.append(require_tenant().user_id)
            or ("Private response", SimpleNamespace(pending_action_json=None, content="Private response"))
        ),
    )
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        json={"message": {
            "chat": {"id": 111111, "type": "private"},
            "text": "Metrics",
        }},
    )
    assert response.status_code == 200
    assert seen == [user_id]
    assert current_tenant() is None
    assert any(payload.get("text") == "Private response" for payload in sent)
    engine.dispose()
