from datetime import date, datetime
from contextlib import contextmanager
import json
from uuid import uuid4

from coach.decision_engine import evaluate_morning_decision
from coach.interactions import (
    advance_button_flow,
    apply_interaction,
    begin_reschedule_flow,
    begin_schedule_flow,
    calendar_version,
    program_version,
    stage_cancel_choices,
    stage_decision_actions,
)
from coach.renderer import render_morning
from db import Goal, PendingInteraction, PlannedSession
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


def _fresh_calendar(monkeypatch):
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"events": [], "state": "fresh", "error": None},
    )


def _constraints(session):
    session.add(
        Goal(
            id=1,
            custom_input="No workouts before 18:00. No workouts after 20:00.",
        )
    )


def test_renderer_stages_only_deterministic_original_session(session, monkeypatch):
    _fixed_now(monkeypatch)
    _fresh_calendar(monkeypatch)
    _constraints(session)
    result = _decision(session, 74)

    text, markup, ids = render_morning(session, result)

    assert "Suggested today: Full Body 1 at 18:00." in text
    assert len(ids) == 1
    assert markup["inline_keyboard"][0][0]["text"] == "Approve and schedule"
    pending = session.get(PendingInteraction, ids[0])
    payload = json.loads(pending.payload_json)
    assert payload["program_session_id"] == result.next_program_session_id
    assert payload["modifications"] == []


def test_interaction_revalidates_and_schedules_once(session, monkeypatch):
    _fixed_now(monkeypatch)
    _fresh_calendar(monkeypatch)
    _constraints(session)
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


def test_button_only_schedule_flow_uses_pending_payload(session, monkeypatch):
    _fixed_now(monkeypatch)
    _fresh_calendar(monkeypatch)
    _, sessions = _add_program(session)
    session.commit()
    monkeypatch.setattr(
        "coach.interactions.calendar_version", lambda _session: "calendar-v1"
    )

    turn = begin_schedule_flow(session)
    row = session.query(PendingInteraction).filter_by(action_type="button_flow").one()
    payload = json.loads(row.payload_json)

    assert payload["flow_type"] == "schedule"
    assert payload["flow_step"] == "choose_date"
    assert payload["program_session_id"] == sessions[0].id
    date_callback = turn.reply_markup["inline_keyboard"][0][0]["callback_data"]
    time_turn = advance_button_flow(session, date_callback)
    time_callback = time_turn.reply_markup["inline_keyboard"][0][0]["callback_data"]
    confirm = advance_button_flow(session, time_callback)

    assert row.action_type == "schedule_original_session"
    final_payload = json.loads(row.payload_json)
    assert final_payload["flow_step"] == "confirm"
    assert final_payload["suggested_time"] == "18:00"
    assert (
        confirm.reply_markup["inline_keyboard"][0][0]["text"]
        == "Approve and schedule"
    )


def test_button_only_reschedule_stores_all_flow_state(session, monkeypatch):
    _fixed_now(monkeypatch)
    _fresh_calendar(monkeypatch)
    monkeypatch.setattr(
        "coach.interactions.calendar_version", lambda _session: "calendar-v1"
    )
    planned = PlannedSession(
        title="Tempo Run",
        activity_type="running",
        target_date=date(2026, 7, 7),
        suggested_time="18:00",
        duration_min=40,
        status="approved",
        source="coach",
    )
    session.add(planned)
    session.flush()

    date_turn = begin_reschedule_flow(session, planned.id)
    row = session.query(PendingInteraction).filter_by(action_type="button_flow").one()
    date_callback = date_turn.reply_markup["inline_keyboard"][0][0]["callback_data"]
    time_turn = advance_button_flow(session, date_callback)
    time_callback = time_turn.reply_markup["inline_keyboard"][0][0]["callback_data"]
    advance_button_flow(session, time_callback)

    payload = json.loads(row.payload_json)
    assert row.action_type == "reschedule_planned_time"
    assert payload["flow_type"] == "reschedule"
    assert payload["flow_step"] == "confirm"
    assert payload["planned_session_id"] == planned.id
    assert payload["target_date"] == "2026-07-06"
    assert payload["suggested_time"] == "18:00"
    assert payload["page"] == 0


def test_stale_flow_nonce_does_not_advance(session, monkeypatch):
    _fixed_now(monkeypatch)
    _add_program(session)
    monkeypatch.setattr(
        "coach.interactions.calendar_version", lambda _session: "calendar-v1"
    )
    turn = begin_schedule_flow(session)
    row = session.query(PendingInteraction).filter_by(action_type="button_flow").one()
    before = row.payload_json
    callback = turn.reply_markup["inline_keyboard"][0][0]["callback_data"]
    parts = callback.split(":")
    parts[2] = "deadbeef"

    stale = advance_button_flow(session, ":".join(parts))

    assert "no longer current" in stale.text
    assert row.payload_json == before


class _OccurrenceApi:
    def __init__(self):
        self.scheduled = []
        self.unscheduled = []

    def get_scheduled_workouts(self, _year, _month):
        return [
            {
                "workoutId": 55,
                "scheduledWorkoutId": 900,
                "date": "2026-07-07",
            }
        ]

    def schedule_workout(self, workout_id, target_date):
        self.scheduled.append((workout_id, target_date))

    def unschedule_workout(self, occurrence_id):
        self.unscheduled.append(occurrence_id)


class _AuthenticatedMutationClient:
    def __init__(self):
        self.api = _OccurrenceApi()
        self.ensure_calls = 0
        self.login_calls = 0

    def ensure_authenticated(self):
        self.ensure_calls += 1

    def login(self):
        self.login_calls += 1
        raise AssertionError("an authenticated tenant client must be reused")


def _install_mutation_client(monkeypatch, fake):
    @contextmanager
    def current():
        yield fake

    monkeypatch.setattr(
        "sync.garmin_registry.current_garmin_client", current
    )


def test_cancel_reuses_authenticated_tenant_client(session, monkeypatch):
    _fixed_now(monkeypatch)
    planned = PlannedSession(
        title="Full Body 2",
        activity_type="strength_training",
        target_date=date(2026, 7, 7),
        suggested_time="18:00",
        duration_min=60,
        status="approved",
        garmin_workout_id=55,
        source="coach",
    )
    session.add(planned)
    session.commit()
    stage_cancel_choices(session)
    row = (
        session.query(PendingInteraction)
        .filter_by(action_type="cancel_planned_session")
        .one()
    )
    fake = _AuthenticatedMutationClient()
    _install_mutation_client(monkeypatch, fake)

    result = apply_interaction(session, row.interaction_id)

    assert result[0] == "applied"
    assert fake.ensure_calls == 1
    assert fake.login_calls == 0
    assert fake.api.unscheduled == [900]
    assert planned.status == "cancelled"


def test_reschedule_reuses_authenticated_tenant_client(session, monkeypatch):
    _fixed_now(monkeypatch)
    _fresh_calendar(monkeypatch)
    planned = PlannedSession(
        title="Full Body 2",
        activity_type="strength_training",
        target_date=date(2026, 7, 7),
        suggested_time="18:00",
        duration_min=60,
        status="approved",
        garmin_workout_id=55,
        source="coach",
    )
    session.add(planned)
    session.flush()
    row = PendingInteraction(
        interaction_id=str(uuid4()),
        action_type="reschedule_planned_time",
        target_type="planned_session",
        target_id=planned.id,
        payload_json=json.dumps(
            {"target_date": "2026-07-08", "suggested_time": "18:30"}
        ),
        program_version=program_version(session),
        sync_version="",
        calendar_version=calendar_version(session),
        created_at=datetime(2026, 7, 6, 8),
        expires_at=datetime(2026, 7, 6, 10),
        status="pending",
    )
    session.add(row)
    session.commit()
    monkeypatch.setattr(
        "coach.scheduling.available_start_times",
        lambda *_args, **_kwargs: [datetime.strptime("18:30", "%H:%M").time()],
    )
    fake = _AuthenticatedMutationClient()
    _install_mutation_client(monkeypatch, fake)

    result = apply_interaction(session, row.interaction_id)

    assert result[0] == "applied"
    assert fake.ensure_calls == 1
    assert fake.login_calls == 0
    assert fake.api.scheduled == [(55, "2026-07-08")]
    assert fake.api.unscheduled == [900]
    assert planned.target_date == date(2026, 7, 8)
