from fastapi.testclient import TestClient
from app import app
import config
import json
from contextlib import nullcontext
from types import SimpleNamespace

client = TestClient(app)

def test_telegram_webhook_unauthorized():
    # Missing secret token
    response = client.post("/telegram/webhook", json={"message": "hello"})
    assert response.status_code == 401

    # Wrong secret token
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong_secret"},
        json={"message": "hello"}
    )
    assert response.status_code == 401

def test_telegram_webhook_payload_too_large():
    # Payload over 2MB limit
    large_payload = "a" * (3 * 1024 * 1024)
    response = client.post(
        "/telegram/webhook",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET,
            "Content-Length": str(len(large_payload))
        },
        json={"message": large_payload}
    )
    assert response.status_code == 413


def test_actionable_message_serializes_reply_markup(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("notify.telegram.send_chat_action", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None: sent.append((text, reply_markup)) or True,
    )
    monkeypatch.setattr(
        "coach.coach.handle_chat",
        lambda *_args, **_kwargs: (
            "Confirm workout.",
            SimpleNamespace(
                pending_action_json=json.dumps({"interaction_ids": ["interaction-1"]}),
                content="Confirm workout.",
            ),
        ),
    )
    markup = {"inline_keyboard": [[{"text": "Approve", "callback_data": "decision_action_1"}]]}
    monkeypatch.setattr("coach.interactions.reply_markup_for_ids", lambda *_args: markup)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET},
        json={"message": {"chat": {"id": 123, "type": "private"}, "text": "Schedule today"}},
    )

    assert response.status_code == 200
    assert sent == [("Confirm workout.", markup)]


def test_static_catalog_menu_is_delivered_without_pending_action(monkeypatch):
    sent = []
    markup = {"keyboard": [[{"text": "Today's recommendation"}]], "is_persistent": True}
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("notify.telegram.send_chat_action", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None: sent.append((text, reply_markup)) or True,
    )
    monkeypatch.setattr(
        "coach.coach.handle_chat",
        lambda *_args, **_kwargs: (
            "Choose an action.",
            SimpleNamespace(
                pending_action_json=json.dumps({"reply_markup": markup}),
                content="Choose an action.",
            ),
        ),
    )

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET},
        json={"message": {"chat": {"id": 123, "type": "private"}, "text": "/menu"}},
    )

    assert response.status_code == 200
    assert sent == [("Choose an action.", markup)]


def test_standard_response_refreshes_the_current_persistent_menu(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("notify.telegram.send_chat_action", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None: sent.append((text, reply_markup)) or True,
    )
    monkeypatch.setattr(
        "coach.coach.handle_chat",
        lambda *_args, **_kwargs: ("Current metrics.", SimpleNamespace(pending_action_json=None, content="Current metrics.")),
    )

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET},
        json={"message": {"chat": {"id": 123, "type": "private"}, "text": "Metrics"}},
    )

    assert response.status_code == 200
    labels = [button["text"] for row in sent[0][1]["keyboard"] for button in row]
    assert "Explain recommendation" not in labels
    assert "Find a workout time" in labels


def test_state_bound_flow_button_continues_the_schedule_flow(monkeypatch):
    edited = []
    choices = []
    markup = {"inline_keyboard": [[{"text": "Approve and schedule", "callback_data": "decision_action_1"}]]}
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda text, chat_id, message_id, reply_markup=None: edited.append((text, chat_id, message_id, reply_markup)) or True,
    )
    monkeypatch.setattr("app.get_session", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "coach.intent_router.handle_flow_callback",
        lambda _db, choice: (
            choices.append(choice)
            or SimpleNamespace(
                text="Please confirm: Full Body 1 on Sunday at 18:00.",
                interactions=[SimpleNamespace()],
                reply_markup=None,
            )
        ),
    )
    monkeypatch.setattr("coach.interactions.reply_markup", lambda *_args: markup)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET},
        json={"callback_query": {
            "id": "callback-1", "data": "flow:abcd1234:d:20260719",
            "message": {"message_id": 7, "chat": {"id": 123, "type": "private"}},
        }},
    )

    assert response.status_code == 200
    assert choices == ["flow:abcd1234:d:20260719"]
    assert edited == [("Please confirm: Full Body 1 on Sunday at 18:00.", "123", 7, markup)]


def test_personal_data_is_ignored_in_group_chats(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("notify.telegram.send_message", lambda *args, **kwargs: sent.append(args) or True)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET},
        json={"message": {"chat": {"id": 123, "type": "group"}, "text": "Show my readiness"}},
    )

    assert response.status_code == 200
    assert sent == []
