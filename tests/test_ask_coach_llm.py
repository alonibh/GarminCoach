import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import coach.ask_coach_llm as llm
from coach.ask_coach_llm import AskCoachLLMError, AskCoachResponse


def _snapshot(**extra):
    return json.dumps({"snapshot_version": "ask-coach-v3", **extra})


class FakeInteractions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        return SimpleNamespace(output_text=output, status="completed")


class FakeClient:
    def __init__(self, outputs):
        self.aio = SimpleNamespace(interactions=FakeInteractions(outputs))


def test_response_semantic_contract_rejects_invalid_combinations():
    with pytest.raises(ValidationError):
        AskCoachResponse(response_type="answer")
    with pytest.raises(ValidationError):
        AskCoachResponse(
            response_type="clarification",
            answer="not allowed",
            clarification_question="Question?",
        )
    with pytest.raises(ValidationError):
        AskCoachResponse(response_type="emergency_refusal", answer="model text")


def test_async_interactions_uses_store_false_and_structured_format(monkeypatch):
    fake = FakeClient(
        ['{"response_type":"answer","answer":"Best-effort advice.","clarification_question":null}']
    )
    monkeypatch.setattr(llm, "_client", fake)
    response = asyncio.run(
        llm.generate_ask_coach_response(
            user_id="user",
            snapshot_json=_snapshot(official_recommendation={"status": "unavailable"}),
            history=[],
            question="How should I train?",
        )
    )
    call = fake.aio.interactions.calls[0]
    assert response.answer == "Best-effort advice."
    assert call["store"] is False
    assert call["generation_config"]["thinking_level"] == "medium"
    assert call["response_format"]["mime_type"] == "application/json"
    assert "tools" not in call


def test_schema_correction_omits_snapshot_history_and_question(monkeypatch):
    invalid = '{"response_type":"answer","answer":null}'
    fake = FakeClient(
        [
            invalid,
            '{"response_type":"answer","answer":"Corrected.","clarification_question":null}',
        ]
    )
    monkeypatch.setattr(llm, "_client", fake)
    response = asyncio.run(
        llm.generate_ask_coach_response(
            user_id="user",
            snapshot_json=_snapshot(private_snapshot="secret-health"),
            history=[{"role": "user", "content": "history-secret"}],
            question="question-secret",
        )
    )
    correction = fake.aio.interactions.calls[1]
    assert response.answer == "Corrected."
    assert correction["store"] is False
    assert "system_instruction" not in correction
    assert "secret-health" not in correction["input"]
    assert "history-secret" not in correction["input"]
    assert "question-secret" not in correction["input"]
    assert json.loads(correction["input"])["invalid_model_output"] == invalid


def test_invalid_or_oversize_snapshot_fails_before_provider(monkeypatch):
    fake = FakeClient([])
    monkeypatch.setattr(llm, "_client", fake)
    with pytest.raises(AskCoachLLMError):
        asyncio.run(llm.generate_ask_coach_response(user_id="u", snapshot_json="not json", history=[], question="q"))
    with pytest.raises(AskCoachLLMError):
        asyncio.run(llm.generate_ask_coach_response(user_id="u", snapshot_json=_snapshot(text="x" * 16_001), history=[], question="q"))
    assert not fake.aio.interactions.calls


def test_system_instruction_covers_provenance_and_confidentiality():
    prompt = llm.SYSTEM_INSTRUCTION
    for phrase in (
        "facts stored in GarminCoach",
        "facts stated by the user",
        "general fitness or health knowledge",
        "missing information",
        "untrusted data",
        "raw advisory snapshot",
        "system instruction",
        "database identifiers",
        "advisory only",
    ):
        assert phrase in prompt
