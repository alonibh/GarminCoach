import asyncio
from contextlib import contextmanager

from coach.ask_coach_llm import AskCoachResponse
from coach.ask_coach_session import session_manager
import coach.telegram_webhook as webhook
from tenant_context import TenantIdentity, current_tenant

USER_ID = "00000000-0000-0000-0000-000000000001"


def test_database_closes_before_gemini_and_context_resets(monkeypatch):
    state = {"database_open": False}
    sent = []

    @contextmanager
    def database_session(_user_id):
        state["database_open"] = True
        try:
            yield object()
        finally:
            state["database_open"] = False

    async def fake_generate(**_kwargs):
        assert not state["database_open"]
        assert not session_manager.lock.locked()
        await asyncio.sleep(0)
        return AskCoachResponse(response_type="answer", answer="Safe answer")

    async def can_deliver(*_args):
        return True

    async def send_plain(text, **kwargs):
        sent.append((text, kwargs))
        return True

    monkeypatch.setattr(webhook, "get_user_session", database_session)
    monkeypatch.setattr(webhook, "build_advisory_snapshot", lambda _db: {})
    monkeypatch.setattr(webhook, "serialize_advisory_snapshot", lambda _data: "{}")
    monkeypatch.setattr(webhook, "generate_ask_coach_response", fake_generate)
    monkeypatch.setattr(webhook, "_can_deliver", can_deliver)
    monkeypatch.setattr(webhook, "_send_plain", send_plain)
    monkeypatch.setattr(
        "notify.telegram.send_chat_action", lambda *_args, **_kwargs: True
    )

    async def scenario():
        await session_manager.clear_all()
        await session_manager.create_session(USER_ID, "123")
        acquired = await session_manager.try_acquire_in_flight(USER_ID)
        await webhook.run_ask_coach_question(
            identity=TenantIdentity(USER_ID, timezone="UTC"),
            chat_id="123",
            generation_token=acquired.generation_token,
            question="Question",
        )
        return await session_manager.get_session(USER_ID)

    view = asyncio.run(scenario())
    assert current_tenant() is None
    assert not state["database_open"]
    assert view.in_flight_token is None
    assert [message["role"] for message in view.history] == ["user", "assistant"]
    assert sent[-1][1]["reply_markup"]["keyboard"] == [["Back to menu"]]


def test_cancellation_always_clears_matching_token(monkeypatch):
    @contextmanager
    def database_session(_user_id):
        yield object()

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(webhook, "get_user_session", database_session)
    monkeypatch.setattr(webhook, "build_advisory_snapshot", lambda _db: {})
    monkeypatch.setattr(webhook, "serialize_advisory_snapshot", lambda _data: "{}")
    monkeypatch.setattr(webhook, "generate_ask_coach_response", cancelled)
    monkeypatch.setattr(
        "notify.telegram.send_chat_action", lambda *_args, **_kwargs: True
    )

    async def scenario():
        await session_manager.clear_all()
        await session_manager.create_session(USER_ID, "123")
        acquired = await session_manager.try_acquire_in_flight(USER_ID)
        try:
            await webhook.run_ask_coach_question(
                identity=TenantIdentity(USER_ID, timezone="UTC"),
                chat_id="123",
                generation_token=acquired.generation_token,
                question="Question",
            )
        except asyncio.CancelledError:
            pass
        return await session_manager.get_session(USER_ID)

    view = asyncio.run(scenario())
    assert view.in_flight_token is None
    assert view.history == ()
    assert current_tenant() is None
