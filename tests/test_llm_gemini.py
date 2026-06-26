"""Tests for _generate_gemini (coach/llm.py) — request shape + error handling."""
import pytest

import config
from coach import llm


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, ok=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.ok = ok if ok is not None else (200 <= status_code < 300)

    def json(self):
        return self._json


def _capture_post(monkeypatch):
    """Patch requests.post; return a dict that captures the call args."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return captured["response"]

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return captured


def _text_response(text):
    return _FakeResp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_generation_config_and_header_auth(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "secret-key", raising=False)
    cap = _capture_post(monkeypatch)
    cap["response"] = _text_response("Train chest today.")

    out = llm._generate_gemini("system", "user msg", [])
    assert out == "Train chest today."

    # Key in header, not URL.
    assert "key=" not in cap["url"]
    assert cap["headers"]["x-goog-api-key"] == "secret-key"

    # generationConfig present with the expected knobs.
    gc = cap["json"]["generationConfig"]
    assert "temperature" in gc
    assert "maxOutputTokens" in gc
    assert gc["maxOutputTokens"] == config.GEMINI_MAX_OUTPUT_TOKENS


def test_history_roles_mapped(monkeypatch):
    cap = _capture_post(monkeypatch)
    cap["response"] = _text_response("ok")
    llm._generate_gemini("sys", "now", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    roles = [c["role"] for c in cap["json"]["contents"]]
    assert roles == ["user", "model", "user"]  # assistant -> model, current user appended


def test_max_tokens_finish_reason(monkeypatch):
    cap = _capture_post(monkeypatch)
    cap["response"] = _FakeResp(200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}]})
    out = llm._generate_gemini("s", "u", [])
    assert "cut off" in out.lower()


def test_safety_finish_reason(monkeypatch):
    cap = _capture_post(monkeypatch)
    cap["response"] = _FakeResp(200, {"candidates": [{"finishReason": "SAFETY", "content": {}}]})
    out = llm._generate_gemini("s", "u", [])
    assert "safety" in out.lower()


def test_prompt_blocked(monkeypatch):
    cap = _capture_post(monkeypatch)
    cap["response"] = _FakeResp(200, {"promptFeedback": {"blockReason": "SAFETY"}})
    out = llm._generate_gemini("s", "u", [])
    assert "blocked" in out.lower()


def test_rate_limited_message(monkeypatch):
    # Patch sleep so retries don't slow the test.
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    cap = _capture_post(monkeypatch)
    cap["response"] = _FakeResp(429, ok=False)
    out = llm._generate_gemini("s", "u", [])
    assert "rate-limited" in out.lower()


def test_bad_key_message(monkeypatch):
    cap = _capture_post(monkeypatch)
    cap["response"] = _FakeResp(403, ok=False)
    out = llm._generate_gemini("s", "u", [])
    assert "api key" in out.lower()
