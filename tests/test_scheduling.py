from datetime import datetime

from coach.scheduling import next_available_time
from db import Goal, ProgramSession, TrainingProgram


def test_sunday_evening_window_accepts_a_ninety_minute_session(session):
    session.add(Goal(
        id=1,
        custom_input="On Sundays-Thursdays no workouts before 18:00. No workouts after 20:00 ever.",
    ))
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
    assert suggestion.render() == "Day 1 — Sunday, July 19 at 18:00."


def test_no_slot_when_a_full_session_does_not_fit(session):
    session.add(Goal(id=1, custom_input="No workouts before 18:00. No workouts after 19:00."))
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


def test_chat_timing_question_uses_calculated_slot_without_llm(session, monkeypatch):
    from coach.coach import handle_chat

    session.add(Goal(
        id=1,
        custom_input="On Sundays-Thursdays no workouts before 18:00. No workouts after 20:00 ever.",
    ))
    program = TrainingProgram(name="Total Package", active=True, status="active")
    session.add(program)
    session.flush()
    session.add(ProgramSession(
        program_id=program.id, name="Day 1", duration_min=90, session_role="coach_strength",
    ))
    session.commit()

    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days: {"state": "fresh", "events": [
            {"title": "Event", "start": "2026-07-19 12:30", "end": "16:00"},
        ]},
    )
    monkeypatch.setattr("time_utils.get_local_now", lambda: datetime(2026, 7, 18, 19, 31))
    monkeypatch.setattr("coach.llm.generate", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")))

    response, _message = handle_chat(session, "When should I do it?")

    assert response == "Day 1 — Sunday, July 19 at 18:00."
