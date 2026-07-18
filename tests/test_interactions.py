from datetime import date, datetime
import json

from coach.decision_engine import evaluate_morning_decision
from coach.interactions import (
    apply_interaction,
    stage_decision_actions,
    stage_free_text_change,
)
from coach.renderer import render_morning
from db import AthleteSafetyReport, Goal, PendingInteraction
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
    assert markup["inline_keyboard"][0][0]["text"] == "Approve and schedule"
    assert markup["inline_keyboard"][0][1]["text"] == "Different time"
    assert markup["inline_keyboard"][1][0]["text"] == "Dismiss"
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
    session.add(Goal(id=1, custom_input="No workouts before 18:00. No workouts after 20:00."))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"events": [], "state": "fresh", "error": None},
    )
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: "calendar-v1")

    response, staged = stage_free_text_change(session, "Schedule the session today")

    assert response == "Confirm: schedule Workout A on Monday at 18:00."
    assert len(staged) == 1
    assert staged[0].status == "pending"


def test_after_midnight_schedule_requests_use_current_day_and_ignore_chat_context(session, monkeypatch):
    from coach.coach import handle_chat

    program, sessions = _add_program(session, key="total_package_3")
    program.name = "Total Package · 3 days"
    for item in sessions:
        item.duration_min = 90
    session.add(Goal(
        id=1,
        custom_input="On Sundays-Thursdays no workouts before 18:00. No workouts after 20:00 ever.",
    ))
    session.commit()
    fixed = datetime(2026, 7, 19, 0, 8)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: fixed)
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"events": [
            {"title": "Sunday event", "start": "2026-07-19 12:30", "end": "16:00"},
            {"title": "Sunday event 2", "start": "2026-07-19 14:55", "end": "15:15"},
        ], "state": "fresh", "error": None},
    )
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: "calendar-v1")
    monkeypatch.setattr(
        "coach.llm.generate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )

    tomorrow_response, tomorrow_message = handle_chat(session, "can we schedule a workout for tomorrow")
    today_response, today_message = handle_chat(session, "can we schedule a workout for today")

    assert tomorrow_response == "Confirm: schedule Day 1 on Monday at 18:00."
    assert today_response == "Confirm: schedule Day 1 on Sunday at 18:00."
    tomorrow_id = json.loads(tomorrow_message.pending_action_json)["interaction_ids"][0]
    today_id = json.loads(today_message.pending_action_json)["interaction_ids"][0]
    tomorrow_payload = json.loads(session.get(PendingInteraction, tomorrow_id).payload_json)
    today_payload = json.loads(session.get(PendingInteraction, today_id).payload_json)
    assert tomorrow_payload["target_date"] == "2026-07-20"
    assert today_payload["target_date"] == "2026-07-19"
    assert tomorrow_payload["suggested_time"] == today_payload["suggested_time"] == "18:00"
    compiled = []
    monkeypatch.setattr(
        "coach.garmin_compiler.compile_and_schedule",
        lambda _session, payload: compiled.append(payload) or True,
    )

    status, confirmation = apply_interaction(session, tomorrow_id)

    assert status == "applied"
    assert confirmation == "Day 1 scheduled for Monday at 18:00."
    assert compiled[0]["target_date"] == "2026-07-20"


def test_safety_free_text_requires_confirmation_before_structured_persistence(session, monkeypatch):
    _fixed_now(monkeypatch)
    response, staged = stage_free_text_change(session, "I felt dizzy during training")

    assert response.startswith("Confirm this report:")
    assert session.query(AthleteSafetyReport).count() == 0
    status, _ = apply_interaction(session, staged[0].interaction_id)
    assert status == "applied"
    assert session.query(AthleteSafetyReport).one().report_type == "dizziness"
