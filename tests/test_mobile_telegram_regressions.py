"""Regression coverage for the mobile and Telegram repairs."""
from datetime import datetime, timedelta
import json
from pathlib import Path

from coach.interactions import apply_interaction
from db import Goal, PendingInteraction, PlannedSession


ROOT = Path(__file__).resolve().parents[1]


def test_onboarding_availability_has_semantic_markup_without_inline_layout():
    markup = (ROOT / "templates" / "onboarding.html").read_text(encoding="utf-8")

    section = markup[markup.index('class="availability-rows-container"'):markup.index("function toggleDayRow")]
    assert "style=" not in section
    assert 'class="avail-rest-control"' in section
    assert 'class="avail-rest-label"' in section
    assert "times.classList.add('is-hidden')" in markup
    assert "times.classList.remove('is-hidden')" in markup


def test_mobile_availability_css_is_responsive_and_controls_hidden_by_class():
    css = (ROOT / "static" / "ui.css").read_text(encoding="utf-8")
    mobile = css[css.rfind("@media (max-width: 640px)"):]

    assert "display: grid !important" in mobile
    assert "grid-template-columns: auto minmax(0, 1fr) auto minmax(0, 1fr)" in mobile
    assert "width: 100% !important; min-width: 0 !important; max-width: 100% !important" in mobile
    assert ".avail-time-pickers.is-hidden { display: none !important; }" in css


def test_final_reschedule_revalidates_current_availability_before_mutation(session, monkeypatch):
    now = datetime(2026, 7, 6, 8, 5)  # Monday
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: now)
    monkeypatch.setattr("coach.interactions.program_version", lambda _session: "program-v1")
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: "calendar-v1")
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"events": [], "state": "fresh", "error": None},
    )
    # The appointment was selected earlier, but Monday has subsequently become a rest day.
    session.add(Goal(id=1, custom_input=json.dumps({"0": {"off": True}})))
    planned = PlannedSession(
        title="Tempo Run", activity_type="running", target_date=now.date(),
        suggested_time="18:00", duration_min=60, status="approved", source="coach",
    )
    session.add(planned)
    session.flush()
    row = PendingInteraction(
        interaction_id="reschedule-regression", decision_id=None,
        action_type="reschedule_planned_time", target_type="planned_session", target_id=planned.id,
        payload_json=json.dumps({"target_date": now.date().isoformat(), "suggested_time": "18:00"}),
        program_version="program-v1", sync_version="", calendar_version="calendar-v1",
        created_at=now, expires_at=now + timedelta(minutes=15), status="pending",
    )
    session.add(row)
    session.flush()

    status, text = apply_interaction(session, row.interaction_id)

    assert status == "stale"
    assert text == "That workout time is no longer available. Choose a new date and time."
    assert row.status == "superseded"
    assert row.failure_reason == "schedule_slot_changed"
    assert planned.suggested_time == "18:00"
    assert planned.target_date == now.date()
