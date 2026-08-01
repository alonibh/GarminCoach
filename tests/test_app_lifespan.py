import asyncio

import pytest

import app
import config


def _patch_lifecycle(monkeypatch, events, *, scheduler_failure=False):
    import coach.ask_coach_llm as llm
    import coach.ask_coach_session as sessions
    import coach.telegram_webhook as webhook
    import db_migration
    import process_lock

    monkeypatch.setattr(config, "GEMINI_API_KEY", "configured")
    monkeypatch.setattr(
        process_lock,
        "acquire_process_lock",
        lambda: events.append("acquire_lock") or object(),
    )
    monkeypatch.setattr(
        process_lock,
        "release_process_lock",
        lambda _lock: events.append("release_lock"),
    )
    monkeypatch.setattr(
        app,
        "validate_startup_configuration",
        lambda: events.append("validate"),
    )
    monkeypatch.setattr(
        app, "preflight_existing_databases", lambda: events.append("integrity_preflight")
    )
    monkeypatch.setattr(
        db_migration,
        "dispose_all_engines",
        lambda: events.append("dispose_engines"),
    )
    monkeypatch.setattr(
        db_migration,
        "run_destructive_migrations",
        lambda: events.append("migrate"),
    )
    monkeypatch.setattr(
        app, "initialize_databases", lambda: events.append("initialize")
    )
    monkeypatch.setattr(
        llm,
        "init_gemini_client",
        lambda: events.append("init_gemini") or object(),
    )

    async def close_gemini():
        events.append("close_gemini")

    monkeypatch.setattr(llm, "close_gemini_client", close_gemini)
    monkeypatch.setattr(
        sessions,
        "start_session_cleanup_task",
        lambda: events.append("start_cleanup") or object(),
    )

    async def cancel_cleanup():
        events.append("cancel_cleanup")

    async def clear_memory():
        events.append("clear_memory")

    monkeypatch.setattr(
        sessions, "cancel_session_cleanup_task", cancel_cleanup
    )
    monkeypatch.setattr(sessions, "clear_in_memory_state", clear_memory)

    async def cancel_active():
        events.append("cancel_active")

    monkeypatch.setattr(webhook, "cancel_active_gemini_tasks", cancel_active)
    monkeypatch.setattr(
        webhook, "set_shutting_down_flag", lambda: events.append("shutting_down")
    )
    monkeypatch.setattr(webhook, "clear_shutting_down_flag", lambda: None)

    def start_scheduler():
        events.append("start_scheduler")
        if scheduler_failure:
            raise RuntimeError("scheduler failed")

    monkeypatch.setattr(app, "start_multi_user_scheduler", start_scheduler)
    monkeypatch.setattr(
        app, "stop_schedulers", lambda: events.append("stop_scheduler")
    )


def test_lifespan_startup_and_shutdown_order(monkeypatch):
    events = []
    _patch_lifecycle(monkeypatch, events)

    async def scenario():
        async with app.lifespan(app.app):
            events.append("serving")

    asyncio.run(scenario())
    assert events == [
        "acquire_lock",
        "validate",
        "integrity_preflight",
        "dispose_engines",
        "migrate",
        "initialize",
        "init_gemini",
        "start_cleanup",
        "start_scheduler",
        "serving",
        "shutting_down",
        "cancel_cleanup",
        "cancel_active",
        "clear_memory",
        "close_gemini",
        "stop_scheduler",
        "dispose_engines",
        "release_lock",
    ]


def test_partial_startup_failure_cleans_initialized_resources(monkeypatch):
    events = []
    _patch_lifecycle(monkeypatch, events, scheduler_failure=True)

    async def scenario():
        async with app.lifespan(app.app):
            raise AssertionError("must not yield")

    with pytest.raises(RuntimeError, match="scheduler failed"):
        asyncio.run(scenario())
    assert "cancel_cleanup" in events
    assert "cancel_active" in events
    assert "clear_memory" in events
    assert "close_gemini" in events
    assert "stop_scheduler" not in events
    assert events[-1] == "release_lock"


def test_cleanup_failure_does_not_block_later_steps(monkeypatch):
    events = []
    _patch_lifecycle(monkeypatch, events)
    import coach.ask_coach_session as sessions

    async def failing_cleanup():
        events.append("cancel_cleanup")
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(
        sessions, "cancel_session_cleanup_task", failing_cleanup
    )

    async def scenario():
        async with app.lifespan(app.app):
            pass

    asyncio.run(scenario())
    assert "cancel_active" in events
    assert "close_gemini" in events
    assert events[-1] == "release_lock"
