from datetime import datetime
import json

import pytest

from coach.intent_router import IntentClassification, classify_intent, route_chat
from coach.interactions import (
    _scheduled_occurrence_id,
    apply_interaction,
    mark_delivery_failed,
    reply_markup,
    request_different_time,
)
from db import ChatDialogueState, ChatIntentAudit, Goal, PendingInteraction, PlannedSession, SyncState
from tests.test_program_state import _add_program


def _guarded(monkeypatch, result):
    monkeypatch.setattr("config.CHAT_ROUTER_MODE", "guarded")
    monkeypatch.setattr("coach.intent_router.get_local_now", lambda: datetime(2026, 7, 19, 8, 0))
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: datetime(2026, 7, 19, 8, 0))
    monkeypatch.setattr("coach.intent_router.classify_intent", lambda *_args: result)


def test_classifier_rejects_hallucinated_evidence(monkeypatch):
    monkeypatch.setattr(
        "coach.intent_router.llm.generate_structured",
        lambda *_args: json.dumps({
            "intent": "schedule_workout", "date_text": "tomorrow",
            "time_text": None, "workout_text": None, "topic": None,
            "missing_slots": [],
        }),
    )
    with pytest.raises(ValueError, match="verbatim evidence"):
        classify_intent("Schedule my workout")


def test_guarded_semantic_schedule_stages_exact_proposal(session, monkeypatch):
    _add_program(session, key="total_package_3")
    session.add(Goal(id=1, custom_input="No workouts before 18:00. No workouts after 20:00."))
    session.commit()
    _guarded(monkeypatch, IntentClassification(
        intent="schedule_workout", date_text="today", time_text="19:00",
        workout_text="training", missing_slots=[],
    ))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )

    routed = route_chat(session, "Can you fit my training in today at 19:00?")

    assert routed is not None
    assert "19:00" in routed.text
    assert len(routed.interactions) == 1
    payload = json.loads(routed.interactions[0].payload_json)
    assert payload["target_date"] == "2026-07-19"
    assert payload["suggested_time"] == "19:00"
    markup = reply_markup(routed.interactions)
    assert [button["text"] for button in markup["inline_keyboard"][0]] == [
        "Approve and schedule", "Different time",
    ]


def test_guarded_missing_date_uses_typed_dialogue_state(session, monkeypatch):
    _guarded(monkeypatch, IntentClassification(
        intent="schedule_workout", workout_text="workout", missing_slots=["date"],
    ))

    routed = route_chat(session, "Please arrange my workout")

    assert routed.text == "Which day should I schedule it for?"
    state = session.get(ChatDialogueState, 1)
    assert state.intent == "schedule_workout"
    assert state.missing_slot == "date"
    assert json.loads(state.slots_json)["workout_text"] == "workout"


def test_different_time_followup_routes_even_during_shadow_rollout(session, monkeypatch):
    _add_program(session, key="total_package_3")
    session.add(Goal(id=1, custom_input="No workouts before 18:00. No workouts after 20:00."))
    session.commit()
    _guarded(monkeypatch, IntentClassification(intent="schedule_workout", date_text="today"))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )
    proposal = route_chat(session, "Schedule the workout today")
    assert request_different_time(session, proposal.interactions[0].interaction_id) == "What exact time would you prefer?"
    monkeypatch.setattr("config.CHAT_ROUTER_MODE", "shadow")
    monkeypatch.setattr(
        "coach.intent_router.classify_intent",
        lambda *_args: IntentClassification(intent="schedule_workout", time_text="19:00"),
    )

    replacement = route_chat(session, "19:00")

    assert replacement is not None
    assert "19:00" in replacement.text
    assert json.loads(replacement.interactions[0].payload_json)["suggested_time"] == "19:00"


def test_guarded_classifier_failure_fails_closed(session, monkeypatch):
    monkeypatch.setattr("config.CHAT_ROUTER_MODE", "guarded")
    monkeypatch.setattr(
        "coach.intent_router.classify_intent",
        lambda *_args: (_ for _ in ()).throw(ValueError("not json")),
    )

    routed = route_chat(session, "Schedule whatever you think is best")

    assert "couldn't safely identify" in routed.text
    assert session.query(PendingInteraction).count() == 0
    audit = session.query(ChatIntentAudit).one()
    assert audit.validation_status == "invalid"


def test_shadow_mode_audits_without_routing(session, monkeypatch):
    monkeypatch.setattr("config.CHAT_ROUTER_MODE", "shadow")
    monkeypatch.setattr(
        "coach.intent_router.classify_intent",
        lambda *_args: IntentClassification(intent="cancel_workout"),
    )

    assert route_chat(session, "Cancel it") is None
    assert session.query(PendingInteraction).count() == 0
    assert session.query(ChatIntentAudit).one().intent == "cancel_workout"


def test_guarded_read_only_classification_bypasses_legacy_keyword_actions(session, monkeypatch):
    from coach.coach import handle_chat

    _guarded(monkeypatch, IntentClassification(intent="general_question", topic="pain"))
    monkeypatch.setattr("coach.coach.llm.generate", lambda *_args, **_kwargs: "Grounded explanation.")

    response, message = handle_chat(session, "What does the word pain mean?")

    assert response == "Grounded explanation."
    assert message.pending_action_json is None
    assert session.query(PendingInteraction).count() == 0


def test_sync_request_is_confirmation_only(session, monkeypatch):
    _guarded(monkeypatch, IntentClassification(intent="request_sync"))

    routed = route_chat(session, "Please refresh my Garmin data")

    assert routed.text == "Confirm: start a Garmin sync now."
    assert routed.interactions[0].action_type == "start_sync"
    assert routed.interactions[0].status == "pending"


def test_delivery_failure_invalidates_pending_controls(session, monkeypatch):
    _guarded(monkeypatch, IntentClassification(intent="request_sync"))
    routed = route_chat(session, "Please refresh my Garmin data")

    mark_delivery_failed(session, [routed.interactions[0].interaction_id], "telegram_send_failed")

    assert routed.interactions[0].status == "failed"
    assert routed.interactions[0].failure_reason == "delivery_failed:telegram_send_failed"


def test_scheduled_occurrence_lookup_requires_matching_workout_and_date():
    raw = {"workouts": [
        {"scheduledWorkoutId": 12, "workoutId": 77, "date": "2026-07-18"},
        {"scheduledWorkoutId": 13, "workout": {"workoutId": 77}, "calendarDate": "2026-07-19"},
    ]}
    from datetime import date

    assert _scheduled_occurrence_id(raw, 77, date(2026, 7, 19)) == 13
    assert _scheduled_occurrence_id(raw, 99, date(2026, 7, 19)) is None


def test_cancel_unschedules_garmin_before_changing_local_state(session, monkeypatch):
    from datetime import date
    from sync.garmin_client import client

    planned = PlannedSession(
        title="Day 1", target_date=date(2026, 7, 19), suggested_time="18:00",
        duration_min=60, status="approved", garmin_workout_id=77,
    )
    session.add(planned)
    session.add(SyncState(
        key="coach_calendar_events",
        value=json.dumps([{"title": "Day 1", "date": "2026-07-19", "start_time": "18:00"}]),
    ))
    session.commit()
    _guarded(monkeypatch, IntentClassification(intent="cancel_workout"))
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: "calendar-v1")
    routed = route_chat(session, "Cancel my workout")

    class FakeApi:
        def __init__(self):
            self.unscheduled = []

        def get_scheduled_workouts(self, year, month):
            return {"workouts": [{
                "scheduledWorkoutId": 13, "workoutId": 77, "date": "2026-07-19",
            }]}

        def unschedule_workout(self, occurrence_id):
            self.unscheduled.append(occurrence_id)

    api = FakeApi()
    monkeypatch.setattr(client, "login", lambda: None)
    monkeypatch.setattr(client, "_api", api)

    status, text = apply_interaction(session, routed.interactions[0].interaction_id)

    assert status == "applied"
    assert text == "Day 1 was cancelled."
    assert api.unscheduled == [13]
    assert planned.status == "cancelled"
    assert json.loads(session.get(SyncState, "coach_calendar_events").value) == []


def test_cancel_keeps_local_state_when_garmin_cannot_verify_occurrence(session, monkeypatch):
    from datetime import date
    from sync.garmin_client import client

    planned = PlannedSession(
        title="Day 1", target_date=date(2026, 7, 19), suggested_time="18:00",
        duration_min=60, status="approved", garmin_workout_id=77,
    )
    session.add(planned)
    session.commit()
    _guarded(monkeypatch, IntentClassification(intent="cancel_workout"))
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: "calendar-v1")
    routed = route_chat(session, "Cancel my workout")

    class FakeApi:
        def get_scheduled_workouts(self, year, month):
            return {"workouts": []}

    monkeypatch.setattr(client, "login", lambda: None)
    monkeypatch.setattr(client, "_api", FakeApi())

    status, _text = apply_interaction(session, routed.interactions[0].interaction_id)

    assert status == "failed"
    assert planned.status == "approved"
