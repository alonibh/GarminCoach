import pytest
from pathlib import Path

import config
from app import validate_startup_configuration


def test_canonical_gemini_defaults():
    assert config.DEFAULT_GEMINI_MODEL == "gemini-3.5-flash-lite"
    assert config.LLM_PROVIDER == "gemini"
    assert config.GEMINI_MODEL == "gemini-3.5-flash-lite"
    assert config.ASK_COACH_MODEL == "gemini-3.5-flash-lite"
    assert config.DAILY_INSIGHT_MODEL == "gemini-3.5-flash-lite"


def test_canonical_thinking_levels():
    assert config.ASK_COACH_THINKING_LEVEL == "low"
    assert config.DAILY_INSIGHT_THINKING_LEVEL == "minimal"
    assert config.DAILY_INSIGHT_MAX_OUTPUT_TOKENS == 1024
    assert config.DAILY_INSIGHT_TIMEOUT_SECONDS == 25
    assert config.DAILY_INSIGHT_CONSENT_VERSION == "daily-insight-v1"


def test_startup_validation_accepts_canonical_config():
    validate_startup_configuration()


def test_startup_validation_rejects_non_canonical_default_model(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_GEMINI_MODEL", "gemini-2.5-flash")
    with pytest.raises(RuntimeError, match="DEFAULT_GEMINI_MODEL"):
        validate_startup_configuration()


def test_startup_validation_rejects_non_canonical_daily_insight_model(monkeypatch):
    monkeypatch.setattr(config, "DAILY_INSIGHT_MODEL", "   ")
    with pytest.raises(RuntimeError, match="DAILY_INSIGHT_MODEL"):
        validate_startup_configuration()


def test_startup_validation_rejects_invalid_daily_insight_thinking_level(monkeypatch):
    monkeypatch.setattr(config, "DAILY_INSIGHT_THINKING_LEVEL", "maximum")
    with pytest.raises(RuntimeError, match="DAILY_INSIGHT_THINKING_LEVEL"):
        validate_startup_configuration()


def test_startup_validation_rejects_invalid_daily_insight_tokens(monkeypatch):
    monkeypatch.setattr(config, "DAILY_INSIGHT_MAX_OUTPUT_TOKENS", 0)
    with pytest.raises(RuntimeError, match="DAILY_INSIGHT_MAX_OUTPUT_TOKENS"):
        validate_startup_configuration()


def test_startup_validation_rejects_invalid_daily_insight_timeout(monkeypatch):
    monkeypatch.setattr(config, "DAILY_INSIGHT_TIMEOUT_SECONDS", 0)
    with pytest.raises(RuntimeError, match="DAILY_INSIGHT_TIMEOUT_SECONDS"):
        validate_startup_configuration()


def test_env_example_contains_canonical_gemini_defaults():
    env_example_path = Path(__file__).resolve().parent.parent / ".env.example"
    content = env_example_path.read_text(encoding="utf-8")
    assert "LLM_PROVIDER=gemini" in content
    assert "GEMINI_MODEL=gemini-3.5-flash-lite" in content
    assert "ASK_COACH_MODEL=gemini-3.5-flash-lite" in content
    assert "ASK_COACH_THINKING_LEVEL=low" in content
    assert "DAILY_INSIGHT_MODEL=gemini-3.5-flash-lite" in content
    assert "DAILY_INSIGHT_THINKING_LEVEL=minimal" in content
    assert "DAILY_INSIGHT_MAX_OUTPUT_TOKENS=1024" in content
    assert "DAILY_INSIGHT_TIMEOUT_SECONDS=25" in content
    assert "gemini-2.5" not in content
