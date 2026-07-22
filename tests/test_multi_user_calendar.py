from datetime import date
from types import SimpleNamespace

import config
import coach.calendar as calendar_module


def test_all_day_events_are_returned_for_calendar_display(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)
    monkeypatch.setattr(config, "ICS_CALENDAR_URL", "https://calendar.google.com/example.ics")
    monkeypatch.setattr(
        calendar_module,
        "events",
        lambda **_kwargs: [SimpleNamespace(
            all_day=True,
            summary="Public holiday",
            start=date(2026, 7, 23),
            end=date(2026, 7, 24),
        )],
    )
    result = calendar_module.get_upcoming_schedule_result(days=7)
    assert result["state"] == "fresh"
    assert result["events"] == [{
        "title": "Public holiday",
        "start": "2026-07-23",
        "end": "",
        "all_day": True,
    }]
