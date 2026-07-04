from datetime import date, datetime

import pytz

from db import CoachMessage, DailyHealth, DailyMetrics, Sleep, Workout


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
    import notify.telegram as telegram
    monkeypatch.setattr(telegram, "send_message", lambda text, **kwargs: sent.append((text, kwargs)))

    coach_module.generate_daily_suggestion(session)

    assert sent == []
    assert session.query(CoachMessage).count() == 0


def test_evening_workout_proposal_is_saved_and_sent(session, monkeypatch):
    import coach.coach as coach_module

    _set_evening(monkeypatch)
    session.add(Workout(
        workout_id=1,
        name="Upper Strength",
        sport_type="strength_training",
        steps_json="[]",
    ))
    session.commit()

    response = """
Tomorrow looks open after work and your recent load is controlled.

Tomorrow's recommendation: normal session Upper Strength at 18:30.

```json
{"action":"schedule_workout","base_workout_id":1,"suggested_time":"18:30","modifications":[]}
```
"""

    monkeypatch.setattr(coach_module, "build_snapshot", lambda session: "snapshot")
    monkeypatch.setattr(coach_module.llm, "generate", lambda *args, **kwargs: response)

    sent = []
    import notify.telegram as telegram
    monkeypatch.setattr(telegram, "send_message", lambda text, **kwargs: sent.append((text, kwargs)))

    coach_module.generate_daily_suggestion(session)

    msg = session.query(CoachMessage).one()
    assert msg.role == "suggestion"
    assert msg.pending_action_json is not None
    assert "normal session Upper Strength" in msg.content
    assert len(sent) == 1
    assert "Evening Check-in" in sent[0][0]
    assert sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve and schedule"
    assert sent[0][1]["reply_markup"]["inline_keyboard"][0][1]["text"] == "Different time"


def test_morning_workout_proposal_is_actionable_and_includes_fixed_short_sleep_once(session, monkeypatch):
    import coach.coach as coach_module
    import metrics.freshness as freshness

    _set_morning(monkeypatch)
    monkeypatch.setattr(freshness, "proactive_metrics_ready", lambda session: True)

    today = date(2026, 7, 4)
    session.add(Sleep(day=today, total_s=(6 * 3600) + (12 * 60), deep_s=1.4 * 3600, score=79))
    session.add(DailyHealth(day=today, hrv_overnight=55, hrv_baseline_low=45, hrv_baseline_high=65))
    session.add(DailyMetrics(day=today, readiness=75, acute_load=10, chronic_load=20, acwr=0.5, sleep_debt_h=3.7))
    session.add(Workout(
        workout_id=1,
        name="Legs & Shoulders",
        sport_type="strength_training",
        steps_json="[]",
    ))
    session.commit()

    response = """
Your readiness is good at 75 and training load is on the low side.

Recommendation: normal session Legs & Shoulders at 18:00. Today's calendar has Work 09:00-17:00 and Dinner 20:30-21:30.

```json
{"action":"schedule_session","title":"Legs & Shoulders","activity_type":"strength_training","base_workout_id":1,"target_date":"2026-07-04","suggested_time":"18:00","duration_min":60,"intensity":"normal","modifications":[]}
```
"""

    monkeypatch.setattr(coach_module, "build_snapshot", lambda session: "snapshot")
    monkeypatch.setattr(coach_module.llm, "generate", lambda *args, **kwargs: response)

    sent = []
    import notify.telegram as telegram
    monkeypatch.setattr(telegram, "send_message", lambda text, **kwargs: sent.append((text, kwargs)))

    coach_module.generate_daily_suggestion(session)

    msg = session.query(CoachMessage).one()
    assert msg.pending_action_json is not None
    assert msg.content.count("Short night - 6h12m, score 79.") == 1
    assert "Today's calendar has Work 09:00-17:00 and Dinner 20:30-21:30" in msg.content
    assert len(sent) == 1
    assert "Morning Briefing" in sent[0][0]
    assert sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve and schedule"
    assert sent[0][1]["reply_markup"]["inline_keyboard"][0][1]["text"] == "Different time"
