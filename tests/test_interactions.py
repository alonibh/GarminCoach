from datetime import date, datetime
import json

from coach.decision_engine import evaluate_morning_decision
from coach.interactions import (
    apply_interaction,
    stage_decision_actions,
    stage_free_text_change,
)
from coach.renderer import render_morning
from db import AthleteSafetyReport, PendingInteraction
from tests.test_decision_engine import TARGET, _fresh_readiness, _fresh_sleep
from tests.test_program_state import _add_program


def _decision(session, score=74):
    _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, score)
    session.commit()
    return evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )


def _fixed_now(monkeypatch):
    fixed = datetime(2026, 7, 6, 8, 5)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: fixed)


def test_renderer_stages_only_deterministic_original_session(session, monkeypatch):
    _fixed_now(monkeypatch)
    result = _decision(session, 74)

    text, markup, ids = render_morning(session, result)

    assert "Suggested today: Workout A." in text
    assert len(ids) == 1
    assert markup["inline_keyboard"][0][0]["text"] == "Schedule session"
    pending = session.get(PendingInteraction, ids[0])
    payload = json.loads(pending.payload_json)
    assert payload["program_session_id"] == result.next_program_session_id
    assert payload["modifications"] == []


def test_interaction_revalidates_and_schedules_once(session, monkeypatch):
    _fixed_now(monkeypatch)
    result = _decision(session, 74)
    pending = stage_decision_actions(session, result)[0]
    calls = []
    monkeypatch.setattr(
        "coach.garmin_compiler.compile_and_schedule",
        lambda _session, payload: calls.append(payload) or True,
    )

    first = apply_interaction(session, pending.interaction_id)
    second = apply_interaction(session, pending.interaction_id)

    assert first[0] == "applied"
    assert second[0] == "stale"
    assert len(calls) == 1
    assert calls[0]["modifications"] == []


def test_program_change_supersedes_old_button(session, monkeypatch):
    _fixed_now(monkeypatch)
    result = _decision(session, 74)
    pending = stage_decision_actions(session, result)[0]
    from db import TrainingProgram
    program = session.query(TrainingProgram).filter_by(active=True).one()
    program.updated_at = datetime(2026, 7, 6, 8, 6)
    session.commit()
    monkeypatch.setattr(
        "coach.garmin_compiler.compile_and_schedule",
        lambda *_args: (_ for _ in ()).throw(AssertionError("called")),
    )

    status, _text = apply_interaction(session, pending.interaction_id)

    assert status == "stale"
    assert pending.status == "superseded"


def test_free_text_can_initiate_but_not_apply_schedule_change(session, monkeypatch):
    _fixed_now(monkeypatch)
    _decision(session, 74)

    response, staged = stage_free_text_change(session, "Schedule the session today")

    assert response == "Confirm: Schedule session."
    assert len(staged) == 1
    assert staged[0].status == "pending"


def test_safety_free_text_requires_confirmation_before_structured_persistence(session, monkeypatch):
    _fixed_now(monkeypatch)
    response, staged = stage_free_text_change(session, "I felt dizzy during training")

    assert response.startswith("Confirm this report:")
    assert session.query(AthleteSafetyReport).count() == 0
    status, _ = apply_interaction(session, staged[0].interaction_id)
    assert status == "applied"
    assert session.query(AthleteSafetyReport).one().report_type == "dizziness"
