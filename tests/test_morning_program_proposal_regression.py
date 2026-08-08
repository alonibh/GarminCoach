from contextlib import contextmanager
from datetime import date, datetime
import pytz

import pytest
from fastapi.testclient import TestClient

from coach.decision_engine import evaluate_morning_decision, selected_workouts_for_date
from coach.interactions import prepare_recovery_morning, reply_markup_for_ids
from db import DailyHealth, MorningBriefState, NotificationOutbox, PlannedSession, ProgramCursor, Sleep
from metrics import freshness
import notify.morning as morning
from notify.morning import morning_deadline, priority_sync_finished, start_priority_fetch
import tenant_context
from tests.test_program_state import _add_program


TARGET = date(2026, 7, 6)


@pytest.fixture(autouse=True)
def bind_test_tenant():
    with tenant_context.tenant_scope(tenant_context.TenantIdentity("00000000-0000-0000-0000-000000000001")):
        yield


@contextmanager
def _bound_session(session):
    yield session
    session.commit()


def _fresh_data(session, readiness_score=75):
    freshness.note_capability_observed(session, observed_at=datetime(2026, 7, 6, 7))
    freshness.record_signal(session, freshness.SLEEP, TARGET, freshness.FRESH, "get_sleep_data")
    freshness.record_signal(session, freshness.SLEEP_SCORE, TARGET, freshness.FRESH, "get_sleep_data")
    freshness.record_signal(session, freshness.TRAINING_READINESS, TARGET, freshness.FRESH, "get_training_readiness")
    session.add(Sleep(day=TARGET, total_s=8 * 3600, score=85))
    session.add(DailyHealth(day=TARGET, training_readiness=readiness_score))
    session.commit()


def test_active_program_no_planned_session_proposes_next_session(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )
    program, source = _add_program(session)
    _fresh_data(session)
    now_val = datetime(2026, 7, 6, 7, 30)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: now_val)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: now_val)

    result = evaluate_morning_decision(session, target=TARGET)
    assert result.decision_type == "PROPOSE_NEXT_SESSION"
    assert result.next_program_session_id == source[0].id
    assert len(result.permitted_actions) == 1
    assert result.permitted_actions[0]["type"] == "schedule_original_session"

    text, interaction_ids = prepare_recovery_morning(session, result)
    assert "Suggested today:" in text
    assert len(interaction_ids) == 1
    markup = reply_markup_for_ids(session, interaction_ids)
    assert markup is not None
    assert markup["inline_keyboard"][0][0]["text"] == "Approve and schedule"


def test_morning_watch_starts_priority_fetch_and_waits_for_data(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr("notify.outbox.get_session", lambda: _bound_session(session))
    program, source = _add_program(session)
    tz = pytz.timezone("Asia/Jerusalem")
    now = tz.localize(datetime(2026, 7, 6, 7, 0))
    monkeypatch.setattr("time_utils.get_local_now", lambda: now)
    monkeypatch.setattr("time_utils.get_local_date", lambda: now.date())
    monkeypatch.setattr(morning, "get_local_now", lambda: now)
    monkeypatch.setattr(morning, "get_local_date", lambda: now.date())
    monkeypatch.setattr("sync.sync_runner.try_start_priority_sync", lambda: True)

    start_priority_fetch()
    state = session.get(MorningBriefState, TARGET)
    assert state.status == "fetching"
    assert session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count() == 0

    # Finished fetch when facts are not ready -> status becomes waiting
    priority_sync_finished()
    state = session.get(MorningBriefState, TARGET)
    assert state.status == "waiting"
    assert session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count() == 0

    # Once facts arrive, priority_sync_finished queues the briefing
    _fresh_data(session)
    priority_sync_finished()
    state = session.get(MorningBriefState, TARGET)
    assert state.status in {"queued", "complete"}
    assert session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count() == 1


def test_program_rest_day_does_not_propose_strength_session(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    program, source = _add_program(session)
    _fresh_data(session)
    cursor = ProgramCursor(
        program_id=program.id,
        next_program_session_id=source[1].id,
        last_completed_program_session_id=source[0].id,
        last_completed_at=datetime(2026, 7, 5, 9),
        policy_version="v1",
        created_at=datetime(2026, 7, 1),
        updated_at=datetime(2026, 7, 5),
    )
    session.add(cursor)
    session.commit()
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: datetime(2026, 7, 6, 7, 30))

    result = evaluate_morning_decision(session, target=TARGET)
    assert result.decision_type == "PROGRAM_REST_DAY"
    assert result.permitted_actions == []


def test_selected_workout_uses_training_readiness_recovery_flow(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    _fresh_data(session, readiness_score=20)
    planned = PlannedSession(
        title="Upper Body", activity_type="strength_training", target_date=TARGET,
        suggested_time="18:00", duration_min=60, status="planned", source="coach"
    )
    session.add(planned)
    session.commit()
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: datetime(2026, 7, 6, 7, 30))

    result = evaluate_morning_decision(session, target=TARGET)
    assert result.decision_type == "REST_RECOMMENDED"
    assert result.planned_session_id == planned.id


def test_training_readiness_not_used_to_force_rest_before_workout_selected(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    program, source = _add_program(session)
    _fresh_data(session, readiness_score=15)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: datetime(2026, 7, 6, 7, 30))

    result = evaluate_morning_decision(session, target=TARGET)
    assert result.decision_type == "PROPOSE_NEXT_SESSION"
    assert result.next_program_session_id == source[0].id


def test_morning_deadline_1130_fallback(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr("notify.outbox.get_session", lambda: _bound_session(session))
    program, source = _add_program(session)
    tz = pytz.timezone("Asia/Jerusalem")
    now = tz.localize(datetime(2026, 7, 6, 11, 30))
    monkeypatch.setattr("time_utils.get_local_now", lambda: now)
    monkeypatch.setattr("time_utils.get_local_date", lambda: now.date())
    monkeypatch.setattr("notify.morning.get_local_now", lambda: now)
    monkeypatch.setattr("notify.morning.get_local_date", lambda: now.date())

    sent = morning_deadline()
    assert sent is True
    state = session.get(MorningBriefState, TARGET)
    assert state.answer_anyway is True
    assert state.status in {"queued", "complete"}
    assert session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count() == 1


def test_idempotency_and_no_duplicate_briefs(session, monkeypatch):
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr("notify.outbox.get_session", lambda: _bound_session(session))
    program, source = _add_program(session)
    _fresh_data(session)
    tz = pytz.timezone("Asia/Jerusalem")
    now = tz.localize(datetime(2026, 7, 6, 7, 30))
    monkeypatch.setattr("time_utils.get_local_now", lambda: now)
    monkeypatch.setattr("time_utils.get_local_date", lambda: now.date())
    monkeypatch.setattr("notify.morning.get_local_now", lambda: now)
    monkeypatch.setattr("notify.morning.get_local_date", lambda: now.date())

    priority_sync_finished()
    count_first = session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count()
    assert count_first == 1

    priority_sync_finished()
    count_second = session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count()
    assert count_second == 1


def test_plan_page_renders_upcoming_planned_session(session, monkeypatch):
    import config
    import db
    import tenant_store
    from control_db import User
    import app as app_module

    monkeypatch.setattr(config, "APP_USERNAME", "", raising=False)
    monkeypatch.setattr("app.resolve_web_session", lambda sess, token: User(
        id="00000000-0000-0000-0000-000000000001", email="test@example.com",
        status="active", role="owner", onboarding_step="complete",
    ))
    monkeypatch.setattr(db, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(app_module, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(tenant_store, "engine_for_user", lambda uid, root=None: session.bind)

    program, source = _add_program(session)
    today_val = date.today()
    planned = PlannedSession(
        title="Legs & Core", activity_type="strength_training", target_date=today_val,
        suggested_time="18:00", duration_min=60, status="planned", source="coach"
    )
    session.add(planned)
    session.commit()

    monkeypatch.setattr("time_utils.get_local_date", lambda: today_val)
    client = TestClient(app_module.app, cookies={"gc_session": "test_token"})
    response = client.get("/program")
    assert response.status_code == 200
    assert "Legs &amp; Core" in response.text or "Legs & Core" in response.text
    assert "Next scheduled session" in response.text
    assert "Nothing scheduled yet" not in response.text


def test_no_training_readiness_authority_text_on_program_proposal(session, monkeypatch):
    from coach.renderer import render_morning
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )
    program, source = _add_program(session)
    _fresh_data(session)
    now_val = datetime(2026, 7, 6, 7, 30)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: now_val)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: now_val)

    result = evaluate_morning_decision(session, target=TARGET)
    text, _, _ = render_morning(session, result)

    assert "Sleep: 8h" in text
    assert "Garmin Sleep Score: 85 (Good)" in text
    assert "Garmin Training Readiness guides this decision" not in text


def test_exact_displayed_time_equals_staged_schedule_payload(session, monkeypatch):
    import json
    from coach.interactions import stage_decision_actions
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )
    program, source = _add_program(session)
    _fresh_data(session)
    now_val = datetime(2026, 7, 6, 7, 30)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: now_val)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: now_val)

    result = evaluate_morning_decision(session, target=TARGET)
    text, interaction_ids = prepare_recovery_morning(session, result)
    staged = stage_decision_actions(session, result)

    assert len(staged) == 1
    staged_payload = json.loads(staged[0].payload_json)
    assert f"at {staged_payload['suggested_time']}." in text
    assert result.planned_start_time == staged_payload["suggested_time"]


def test_no_valid_calendar_slot_shows_explicit_no_slot_state_without_button(session, monkeypatch):
    from coach.renderer import render_morning
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "stale", "events": [], "error": "Calendar unavailable"},
    )
    program, source = _add_program(session)
    _fresh_data(session)
    now_val = datetime(2026, 7, 6, 7, 30)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: now_val)

    result = evaluate_morning_decision(session, target=TARGET)
    assert result.decision_type == "PROPOSE_NEXT_SESSION"
    assert result.planned_start_time is None
    assert result.permitted_actions == []

    text, interaction_ids = prepare_recovery_morning(session, result)
    assert "No valid workout slot available today" in text
    assert "Suggested today:" not in text
    assert len(interaction_ids) == 0


def test_pinned_runtime_imports_fail_visibly_when_missing():
    import coach.active_recovery as ar
    import coach.ask_coach_llm as acl
    assert hasattr(ar, "GarminConnectNotFoundError")
    assert hasattr(acl, "genai")


def test_plan_page_uses_athlete_local_date_and_canonical_active_statuses(session, monkeypatch):
    import config
    import db
    import tenant_store
    from control_db import User
    import app as app_module

    monkeypatch.setattr(config, "APP_USERNAME", "", raising=False)
    monkeypatch.setattr("app.resolve_web_session", lambda sess, token: User(
        id="00000000-0000-0000-0000-000000000001", email="test@example.com",
        status="active", role="owner", onboarding_step="complete",
    ))
    monkeypatch.setattr(db, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(app_module, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(tenant_store, "engine_for_user", lambda uid, root=None: session.bind)

    program, source = _add_program(session)
    local_date = date(2026, 7, 6)

    done_sess = PlannedSession(
        title="Should Be Ignored Done", activity_type="strength_training", target_date=local_date,
        suggested_time="10:00", duration_min=60, status="completed", source="coach"
    )
    active_sess = PlannedSession(
        title="Active Future Workout", activity_type="strength_training", target_date=local_date,
        suggested_time="18:00", duration_min=60, status="planned", source="coach"
    )
    session.add(done_sess)
    session.add(active_sess)
    session.commit()

    monkeypatch.setattr("time_utils.get_local_date", lambda: local_date)
    client = TestClient(app_module.app, cookies={"gc_session": "test_token"})
    response = client.get("/program")
    assert response.status_code == 200
    assert "Active Future Workout" in response.text
    assert "Should Be Ignored Done" not in response.text


def test_staging_reuses_precalculated_time_without_second_calendar_lookup(session, monkeypatch):
    import json
    from coach.interactions import stage_decision_actions, prepare_recovery_morning, reply_markup_for_ids
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))

    payload_calendar_calls = 0

    def mock_calendar(days=7):
        nonlocal payload_calendar_calls
        if days == 7:
            payload_calendar_calls += 1
            if payload_calendar_calls > 1:
                raise RuntimeError("_schedule_payload must not fetch calendar a second time during staging!")
        return {"state": "fresh", "events": [], "error": None}

    monkeypatch.setattr("coach.calendar.get_upcoming_schedule_result", mock_calendar)
    program, source = _add_program(session)
    _fresh_data(session)
    now_val = datetime(2026, 7, 6, 7, 30)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: now_val)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: now_val)

    result = evaluate_morning_decision(session, target=TARGET)
    assert payload_calendar_calls == 1
    assert result.planned_start_time == "18:00"

    text, interaction_ids = prepare_recovery_morning(session, result)
    staged = stage_decision_actions(session, result)

    assert payload_calendar_calls == 1
    assert len(staged) == 1
    staged_payload = json.loads(staged[0].payload_json)
    assert staged_payload["suggested_time"] == "18:00"
    markup = reply_markup_for_ids(session, interaction_ids)
    assert markup["inline_keyboard"][0][0]["text"] == "Approve and schedule"


def test_morning_facts_fingerprint_updates_decision_record_idempotency(session, monkeypatch):
    from db import DecisionRecord
    monkeypatch.setattr(morning, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr("notify.outbox.get_session", lambda: _bound_session(session))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=7: {"state": "fresh", "events": [], "error": None},
    )
    program, source = _add_program(session)
    now_val = datetime(2026, 7, 6, 7, 0)
    monkeypatch.setattr("time_utils.get_local_date", lambda: TARGET)
    monkeypatch.setattr("time_utils.get_local_now", lambda: now_val)
    monkeypatch.setattr("coach.interactions.get_local_now", lambda: now_val)

    # 1. First evaluation: no informational facts exist in DB
    result1 = evaluate_morning_decision(session, target=TARGET)
    assert result1.decision_type == "PROPOSE_NEXT_SESSION"
    assert result1.observations == []
    rec1 = session.query(DecisionRecord).filter_by(idempotency_key=result1.idempotency_key).one()
    assert rec1.decision_id == result1.decision_id

    # 2. Fresh sleep/HRV facts arrive in DB while program/session/time stay identical
    _fresh_data(session)

    # 3. Second evaluation: must produce a new decision with fresh facts, NOT cached rec1
    result2 = evaluate_morning_decision(session, target=TARGET)
    assert result2.decision_type == "PROPOSE_NEXT_SESSION"
    assert result2.decision_id != result1.decision_id
    assert len(result2.observations) > 0
    assert result2.idempotency_key != result1.idempotency_key
    rec2 = session.query(DecisionRecord).filter_by(idempotency_key=result2.idempotency_key).one()
    assert rec2.decision_id == result2.decision_id

    # 4. Third evaluation with identical facts: must reuse result2 (same decision_id & idempotency_key)
    result3 = evaluate_morning_decision(session, target=TARGET)
    assert result3.decision_id == result2.decision_id
    assert result3.idempotency_key == result2.idempotency_key

    # 5. Outbox/brief dedup: queueing outbox message remains strictly one per day
    tz = pytz.timezone("Asia/Jerusalem")
    now_tz = tz.localize(datetime(2026, 7, 6, 7, 30))
    monkeypatch.setattr("notify.morning.get_local_now", lambda: now_tz)
    monkeypatch.setattr("notify.morning.get_local_date", lambda: now_tz.date())

    priority_sync_finished()
    count_first = session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count()
    assert count_first == 1

    priority_sync_finished()
    count_second = session.query(NotificationOutbox).filter_by(event_type="morning_briefing").count()
    assert count_second == 1

