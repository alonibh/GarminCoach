from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import json

import pytest

from garminconnect import GarminConnectAuthenticationError

try:
    from coach.active_recovery import ACTIVE_RECOVERY_WORKOUT_NAME, ActiveRecoveryTemplateResult
    _garminconnect_030_available = True
except ImportError:
    _garminconnect_030_available = False

pytestmark = pytest.mark.skipif(
    not _garminconnect_030_available,
    reason="requires garminconnect[typed]>=0.3 (garminconnect.workout submodule)",
)

if _garminconnect_030_available:
    from coach.active_recovery import ACTIVE_RECOVERY_WORKOUT_NAME, ActiveRecoveryTemplateResult
from coach.decision_engine import evaluate_selected_workout_recovery
from coach.interactions import (
    _compensate_remote_recovery,
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


class _BoundaryApi(_Api):
    """Fake Garmin calendar whose mutations prove their client boundary."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.depth = 0
        self.mutation_depths: list[int] = []
        self.fail_reads = 0
        self.fail_after_unschedule = False

    def get_scheduled_workouts(self, year, month):
        if self.fail_reads:
            self.fail_reads -= 1
            raise GarminConnectAuthenticationError("expired")
        if self.fail_after_unschedule and self.unscheduled:
            self.fail_after_unschedule = False
            raise GarminConnectAuthenticationError("expired")
        return super().get_scheduled_workouts(year, month)

    def schedule_workout(self, workout_id, target):
        assert self.depth == 1
        self.mutation_depths.append(self.depth)
        super().schedule_workout(workout_id, target)

    def unschedule_workout(self, occurrence):
        assert self.depth == 1
        self.mutation_depths.append(self.depth)
        super().unschedule_workout(occurrence)


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


def _use_clients(monkeypatch, *clients):
    entered = []
    @contextmanager
    def current():
        client = clients[min(len(entered), len(clients) - 1)]
        entered.append(client)
        api = client.api
        if isinstance(api, _BoundaryApi):
            api.depth += 1
        try:
            yield client
        finally:
            if isinstance(api, _BoundaryApi):
                api.depth -= 1
    monkeypatch.setattr("sync.garmin_registry.current_garmin_client", current)
    return entered


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


@pytest.mark.parametrize(
    ("originals", "walks", "choice"),
    [((900, 901), (), "active_recovery"), ((900, 901), (1001,), "active_recovery"),
     ((900,), (1001, 1002), "active_recovery"), ((900, 901), (), "rest")],
)
def test_ambiguous_occurrences_fail_before_any_remote_mutation(session, monkeypatch, originals, walks, choice):
    planned, row = _claim_walk(session, monkeypatch)
    payload = json.loads(row.payload_json)
    payload["selected_choice"] = choice
    row.payload_json = json.dumps(payload)
    api = _BoundaryApi(originals=originals, walks=walks)
    _use_clients(monkeypatch, _Client(api), _Client(api))
    monkeypatch.setattr(
        "coach.active_recovery.ensure_active_recovery_workout",
        lambda _session: ActiveRecoveryTemplateResult(ok=True, workout_id=77),
    )
    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert state == "failed" and "original workout remains unchanged" in text
    assert api.scheduled == [] and api.unscheduled == [] and api.mutation_depths == []
    assert planned.status == "planned"


def test_auth_after_walk_schedule_compensates_in_fresh_boundary(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    api = _BoundaryApi()
    primary, cleanup = _Client(api), _Client(api)
    entered = _use_clients(monkeypatch, primary, cleanup)
    monkeypatch.setattr("coach.active_recovery.ensure_active_recovery_workout", lambda _s: ActiveRecoveryTemplateResult(ok=True, workout_id=77))
    # The post-schedule walk read loses auth; the fresh cleanup boundary sees
    # and removes the newly introduced walk.
    original_get = api.get_scheduled_workouts
    raised = {"value": False}
    def after_schedule(year, month):
        if api.scheduled and not raised["value"]:
            raised["value"] = True
            api.fail_reads = 1
        return original_get(year, month)
    api.get_scheduled_workouts = after_schedule

    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert state == "failed" and "disconnected" in text and "restored" in text and "Reconnect Garmin" in text
    assert primary.expired and len(entered) == 2
    assert api.by_workout[55] == [900] and api.by_workout[77] == []
    assert api.mutation_depths and all(depth == 1 for depth in api.mutation_depths)
    assert planned.status == "planned"


def test_auth_after_original_unschedule_restores_original(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    api = _BoundaryApi()
    api.fail_after_unschedule = True
    primary, cleanup = _Client(api), _Client(api)
    entered = _use_clients(monkeypatch, primary, cleanup)
    monkeypatch.setattr("coach.active_recovery.ensure_active_recovery_workout", lambda _s: ActiveRecoveryTemplateResult(ok=True, workout_id=77))

    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert state == "failed" and "original remote state was restored" in text
    assert primary.expired and len(entered) == 2
    assert len(api.by_workout[55]) == 1 and api.by_workout[77] == []
    assert planned.status == "planned"


def test_auth_during_rest_compensation_is_truthful_and_marks_cleanup_expired(session, monkeypatch):
    planned, row = _claim_walk(session, monkeypatch)
    payload = json.loads(row.payload_json)
    payload["selected_choice"] = "rest"
    row.payload_json = json.dumps(payload)
    api = _BoundaryApi()
    api.fail_after_unschedule = True
    primary, cleanup = _Client(api), _Client(api)
    # First read after unschedule fails on primary; first cleanup read fails too.
    original_get = api.get_scheduled_workouts
    calls = {"after_unschedule": 0}
    def fail_twice(year, month):
        if api.unscheduled and calls["after_unschedule"] < 2:
            calls["after_unschedule"] += 1
            raise GarminConnectAuthenticationError("expired")
        return original_get(year, month)
    api.get_scheduled_workouts = fail_twice
    _use_clients(monkeypatch, primary, cleanup)

    state, text = apply_claimed_recovery_choice(session, row.interaction_id)
    assert state == "failed" and "disconnected" in text and "could not be fully verified" in text
    assert "Nothing was changed" not in text and "original workout remains unchanged" not in text
    assert primary.expired and cleanup.expired and planned.status == "planned"


def test_unobservable_walk_still_restores_original_before_incomplete_result():
    api = _BoundaryApi(originals=(), walks=())
    client = _Client(api)
    api.depth = 1
    try:
        verified = _compensate_remote_recovery(
            api, original_id=55, walk_id=77, target=DAY,
            before_originals=[900], before_walks=[], schedule_attempted=True,
            garmin_client=client,
        )
    finally:
        api.depth = 0
    assert verified is False
    assert len(api.by_workout[55]) == 1
    assert api.by_workout[77] == []
    assert api.mutation_depths == [1]
