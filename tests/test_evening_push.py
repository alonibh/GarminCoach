from datetime import datetime

import pytz

from db import CoachMessage, Workout


def _set_evening(monkeypatch):
    import time_utils

    tz = pytz.timezone("Asia/Jerusalem")
    fixed_now = tz.localize(datetime(2026, 7, 3, 19, 0))
    monkeypatch.setattr(time_utils, "get_local_now", lambda: fixed_now)


def test_evening_no_push_is_not_saved_or_sent(session, monkeypatch):
    import coach.coach as coach_module

    _set_evening(monkeypatch)
    monkeypatch.setattr(coach_module, "build_snapshot", lambda session: "snapshot")
    monkeypatch.setattr(coach_module.llm, "generate", lambda *args, **kwargs: "NO_PUSH")

    sent = []
    import notify.telegram as telegram
    monkeypatch.setattr(telegram, "send_message", lambda text: sent.append(text))

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
    monkeypatch.setattr(telegram, "send_message", lambda text: sent.append(text))

    coach_module.generate_daily_suggestion(session)

    msg = session.query(CoachMessage).one()
    assert msg.role == "suggestion"
    assert msg.pending_action_json is not None
    assert "normal session Upper Strength" in msg.content
    assert len(sent) == 1
    assert "Evening Check-in" in sent[0]
