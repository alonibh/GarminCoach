from datetime import datetime

import pytz

from app import _ensure_schedule_target_date
from db import CoachMessage


def _freeze_local_now(monkeypatch, value: datetime):
    import time_utils

    monkeypatch.setattr(
        time_utils,
        "get_local_now",
        lambda: pytz.timezone("Asia/Jerusalem").localize(value),
    )


def test_approval_target_date_uses_message_day_before_evening(monkeypatch):
    _freeze_local_now(monkeypatch, datetime(2026, 6, 30, 16, 0))
    msg = CoachMessage(
        role="assistant",
        content="",
        created_at=datetime(2026, 6, 30, 15, 15),
        pending_action_json=None,
    )
    payload = {"action": "schedule_workout", "base_workout_id": 1, "suggested_time": "09:30"}

    enriched = _ensure_schedule_target_date(payload, msg)

    assert enriched["target_date"] == "2026-06-30"
    assert "target_date" not in payload


def test_approval_target_date_uses_next_day_for_evening_message(monkeypatch):
    _freeze_local_now(monkeypatch, datetime(2026, 6, 30, 19, 30))
    msg = CoachMessage(
        role="assistant",
        content="",
        created_at=datetime(2026, 6, 30, 19, 0),
        pending_action_json=None,
    )
    payload = {"action": "schedule_workout", "base_workout_id": 1, "suggested_time": "18:00"}

    enriched = _ensure_schedule_target_date(payload, msg)

    assert enriched["target_date"] == "2026-07-01"


def test_approval_target_date_preserves_explicit_target_date():
    msg = CoachMessage(
        role="assistant",
        content="",
        created_at=datetime(2026, 6, 30, 19, 0),
        pending_action_json=None,
    )
    payload = {
        "action": "schedule_workout",
        "base_workout_id": 1,
        "target_date": "2026-07-04",
        "suggested_time": "18:00",
    }

    enriched = _ensure_schedule_target_date(payload, msg)

    assert enriched is payload
    assert enriched["target_date"] == "2026-07-04"


def test_approval_target_date_rolls_stale_message_forward(monkeypatch):
    _freeze_local_now(monkeypatch, datetime(2026, 7, 1, 10, 0))
    msg = CoachMessage(
        role="assistant",
        content="",
        created_at=datetime(2026, 6, 30, 15, 15),
        pending_action_json=None,
    )
    payload = {"action": "schedule_workout", "base_workout_id": 1, "suggested_time": "09:30"}

    enriched = _ensure_schedule_target_date(payload, msg)

    assert enriched["target_date"] == "2026-07-01"
