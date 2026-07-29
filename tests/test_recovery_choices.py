from datetime import date, datetime
import json

from coach.decision_engine import evaluate_selected_workout_recovery
from coach.interactions import (
    apply_claimed_recovery_choice, claim_recovery_choice, reply_markup, stage_recovery_choice,
)
from db import PendingInteraction, PlannedSession
from metrics import freshness


DAY = date(2026, 7, 6)
NOW = datetime(2026, 7, 6, 8, 0)


def _actionable(session):
    planned = PlannedSession(title="Full Body", activity_type="strength_training", target_date=DAY,
        suggested_time="18:00", duration_min=60, intensity="normal", status="planned", source="coach")
    session.add(planned)
    session.flush()
    freshness.note_capability_observed(session, observed_at=NOW)
    freshness.record_signal(session, freshness.TRAINING_READINESS, DAY, freshness.FRESH, "test")
    from db import DailyHealth
    session.add(DailyHealth(day=DAY, training_readiness=1))
    return planned, evaluate_selected_workout_recovery(session, planned_session_id=planned.id, target=DAY, evaluated_at=NOW)


def test_one_recovery_choice_set_stages_and_claims_once(session, monkeypatch):
    monkeypatch.setattr("coach.interactions.get_local_date", lambda: DAY)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: NOW)
    planned, result = _actionable(session)
    row = stage_recovery_choice(session, result)
    assert row is stage_recovery_choice(session, result)
    payload = json.loads(row.payload_json)
    assert payload["planned_session"]["id"] == planned.id
    markup = reply_markup([row])
    buttons = markup["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Keep workout", "30-min walk", "Rest"]
    assert all(len(button["callback_data"].encode()) < 64 for button in buttons)
    claim = claim_recovery_choice(session, buttons[2]["callback_data"])
    assert claim and claim.claimed and claim.selected_choice == "rest"
    assert not claim_recovery_choice(session, buttons[0]["callback_data"]).claimed
    payload = json.loads(session.get(PendingInteraction, row.interaction_id).payload_json)
    assert payload["selected_choice"] == "rest" and payload["processing_started_at"]


def test_local_rest_keeps_program_session_pending(session, monkeypatch):
    monkeypatch.setattr("coach.interactions.get_local_date", lambda: DAY)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: NOW)
    planned, result = _actionable(session)
    row = stage_recovery_choice(session, result)
    callback = reply_markup([row])["inline_keyboard"][0][2]["callback_data"]
    assert claim_recovery_choice(session, callback).claimed
    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert (state, text, planned.status, row.status) == ("applied", "Rest selected for today.", "rest_selected", "applied"), row.failure_reason
    assert session.query(PlannedSession).count() == 1
