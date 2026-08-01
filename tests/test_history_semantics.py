import asyncio
from contextlib import contextmanager

from coach.ask_coach_llm import AskCoachResponse
from coach.ask_coach_session import session_manager
import coach.telegram_webhook as webhook
from tenant_context import TenantIdentity

USER_ID = "00000000-0000-0000-0000-000000000001"


def test_application_refusals_are_delivered_but_not_added_to_history(monkeypatch):
    @contextmanager
    def database(_user_id):
        yield object()

    async def refusal(**_kwargs):
        return AskCoachResponse(response_type="out_of_scope")

    async def can_deliver(*_args):
        return True

    async def send(*_args, **_kwargs):
        return True

    monkeypatch.setattr(webhook, "get_user_session", database)
    monkeypatch.setattr(webhook, "_valid_consent", lambda _user_id: True)
    monkeypatch.setattr(webhook, "build_advisory_snapshot", lambda _db: {})
    monkeypatch.setattr(webhook, "serialize_advisory_snapshot", lambda _data: "{}")
    monkeypatch.setattr(webhook, "generate_ask_coach_response", refusal)
    monkeypatch.setattr(webhook, "_can_deliver", can_deliver)
    monkeypatch.setattr(webhook, "_send_plain", send)
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
            question="Write code",
        )
        return await session_manager.get_session(USER_ID)

    assert asyncio.run(scenario()).history == ()
