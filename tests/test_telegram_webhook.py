import asyncio
import threading
import time

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


def test_operational_text_receives_reply_menu(monkeypatch):
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
    assert sent[0][2]["keyboard"]
    assert "Recovery metrics" in sent[0][2]["keyboard"][3]
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
    sent = []
    asyncio.run(session_manager.create_session(USER_ID, "123"))
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, reply_markup, kwargs)) or True
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
    assert "Ask Coach is active" in sent[0][0]
    assert sent[0][1]["keyboard"] == [["Back to menu"]]
    assert sent[0][2]["parse_mode"] is None


def test_back_to_menu_closes_session_and_discards_late_delivery(monkeypatch):
    asyncio.run(session_manager.create_session(USER_ID, "123"))
    acquired = asyncio.run(session_manager.try_acquire_in_flight(USER_ID))
    sent = []
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, reply_markup)) or True
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
    assert sent[0][1]["keyboard"]


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


def test_calendar_callback_returns_promptly_and_loads_off_event_loop(monkeypatch):
    from coach import telegram_webhook

    sent = []
    worker_threads = []
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **_kwargs: (
            sent.append(text) or True
        ),
    )

    def load(identity):
        worker_threads.append((identity.user_id, threading.get_ident()))
        time.sleep(0.1)
        return {"state": "unconfigured", "events": []}, []

    monkeypatch.setattr(telegram_webhook, "_load_calendar_for_user", load)
    payload = {
        "update_id": 998,
        "callback_query": {
            "id": "calendar",
            "data": "menu:calendar",
            "message": {
                "message_id": 8,
                "chat": {"id": 123, "type": "private"},
            },
        },
    }

    async def exercise():
        started = time.monotonic()
        result = await telegram_webhook.handle_telegram_update(payload)
        elapsed = time.monotonic() - started
        await asyncio.sleep(0.16)
        return result, elapsed

    result, elapsed = asyncio.run(exercise())
    assert result == {"status": "ok"}
    assert elapsed < 0.05
    assert sent[0] == "Loading calendar…"
    assert "No private calendar connected" in sent[-1]
    assert worker_threads == [(USER_ID, worker_threads[0][1])]
    assert worker_threads[0][1] != threading.get_ident()


def test_reply_keyboard_text_routes_and_appends_without_editing(monkeypatch):
    from coach import telegram_webhook

    sent = []
    monkeypatch.setattr(
        telegram_webhook,
        "_operational_callback",
        lambda action, **_kwargs: (
            f"handled {action}", telegram_webhook.main_menu_markup()
        ),
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, reply_markup, kwargs)) or True
        ),
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda *_args, **_kwargs: pytest.fail("main menu must not edit history"),
    )

    response = client.post(
        "/telegram/webhook",
        headers=_headers(),
        json={"update_id": 1001, "message": {
            "chat": {"id": 123, "type": "private"},
            "text": "Recovery metrics",
        }},
    )

    assert response.status_code == 200
    assert sent[0][0] == "handled menu:metrics"
    assert sent[0][1]["keyboard"]
    assert sent[0][2]["parse_mode"] is None


def test_start_sends_reply_keyboard(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, reply_markup, kwargs)) or True
        ),
    )
    response = client.post(
        "/telegram/webhook", headers=_headers(), json={
            "update_id": 1002,
            "message": {"chat": {"id": 123, "type": "private"}, "text": "/start"},
        }
    )
    assert response.status_code == 200
    assert sent[0][0] == "GarminCoach menu"
    assert sent[0][2] == {"parse_mode": None}
    assert sent[0][1]["keyboard"]


def test_back_to_menu_text_never_reaches_gemini(monkeypatch):
    from coach import telegram_webhook

    sent = []
    asyncio.run(session_manager.create_session(USER_ID, "123"))
    monkeypatch.setattr(
        telegram_webhook,
        "_register_task",
        lambda *_args: pytest.fail("Back to menu must not invoke Gemini"),
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, reply_markup)) or True
        ),
    )
    response = client.post(
        "/telegram/webhook", headers=_headers(), json={
            "update_id": 1003,
            "message": {"chat": {"id": 123, "type": "private"}, "text": "Back to menu"},
        }
    )
    assert response.status_code == 200
    assert not asyncio.run(session_manager.has_active_session(USER_ID))
    assert sent[0][0] == "GarminCoach menu"
    assert sent[0][1]["keyboard"]


def test_legacy_menu_callback_appends_without_editing(monkeypatch):
    from coach import telegram_webhook

    sent = []
    monkeypatch.setattr(
        telegram_webhook,
        "_operational_callback",
        lambda action, **_kwargs: (f"handled {action}", None),
    )
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append(text) or True
        ),
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda *_args, **_kwargs: pytest.fail("legacy menu must append"),
    )
    response = client.post(
        "/telegram/webhook", headers=_headers(), json={
            "update_id": 1004,
            "callback_query": {"id": "old", "data": "menu:metrics", "message": {
                "message_id": 1, "chat": {"id": 123, "type": "private"},
            }},
        }
    )
    assert response.status_code == 200
    assert sent == ["handled menu:metrics"]


def test_consent_acceptance_appends_ask_coach_reply_keyboard(monkeypatch):
    sent = []
    edits = []
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "coach.telegram_webhook.record_ask_coach_consent",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda text, chat_id, message_id, reply_markup=None, **kwargs: (
            edits.append((text, reply_markup, kwargs)) or True
        ),
    )
    monkeypatch.setattr(
        "notify.telegram.send_message",
        lambda text, chat_id=None, reply_markup=None, **kwargs: (
            sent.append((text, reply_markup, kwargs)) or True
        ),
    )
    response = client.post(
        "/telegram/webhook", headers=_headers(), json={
            "update_id": 1005,
            "callback_query": {"id": "consent", "data": "ask:consent_agree", "message": {
                "message_id": 1, "chat": {"id": 123, "type": "private"},
            }},
        }
    )
    assert response.status_code == 200
    assert edits[0][1] == {"inline_keyboard": []}
    assert sent[0][0].startswith("Ask Coach is active")
    assert sent[0][1]["keyboard"] == [["Back to menu"]]


def test_date_selection_edits_progress_before_blocking_calendar_work(
    monkeypatch
):
    from coach import telegram_webhook

    edits = []
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda text, *_args, **_kwargs: edits.append(text) or True,
    )

    def slow_operational(_identity, _callback_data, _chat_id):
        started.set()
        release.wait(timeout=2)
        return "Choose a time.", {"inline_keyboard": []}

    monkeypatch.setattr(
        telegram_webhook, "_run_operational_for_user", slow_operational
    )
    payload = {
        "update_id": 2001,
        "callback_query": {
            "id": "date",
            "data": "flow:interaction:nonce:date:0",
            "message": {
                "message_id": 77,
                "chat": {"id": 123, "type": "private"},
            },
        },
    }

    async def exercise():
        before = time.monotonic()
        response = await telegram_webhook.handle_telegram_update(payload)
        elapsed = time.monotonic() - before
        assert await asyncio.to_thread(started.wait, 1)
        assert edits == ["Checking available times…"]
        release.set()
        while telegram_webhook._active_tasks:
            await asyncio.sleep(0.01)
        return response, elapsed

    response, elapsed = asyncio.run(exercise())

    assert response == {"status": "ok"}
    assert elapsed < 0.1
    assert edits == ["Checking available times…", "Choose a time."]


def test_confirm_progress_is_prompt_and_duplicate_taps_mutate_once(
    monkeypatch
):
    from coach import telegram_webhook
    from coach.interactions import GarminInteractionClaim

    edits = []
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    apply_calls = []
    claimed = False
    monkeypatch.setattr(
        "notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "notify.telegram.edit_message_text",
        lambda text, *_args, **_kwargs: edits.append(text) or True,
    )

    def claim(_identity, _callback_data):
        nonlocal claimed
        accepted = not claimed
        claimed = True
        return GarminInteractionClaim(
            interaction_id="interaction",
            action_type="schedule_original_session",
            title="Full Body 2",
            claimed=accepted,
        )

    def slow_apply(identity, interaction_id):
        from tenant_context import current_tenant

        apply_calls.append(
            (identity.user_id, interaction_id, current_tenant())
        )
        started.set()
        release.wait()
        completed.set()
        return "applied", "Full Body 2 scheduled."

    monkeypatch.setattr(telegram_webhook, "_claim_garmin_callback", claim)
    monkeypatch.setattr(
        telegram_webhook, "_apply_claimed_for_user", slow_apply
    )

    def payload(update_id):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"confirm-{update_id}",
                "data": "decision_action_interaction",
                "message": {
                    "message_id": 78,
                    "chat": {"id": 123, "type": "private"},
                },
            },
        }

    async def exercise():
        first = await telegram_webhook.handle_telegram_update(payload(2002))
        assert first == {"status": "ok"}
        assert await asyncio.to_thread(started.wait, 1)
        assert not release.is_set()
        assert not completed.is_set()
        assert edits == ["Scheduling Full Body 2…"]
        second = await telegram_webhook.handle_telegram_update(payload(2003))
        assert not release.is_set()
        assert not completed.is_set()
        release.set()
        await asyncio.gather(*tuple(telegram_webhook._active_tasks))
        assert completed.is_set()
        return first, second

    first, second = asyncio.run(exercise())

    assert first == second == {"status": "ok"}
    assert len(apply_calls) == 1
    assert apply_calls[0][0:2] == (USER_ID, "interaction")
    assert apply_calls[0][2].user_id == USER_ID
    assert edits == [
        "Scheduling Full Body 2…",
        "Full Body 2 scheduled.",
    ]


def test_worker_thread_binds_and_resets_immutable_tenant(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from coach import telegram_webhook
    from tenant_context import TenantIdentity, current_tenant

    observed = []
    identity = TenantIdentity(USER_ID, role="owner", timezone="UTC")

    def operational(_callback_data, *, identity, chat_id):
        observed.append((current_tenant(), identity, chat_id))
        return "done", None

    monkeypatch.setattr(
        telegram_webhook, "_operational_callback", operational
    )

    def invoke():
        result = telegram_webhook._run_operational_for_user(
            identity, "flow:id:nonce:date:0", "123"
        )
        return result, current_tenant()

    with ThreadPoolExecutor(max_workers=1) as executor:
        result, after = executor.submit(invoke).result(timeout=2)

    assert result == ("done", None)
    assert observed == [(identity, identity, "123")]
    assert after is None
