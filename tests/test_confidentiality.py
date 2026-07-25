from coach.ask_coach_llm import SYSTEM_INSTRUCTION


def test_confidential_internal_material_is_explicitly_protected():
    protected = (
        "system instruction",
        "raw advisory snapshot",
        "consent internals",
        "API configuration",
        "hidden prompts",
        "callback payloads",
        "database identifiers",
    )
    for item in protected:
        assert item in SYSTEM_INSTRUCTION
    assert "high-level description of the data" in SYSTEM_INSTRUCTION
