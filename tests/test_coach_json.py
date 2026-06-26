"""Tests for _extract_and_strip_json (coach/coach.py)."""
from coach.coach import _extract_and_strip_json


def test_no_json_block_returns_text_unchanged():
    text = "Great work today! Keep it up."
    stripped, json_str = _extract_and_strip_json(text)
    assert stripped == text
    assert json_str is None


def test_valid_json_block_is_extracted_and_stripped():
    text = (
        "Here's your workout for today.\n\n"
        '```json\n{"action": "schedule_workout", "base_workout_id": 1}\n```'
    )
    stripped, json_str = _extract_and_strip_json(text)
    assert "```json" not in stripped
    assert "Here's your workout for today." in stripped
    assert json_str is not None
    assert '"schedule_workout"' in json_str


def test_malformed_json_block_keeps_raw_text():
    # Invalid JSON (trailing comma) must NOT crash; falls back to (text, None).
    text = 'Do this.\n```json\n{"action": "schedule_workout",}\n```'
    stripped, json_str = _extract_and_strip_json(text)
    assert json_str is None
    # On failure we return the original text untouched.
    assert stripped == text


def test_only_first_block_extracted():
    text = (
        '```json\n{"a": 1}\n```\n'
        "middle\n"
        '```json\n{"b": 2}\n```'
    )
    stripped, json_str = _extract_and_strip_json(text)
    assert '"a": 1' in json_str
    assert '"b": 2' not in json_str
