from datetime import datetime
import json

from coach.scheduling import _parse_clock, is_schedule_request, is_timing_question, next_available_time, requested_day
from db import Goal, ProgramSession, TrainingProgram


def test_sunday_evening_window_accepts_a_ninety_minute_session(session):
    avail = json.dumps({"6": {"off": False, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Total Package", active=True, status="active")
    session.add(program)
    session.flush()
    session.add(ProgramSession(
        program_id=program.id, name="Day 1", duration_min=90, session_role="coach_strength",
    ))
    session.commit()

    suggestion = next_available_time(
        session,
        now=datetime(2026, 7, 18, 19, 31),
        schedule=[
            {"title": "Event", "start": "2026-07-19 12:30", "end": "16:00"},
            {"title": "Overlap", "start": "2026-07-19 13:40", "end": "14:00"},
        ],
    )

    assert suggestion is not None
    assert suggestion.day.isoformat() == "2026-07-19"
    assert suggestion.start.strftime("%H:%M") == "18:00"
    assert suggestion.render() == "Day 1 — Sunday at 18:00."


def test_no_slot_when_a_full_session_does_not_fit(session):
    avail = json.dumps({"5": {"off": False, "start": "18:00", "end": "19:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Program", active=True, status="active")
    session.add(program)
    session.flush()
    session.add(ProgramSession(
        program_id=program.id, name="Day 1", duration_min=90, session_role="coach_strength",
    ))
    session.commit()

    suggestion = next_available_time(
        session, now=datetime(2026, 7, 18, 9, 0), schedule=[], max_days=1,
    )

    assert suggestion is None


def test_operational_chat_text_is_button_only_without_llm(session, monkeypatch):
    from coach.coach import handle_chat

    avail = json.dumps({"6": {"off": False, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Total Package", active=True, status="active")
    session.add(program)
    session.flush()
    session.add(ProgramSession(
        program_id=program.id, name="Day 1", duration_min=90, session_role="coach_strength",
    ))
    session.commit()

    monkeypatch.setattr("coach.llm.generate", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")))

    response, message = handle_chat(session, "Can I do it tomorrow?")

    assert response.startswith("Use the buttons below")
    assert message.pending_action_json is None


def test_timing_intent_variants_and_requested_days():
    today = datetime(2026, 7, 18).date()
    variants = (
        "When should I do it?",
        "Can I do it tomorrow?",
        "Could I train Sunday?",
        "What about Monday for the workout?",
        "Is there a time window tomorrow for my session?",
    )
    assert all(is_timing_question(text) for text in variants)
    assert requested_day("Can I do it tomorrow?", today).isoformat() == "2026-07-19"
    assert requested_day("Could I train Monday?", today).isoformat() == "2026-07-20"
    assert is_schedule_request("Can we schedule a workout for tomorrow?")
    assert is_schedule_request("Please book the session for Sunday")


def test_clock_parser_accepts_common_twelve_hour_variants():
    assert _parse_clock("6:30 pm").strftime("%H:%M") == "18:30"
    assert _parse_clock("6:30 p.m.").strftime("%H:%M") == "18:30"
    assert _parse_clock("6 am").strftime("%H:%M") == "06:00"


def test_timing_text_never_falls_back_to_legacy_llm(session, monkeypatch):
    from coach.coach import handle_chat

    avail = json.dumps({"6": {"off": False, "start": "18:00", "end": "19:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Program", active=True, status="active")
    session.add(program)
    session.flush()
    session.add(ProgramSession(
        program_id=program.id, name="Day 1", duration_min=90, session_role="coach_strength",
    ))
    session.commit()
    monkeypatch.setattr("coach.llm.generate", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")))

    response, _message = handle_chat(session, "Can I do it tomorrow?")

    assert response.startswith("Use the buttons below")


def test_rest_day_enforces_zero_available_start_times(session):
    avail = json.dumps({"5": {"off": True, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Program", active=True, status="active")
    session.add(program)
    session.flush()
    session.add(ProgramSession(
        program_id=program.id, name="Day 1", duration_min=45, session_role="coach_strength",
    ))
    session.commit()

    suggestion = next_available_time(
        session, now=datetime(2026, 7, 18, 9, 0), schedule=[], max_days=1,
    )

    assert suggestion is None
