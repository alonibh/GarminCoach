from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import json

from garminconnect import GarminConnectAuthenticationError

from coach.active_recovery import ACTIVE_RECOVERY_WORKOUT_NAME, ActiveRecoveryTemplateResult
from coach.decision_engine import evaluate_selected_workout_recovery
from coach.interactions import (
    apply_claimed_recovery_choice,
    claim_recovery_choice,
    reply_markup,
    stage_decision_actions,
    stage_recovery_choice,
)
from coach.renderers import render_next_workout
from db import DailyHealth, NotificationOutbox, PendingInteraction, PlannedSession, SyncState
from metrics import freshness


DAY = date(2026, 7, 6)
NOW = datetime(2026, 7, 6, 8, 0)


def _result(session, *, garmin_workout_id: int | None = None):
    planned = PlannedSession(
        title="Full Body", activity_type="strength_training", target_date=DAY,
        suggested_time="18:00", duration_min=60, intensity="normal", status="planned",
        source="coach", garmin_workout_id=garmin_workout_id,
    )
    session.add(planned)
    session.flush()
    freshness.note_capability_observed(session, observed_at=NOW)
    freshness.record_signal(session, freshness.TRAINING_READINESS, DAY, freshness.FRESH, "test")
    session.add(DailyHealth(day=DAY, training_readiness=1))
    return planned, evaluate_selected_workout_recovery(
        session, planned_session_id=planned.id, target=DAY, evaluated_at=NOW,
    )


class _Api:
    def __init__(self, *, originals=(900,), walks=()):
        self.by_workout = {55: list(originals), 77: list(walks)}
        self.scheduled: list[tuple[int, str]] = []
        self.unscheduled: list[int] = []
        self.next_id = 1000

    def get_scheduled_workouts(self, _year, _month):
        return [
            {"workoutId": workout_id, "scheduledWorkoutId": occurrence, "date": DAY.isoformat()}
            for workout_id, occurrences in self.by_workout.items()
            for occurrence in occurrences
        ]

    def schedule_workout(self, workout_id, target):
        self.scheduled.append((workout_id, target))
        self.next_id += 1
        self.by_workout.setdefault(workout_id, []).append(self.next_id)

    def unschedule_workout(self, occurrence):
        self.unscheduled.append(occurrence)
        for occurrences in self.by_workout.values():
            if occurrence in occurrences:
                occurrences.remove(occurrence)


class _Client:
    def __init__(self, api, error: Exception | None = None):
        self.api = api
        self.error = error
        self.expired = False

    def ensure_authenticated(self):
        if self.error:
            raise self.error

    def mark_session_expired(self):
        self.expired = True


def _use_client(monkeypatch, client):
    @contextmanager
    def current():
        yield client
    monkeypatch.setattr("sync.garmin_registry.current_garmin_client", current)


def _claim_walk(session, monkeypatch, *, garmin_workout_id=55):
    monkeypatch.setattr("coach.interactions.get_local_date", lambda: DAY)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: NOW)
    planned, result = _result(session, garmin_workout_id=garmin_workout_id)
    row = stage_recovery_choice(session, result)
    callback = reply_markup([row])["inline_keyboard"][0][1]["callback_data"]
    assert claim_recovery_choice(session, callback).claimed
    return planned, row


def test_active_recovery_replaces_one_event_and_enqueues_one_reminder(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    session.add(SyncState(key="coach_calendar_events", value=json.dumps([
        {"title": planned.title, "date": DAY.isoformat(), "start_time": "18:00", "duration_min": 60},
        {"title": "Unrelated", "date": DAY.isoformat(), "start_time": "12:00", "duration_min": 20},
    ])))
    api = _Api()
    _use_client(monkeypatch, _Client(api))
    monkeypatch.setattr(
        "coach.active_recovery.ensure_active_recovery_workout",
        lambda _session: ActiveRecoveryTemplateResult(ok=True, workout_id=77),
    )

    assert apply_claimed_recovery_choice(session, row.interaction_id)[0] == "applied"
    recovery = session.query(PlannedSession).filter_by(source="recovery_choice").one()
    assert (planned.status, recovery.title, recovery.suggested_time, recovery.duration_min) == (
        "replaced_by_active_recovery", ACTIVE_RECOVERY_WORKOUT_NAME, "18:00", 30,
    )
    events = json.loads(session.get(SyncState, "coach_calendar_events").value)
    assert {event["title"] for event in events} == {"Unrelated", ACTIVE_RECOVERY_WORKOUT_NAME}
    assert events.count({"title": ACTIVE_RECOVERY_WORKOUT_NAME, "date": DAY.isoformat(), "start_time": "18:00", "duration_min": 30}) == 1
    assert session.query(NotificationOutbox).filter_by(event_type="pre_workout_reminder").count() == 1
    assert api.by_workout[55] == [] and len(api.by_workout[77]) == 1


def test_processing_row_never_stages_competing_choice_or_buttons(session, monkeypatch):
    monkeypatch.setattr("coach.interactions.get_local_date", lambda: DAY)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: NOW)
    planned, result = _result(session)
    row = stage_recovery_choice(session, result)
    row.status = "processing"
    assert stage_recovery_choice(session, result) is row
    assert session.query(PendingInteraction).filter_by(action_type="choose_recovery_outcome").count() == 1
    assert stage_decision_actions(session, result, action_types={"start_sync"}) == []


def test_old_pending_is_superseded_but_old_processing_blocks_new_staging(session, monkeypatch):
    monkeypatch.setattr("coach.interactions.get_local_date", lambda: DAY)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: NOW)
    _planned, result = _result(session)
    old = stage_recovery_choice(session, result)
    payload = json.loads(old.payload_json)
    payload["decision_date"] = "2026-07-05"
    old.payload_json = json.dumps(payload)
    replacement = stage_recovery_choice(session, result)
    assert old.status == "superseded" and replacement is not old
    replacement.status = "processing"
    payload = json.loads(replacement.payload_json)
    payload["decision_date"] = "2026-07-05"
    replacement.payload_json = json.dumps(payload)
    assert stage_recovery_choice(session, result) is replacement


def test_remote_walk_verification_failure_compensates_new_walk(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    api = _Api()
    _use_client(monkeypatch, _Client(api))
    monkeypatch.setattr(
        "coach.active_recovery.ensure_active_recovery_workout",
        lambda _session: ActiveRecoveryTemplateResult(ok=True, workout_id=77),
    )
    calls = {"count": 0}
    real = api.get_scheduled_workouts
    def inconsistent(year, month):
        calls["count"] += 1
        # The first post-schedule read omits the walk; compensation's fresh read sees it.
        if calls["count"] == 3:
            return [{"workoutId": 55, "scheduledWorkoutId": 900, "date": DAY.isoformat()}]
        return real(year, month)
    api.get_scheduled_workouts = inconsistent

    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert state == "failed" and "original workout remains unchanged" in text
    assert api.by_workout[55] == [900] and api.by_workout[77] == []
    assert planned.status == "planned"


def test_auth_failure_marks_client_expired_and_requests_reconnect(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    client = _Client(_Api(), GarminConnectAuthenticationError("expired"))
    _use_client(monkeypatch, client)
    # Original uses the same authenticated read path but no mutation.
    payload = json.loads(row.payload_json)
    payload["selected_choice"] = "original"
    row.payload_json = json.dumps(payload)

    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert state == "failed" and "Reconnect Garmin" in text and client.expired
    assert planned.status == "planned"


def test_garmin_backed_rest_removes_only_original_and_adds_no_replacement(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    payload = json.loads(row.payload_json)
    payload["selected_choice"] = "rest"
    row.payload_json = json.dumps(payload)
    session.add(SyncState(key="coach_calendar_events", value=json.dumps([
        {"title": planned.title, "date": DAY.isoformat(), "start_time": "18:00", "duration_min": 60},
        {"title": "Unrelated", "date": DAY.isoformat(), "start_time": "12:00", "duration_min": 20},
    ])))
    api = _Api()
    _use_client(monkeypatch, _Client(api))

    assert apply_claimed_recovery_choice(session, row.interaction_id) == ("applied", "Rest selected for today.")
    assert planned.status == "rest_selected" and api.by_workout[55] == []
    assert session.query(PlannedSession).filter_by(source="recovery_choice").count() == 0
    assert session.query(NotificationOutbox).filter_by(event_type="pre_workout_reminder").count() == 0
    assert json.loads(session.get(SyncState, "coach_calendar_events").value) == [
        {"title": "Unrelated", "date": DAY.isoformat(), "start_time": "12:00", "duration_min": 20},
    ]


def test_next_workout_skips_terminal_source_and_shows_active_recovery(session, monkeypatch):
    monkeypatch.setattr("coach.renderers.get_local_now", lambda: NOW)
    session.add_all([
        PlannedSession(title="Source", target_date=DAY, suggested_time="18:00", status="replaced_by_active_recovery"),
        PlannedSession(title=ACTIVE_RECOVERY_WORKOUT_NAME, target_date=DAY, suggested_time="18:00", status="approved"),
    ])
    session.flush()
    assert ACTIVE_RECOVERY_WORKOUT_NAME in render_next_workout(session)
