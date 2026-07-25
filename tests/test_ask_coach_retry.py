import asyncio

from coach.ask_coach_session import AcquireStatus, AskCoachSessionManager


def test_stale_retry_nonce_never_acquires_generation():
    async def scenario():
        manager = AskCoachSessionManager()
        await manager.create_session("user", "chat")
        first = await manager.try_acquire_in_flight("user")
        nonce = await manager.set_pending_retry(
            "user", first.generation_token, "original question"
        )
        await manager.clear_in_flight_if_matches("user", first.generation_token)
        stale_question = await manager.pending_retry(
            "user", "chat", "wrong-nonce"
        )
        valid_question = await manager.pending_retry("user", "chat", nonce)
        acquired = (
            await manager.try_acquire_in_flight("user")
            if valid_question
            else None
        )
        return stale_question, valid_question, acquired

    stale, valid, acquired = asyncio.run(scenario())
    assert stale is None
    assert valid == "original question"
    assert acquired.status == AcquireStatus.ACQUIRED


def test_retry_does_not_duplicate_question_in_history():
    async def scenario():
        manager = AskCoachSessionManager()
        await manager.create_session("user", "chat")
        first = await manager.try_acquire_in_flight("user")
        nonce = await manager.set_pending_retry(
            "user", first.generation_token, "question"
        )
        await manager.clear_in_flight_if_matches("user", first.generation_token)
        question = await manager.pending_retry("user", "chat", nonce)
        retry = await manager.try_acquire_in_flight("user")
        await manager.record_successful_turn(
            "user", retry.generation_token, question, "answer"
        )
        return await manager.get_session("user")

    view = asyncio.run(scenario())
    assert [item["content"] for item in view.history] == ["question", "answer"]
    assert view.pending_retry_question is None
