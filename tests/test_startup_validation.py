import pytest

import app
import config


def test_valid_startup_configuration_makes_no_gemini_request(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "configured")
    monkeypatch.setattr(
        "coach.ask_coach_llm.init_gemini_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not create client")),
    )
    app.validate_startup_configuration()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GEMINI_API_KEY", " "),
        ("ASK_COACH_MODEL", ""),
        ("ASK_COACH_THINKING_LEVEL", "extreme"),
        ("ASK_COACH_TIMEOUT_SECONDS", 0),
        ("ASK_COACH_HISTORY_MAX_MESSAGES", 1),
        ("ASK_COACH_SNAPSHOT_MAX_CHARS", 999),
        ("ASK_COACH_TRANSIENT_RETRIES", 2),
        ("APP_WORKER_COUNT", 2),
        ("ASK_COACH_DATA_CATEGORIES_VERSION", ""),
    ],
)
def test_invalid_startup_configuration_is_rejected(monkeypatch, name, value):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "configured")
    monkeypatch.setattr(config, name, value)
    with pytest.raises(RuntimeError):
        app.validate_startup_configuration()


def test_duplicate_or_empty_category_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "configured")
    monkeypatch.setattr(
        config,
        "CURRENT_ASK_COACH_DATA_CATEGORIES",
        ("recovery.metrics", " Recovery.Metrics "),
    )
    with pytest.raises(RuntimeError):
        app.validate_startup_configuration()
    monkeypatch.setattr(
        config, "CURRENT_ASK_COACH_DATA_CATEGORIES", ("recovery.metrics", "")
    )
    with pytest.raises(ValueError):
        app.validate_startup_configuration()
    monkeypatch.setattr(config, "CURRENT_ASK_COACH_DATA_CATEGORIES", ())
    with pytest.raises(RuntimeError):
        app.validate_startup_configuration()
