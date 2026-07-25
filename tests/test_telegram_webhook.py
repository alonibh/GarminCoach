import asyncio

from fastapi.testclient import TestClient
import pytest

import config
from app import app
from coach.ask_coach_session import session_manager
from coach.telegram_webhook import (
    OPERATIONAL_TEXT_GUIDANCE,
    _split_plain_text,
    update_deduplicator,
)
from control_db import init_control_db

client = TestClient(app)
USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def setup_control_db():
    init_control_db()
    from control_db import IntegrationRoute, User, get_control_session, utcnow
    from secret_vault import UserSecretVault
    from telegram_link import _chat_hmac

    with get_control_session() as control_session:
        user = control_session.get(User, USER_ID)
        if not user:
            user = User(
                id=USER_ID,
                email="test@example.com",
                status="active",
                role="owner",
                timezone="UTC",
            )
            control_session.add(user)
        user.telegram_linked = True
        control_session.flush()
        route = control_session.get(IntegrationRoute, USER_ID)
        if not route:
            route = IntegrationRoute(user_id=USER_ID)
            control_session.add(route)
        route.telegram_chat_hmac = _chat_hmac("123")
        route.updated_at = utcnow()
    UserSecretVault().update(USER_ID, telegram_chat_id="123")
    update_deduplicator.clear()
    asyncio.run(session_manager.clear_all())
    yield
    asyncio.run(session_manager.clear_all())
    update_deduplicator.clear()


def _headers():
    return {
        "X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET
    }


def test_telegram_webhook_unauthorized():
    assert client.post("/telegram/webhook", json={"message": "hello"}).status_code == 401
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong_secret"},
        json={"message": "hello"},
    )
    assert response.status_code == 401


def test_telegram_webhook_payload_too_large():
    response = client.post(
        "/telegram/webhook",
        headers={
            **_headers(),
            "Content-Length": str(3 * 1024 * 1024),
        },
        json={"message": "small body"},
    )
    assert response.status_code == 413


def test_operational_text_receives_inline_menu(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, chat_id, reply_markup, kwargs)) or True
        ),
    )

    response = client.post(
        "/telegram/webhook",
        headers=_headers(),
        json={
            "update_id": 100,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "text": "Metrics",
            },
        },
    )

    assert response.status_code == 200
    assert sent[0][0] == OPERATIONAL_TEXT_GUIDANCE
    callbacks = {
        button["callback_data"]
        for row in sent[0][2]["inline_keyboard"]
        for button in row
    }
    assert {"menu:metrics", "menu:ask_coach", "menu:privacy"} <= callbacks
    assert sent[0][3]["parse_mode"] is None


def test_update_id_is_accepted_only_once(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda *args, **kwargs: sent.append((args, kwargs)) or True,
    )
    payload = {
        "update_id": 101,
        "message": {
            "chat": {"id": 123, "type": "private"},
            "text": "Metrics",
        },
    }

    assert client.post("/telegram/webhook", headers=_headers(), json=payload).status_code == 200
    assert client.post("/telegram/webhook", headers=_headers(), json=payload).status_code == 200

    assert len(sent) == 1


@pytest.mark.parametrize(
    "callback_data",
    [
        "menu:metrics",
        "decision_action_old",
        "decision_cancel_old",
        "decision_different_time_old",
        "flow:old:nonce:date:0",
        "morning_synced_old",
        "morning_anyway_old",
        "approve_workout_old",
        "reject_workout_old",
        "reschedule_workout_old",
        "catalog_details_metric_sleep",
        "unknown:callback",
    ],
)
def test_active_ask_coach_blocks_every_other_callback(
    monkeypatch, callback_data
):
    edited = []
    asyncio.run(session_manager.create_session(USER_ID, "123"))
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda text, chat_id, message_id, reply_markup=None, **kwargs: (
            edited.append((text, reply_markup, kwargs)) or True
        ),
    )

    response = client.post(
        "/telegram/webhook",
        headers=_headers(),
        json={
            "update_id": hash(callback_data) & 0x7FFFFFFF,
            "callback_query": {
                "id": "callback",
                "data": callback_data,
                "message": {
                    "message_id": 7,
                    "chat": {"id": 123, "type": "private"},
                },
            },
        },
    )

    assert response.status_code == 200
    assert "Ask Coach is active" in edited[0][0]
    assert edited[0][1]["inline_keyboard"][0][0]["callback_data"] == "ask:exit"
    assert edited[0][2]["parse_mode"] is None


def test_back_to_menu_closes_session_and_discards_late_delivery(monkeypatch):
    asyncio.run(session_manager.create_session(USER_ID, "123"))
    acquired = asyncio.run(session_manager.try_acquire_in_flight(USER_ID))
    edited = []
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda text, chat_id, message_id, reply_markup=None, **kwargs: (
            edited.append((text, reply_markup)) or True
        ),
    )

    response = client.post(
        "/telegram/webhook",
        headers=_headers(),
        json={
            "update_id": 102,
            "callback_query": {
                "id": "callback",
                "data": "ask:exit",
                "message": {
                    "message_id": 7,
                    "chat": {"id": 123, "type": "private"},
                },
            },
        },
    )

    assert response.status_code == 200
    assert not asyncio.run(session_manager.has_active_session(USER_ID))
    assert not asyncio.run(
        session_manager.validate_session_for_delivery(
            USER_ID, "123", acquired.generation_token
        )
    )
    assert edited[0][1]["inline_keyboard"]


def test_plain_text_split_is_bounded_and_lossless():
    text = ("word " * 1200).strip()
    chunks = _split_plain_text(text)
    assert all(len(chunk) <= 3800 for chunk in chunks)
    assert " ".join(chunks) == text


def test_group_chat_is_ignored(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda *args, **kwargs: sent.append(args) or True,
    )
    response = client.post(
        "/telegram/webhook",
        headers=_headers(),
        json={
            "update_id": 103,
            "message": {
                "chat": {"id": 123, "type": "group"},
                "text": "Show my readiness",
            },
        },
    )
    assert response.status_code == 200
    assert sent == []
