from datetime import date, datetime, time
import json
from unittest.mock import MagicMock

from coach.interactions import (
    advance_button_flow,
    apply_claimed_interaction,
    begin_reschedule_flow,
    begin_schedule_flow,
    claim_garmin_interaction,
)
from coach.scheduling import fallback_start_times
from db import AthleteProfile, Goal, PlannedSession, ProgramSession, TrainingProgram


def test_fallback_start_times_returns_broad_window_for_off_day(session):
    avail = json.dumps({"0": {"off": True, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    session.commit()

    monday = date(2026, 7, 20)  # Monday
    starts = fallback_start_times(
        session, now=datetime(2026, 7, 19, 10, 0), target_day=monday, duration_min=60
    )
    assert len(starts) > 0
    assert time(7, 0) in starts or time(8, 0) in starts


def test_fallback_start_times_for_today_filters_past_times(session):
    avail = json.dumps({"0": {"off": False, "start": "10:00", "end": "22:00"}})
    session.add(Goal(id=1, custom_input=avail))
    session.commit()

    monday = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 14, 10)
    starts = fallback_start_times(
        session, now=now, target_day=monday, duration_min=60
    )
    assert all(datetime.combine(monday, t) >= datetime(2026, 7, 20, 14, 15) for t in starts)


def test_schedule_flow_offers_force_when_no_slots_available(session, monkeypatch):
    avail = json.dumps({"0": {"off": True, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Full Body", active=True, status="active")
    session.add(program)
    session.flush()
    prog_session = ProgramSession(
        program_id=program.id, name="Upper Body A", duration_min=60, session_role="coach_strength"
    )
    session.add(prog_session)
    session.commit()

    monkeypatch.setattr(
        "coach.interactions.get_local_now",
        lambda: datetime(2026, 7, 20, 9, 0),
    )
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda *args, **kwargs: {"events": [], "state": "fresh"},
    )

    turn = begin_schedule_flow(session)
    assert "Choose a date" in turn.text

    # Extract interaction id and nonce
    markup = turn.reply_markup
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]
    # flow:interaction_id:nonce:date:0

    # Pick Monday (2026-07-20), which is marked OFF
    turn_step2 = advance_button_flow(session, callback_data)
    assert "No open slots found on" in turn_step2.text
    assert "force schedule anyway" in turn_step2.text.lower()
    assert turn_step2.reply_markup is not None

    # Find the force button callback data
    force_btn = turn_step2.reply_markup["inline_keyboard"][0][0]
    assert "Force schedule anyway" in force_btn["text"]
    assert ":force:0" in force_btn["callback_data"]

    # Advance with Force
    turn_step3 = advance_button_flow(session, force_btn["callback_data"])
    assert "Choose a time (force schedule" in turn_step3.text
    assert len(turn_step3.reply_markup["inline_keyboard"]) > 0

    # Select the first offered time
    time_btn = turn_step3.reply_markup["inline_keyboard"][0][0]
    turn_step4 = advance_button_flow(session, time_btn["callback_data"])
    assert "Confirm (force): schedule Upper Body A" in turn_step4.text


def test_force_schedule_apply_bypasses_slot_check(session, monkeypatch):
    avail = json.dumps({"0": {"off": True, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    program = TrainingProgram(name="Full Body", active=True, status="active")
    session.add(program)
    session.flush()
    prog_session = ProgramSession(
        program_id=program.id, name="Upper Body A", duration_min=60, session_role="coach_strength"
    )
    session.add(prog_session)
    session.commit()

    monday = datetime(2026, 7, 20, 9, 0)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: monday)
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda *args, **kwargs: {"events": [], "state": "fresh"},
    )
    monkeypatch.setattr(
        "coach.garmin_compiler.compile_and_schedule_for_interaction",
        lambda sess, payload: MagicMock(ok=True, stage="schedule", user_message="ok"),
    )

    turn = begin_schedule_flow(session)
    cb_date = turn.reply_markup["inline_keyboard"][0][0]["callback_data"]
    turn_force_prompt = advance_button_flow(session, cb_date)
    cb_force = turn_force_prompt.reply_markup["inline_keyboard"][0][0]["callback_data"]
    turn_times = advance_button_flow(session, cb_force)
    cb_time = turn_times.reply_markup["inline_keyboard"][0][0]["callback_data"]
    turn_confirm = advance_button_flow(session, cb_time)

    # Extract interaction id
    confirm_btn = turn_confirm.reply_markup["inline_keyboard"][0][0]
    interaction_id = confirm_btn["callback_data"].replace("decision_action_", "")

    claim = claim_garmin_interaction(session, interaction_id)
    assert claim.claimed is True

    status, msg = apply_claimed_interaction(session, interaction_id)
    assert status == "applied"
    assert "Upper Body A scheduled for" in msg


def test_force_reschedule_flow(session, monkeypatch):
    avail = json.dumps({"1": {"off": False, "start": "18:00", "end": "20:00"}})
    session.add(Goal(id=1, custom_input=avail))
    planned = PlannedSession(
        title="Lower Body B",
        target_date=date(2026, 7, 20),
        suggested_time="18:00",
        duration_min=60,
        activity_type="strength_training",
        status="approved",
        created_at=datetime(2026, 7, 19, 10, 0),
        updated_at=datetime(2026, 7, 19, 10, 0),
    )
    session.add(planned)
    session.commit()

    monkeypatch.setattr(
        "coach.interactions.get_local_now",
        lambda: datetime(2026, 7, 20, 9, 0),
    )
    # Fully book Tuesday with a busy calendar event from 17:00 to 21:00
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda *args, **kwargs: {
            "events": [{"title": "Busy", "start": "2026-07-21 17:00", "end": "21:00"}],
            "state": "fresh",
        },
    )

    turn = begin_reschedule_flow(session, planned_session_id=planned.id)
    # Pick Tuesday (2026-07-21) -> index 1
    cb_tue = turn.reply_markup["inline_keyboard"][0][1]["callback_data"]
    turn_step2 = advance_button_flow(session, cb_tue)
    assert "No open slots found on" in turn_step2.text

    cb_force = turn_step2.reply_markup["inline_keyboard"][0][0]["callback_data"]
    turn_times = advance_button_flow(session, cb_force)
    assert "Choose a time (force schedule" in turn_times.text

    cb_time = turn_times.reply_markup["inline_keyboard"][0][0]["callback_data"]
    turn_confirm = advance_button_flow(session, cb_time)
    assert "Confirm (force): move Lower Body B" in turn_confirm.text

    confirm_btn = turn_confirm.reply_markup["inline_keyboard"][0][0]
    interaction_id = confirm_btn["callback_data"].replace("decision_action_", "")
    claim = claim_garmin_interaction(session, interaction_id)
    assert claim.claimed is True

    status, msg = apply_claimed_interaction(session, interaction_id)
    assert status == "applied"
    assert "Lower Body B moved to" in msg
    assert planned.target_date == date(2026, 7, 21)
