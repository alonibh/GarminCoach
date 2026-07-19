from datetime import date, datetime
import json

import pytz

from db import CoachMessage, DailyHealth, DailyMetrics, Goal, Sleep, Workout


def _set_evening(monkeypatch):
    import time_utils

    tz = pytz.timezone("Asia/Jerusalem")
    fixed_now = tz.localize(datetime(2026, 7, 3, 19, 0))
    monkeypatch.setattr(time_utils, "get_local_now", lambda: fixed_now)
    monkeypatch.setattr(time_utils, "get_local_date", lambda: fixed_now.date())


def _set_morning(monkeypatch):
    import time_utils

    tz = pytz.timezone("Asia/Jerusalem")
    fixed_now = tz.localize(datetime(2026, 7, 4, 8, 0))
    monkeypatch.setattr(time_utils, "get_local_now", lambda: fixed_now)
    monkeypatch.setattr(time_utils, "get_local_date", lambda: fixed_now.date())


def test_evening_no_push_is_not_saved_or_sent(session, monkeypatch):
    import coach.coach as coach_module

    _set_evening(monkeypatch)
    monkeypatch.setattr(coach_module, "build_snapshot", lambda session: "snapshot")
    monkeypatch.setattr(coach_module.llm, "generate", lambda *args, **kwargs: "NO_PUSH")

    sent = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, **kwargs: sent.append((text, kwargs)) or True,
    )

    coach_module.generate_daily_suggestion(session)

    assert sent == []
    assert session.query(CoachMessage).count() == 0


def test_evening_never_creates_tomorrow_workout_before_new_sleep(session, monkeypatch):
    import coach.coach as coach_module

    _set_evening(monkeypatch)
    session.add(Workout(
        workout_id=1,
        name="Upper Strength",
        sport_type="strength_training",
        steps_json="[]",
    ))
    session.commit()

    monkeypatch.setattr(
        coach_module.llm, "generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM called")),
    )

    sent = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, **kwargs: sent.append((text, kwargs)) or True,
    )

    coach_module.generate_daily_suggestion(session)

    assert session.query(CoachMessage).count() == 0
    assert sent == []


def test_morning_workout_proposal_is_actionable_without_positive_readiness_rationale(session, monkeypatch):
    import coach.coach as coach_module
    import metrics.freshness as freshness

    _set_morning(monkeypatch)
    from tests.test_program_state import _add_program

    today = date(2026, 7, 4)
    session.add(Sleep(day=today, total_s=(6 * 3600) + (12 * 60), deep_s=1.4 * 3600, score=79))
    session.add(DailyHealth(day=today, training_readiness=75))
    session.add(Goal(id=1, custom_input="No workouts before 18:00. No workouts after 20:00."))
    _add_program(session)
    freshness.note_capability_observed(session, observed_at=datetime(2026, 7, 4, 7, 30))
    freshness.record_signal(session, freshness.SLEEP, today, freshness.FRESH, "get_sleep_data")
    freshness.record_signal(session, freshness.SLEEP_SCORE, today, freshness.FRESH, "get_sleep_data")
    freshness.record_signal(session, freshness.TRAINING_READINESS, today, freshness.FRESH, "get_training_readiness")
    session.commit()
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: datetime(2026, 7, 4, 8, 0))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )
    monkeypatch.setattr(
        coach_module.llm, "generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM called")),
    )

    sent = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, **kwargs: sent.append((text, kwargs)) or True,
    )

    coach_module.generate_daily_suggestion(session)

    msg = session.query(CoachMessage).one()
    assert json.loads(msg.pending_action_json)["interaction_ids"]
    assert msg.content == "Suggested today: Full Body 1 at 18:00."
    assert len(sent) == 1
    assert "Morning Briefing" in sent[0][0]
    assert sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve and schedule"
