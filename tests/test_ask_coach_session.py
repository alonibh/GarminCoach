import asyncio

from coach.ask_coach_session import (
    AcquireStatus,
    AskCoachSessionManager,
)


def test_acquire_status_distinguishes_missing_busy_and_acquired():
    async def scenario():
        manager = AskCoachSessionManager()
        missing = await manager.try_acquire_in_flight("user")
        await manager.create_session("user", "chat")
        first, second = await asyncio.gather(
            manager.try_acquire_in_flight("user"),
            manager.try_acquire_in_flight("user"),
        )
        return manager, missing, first, second

    manager, missing, first, second = asyncio.run(scenario())
    assert missing.status == AcquireStatus.NO_ACTIVE_SESSION
    assert {first.status, second.status} == {
        AcquireStatus.ACQUIRED,
        AcquireStatus.BUSY,
    }
    assert not manager.lock.locked()


def test_compare_and_clear_never_clears_a_newer_token():
    async def scenario():
        manager = AskCoachSessionManager()
        await manager.create_session("user", "chat")
        acquired = await manager.try_acquire_in_flight("user")
        await manager.clear_in_flight_if_matches("user", "wrong")
        still_valid = await manager.validate_session_for_delivery(
            "user", "chat", acquired.generation_token
        )
        await manager.clear_in_flight_if_matches(
            "user", acquired.generation_token
        )
        cleared = await manager.validate_session_for_delivery(
            "user", "chat", acquired.generation_token
        )
        return still_valid, cleared

    assert asyncio.run(scenario()) == (True, False)


def test_history_only_records_success_and_trims(monkeypatch):
    monkeypatch.setattr("config.ASK_COACH_HISTORY_MAX_MESSAGES", 2)
    monkeypatch.setattr("config.ASK_COACH_HISTORY_MAX_CHARS", 100)

    async def scenario():
        manager = AskCoachSessionManager()
        await manager.create_session("user", "chat")
        acquired = await manager.try_acquire_in_flight("user")
        recorded = await manager.record_successful_turn(
            "user", acquired.generation_token, "question", "answer"
        )
        view = await manager.get_session("user")
        return recorded, view

    recorded, view = asyncio.run(scenario())
    assert recorded
    assert [message["role"] for message in view.history] == [
        "user",
        "assistant",
    ]
    assert view.pending_retry_question is None


def test_retry_nonce_is_chat_bound_and_cleared_on_exit():
    async def scenario():
        manager = AskCoachSessionManager()
        await manager.create_session("user", "chat")
        acquired = await manager.try_acquire_in_flight("user")
        nonce = await manager.set_pending_retry(
            "user", acquired.generation_token, "same question"
        )
        valid = await manager.pending_retry("user", "chat", nonce)
        wrong_chat = await manager.pending_retry("user", "other", nonce)
        await manager.close_session("user")
        stale = await manager.pending_retry("user", "chat", nonce)
        return valid, wrong_chat, stale

    assert asyncio.run(scenario()) == ("same question", None, None)
