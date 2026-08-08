from datetime import date, datetime

import pytest

from coach.decision_engine import evaluate_morning_decision, training_readiness_category
from coach.renderer import render_morning
from coach.program_state import initialize_program_cursor
from db import DailyHealth, DecisionRecord, PlannedSession, ProgramCursor, Sleep
from metrics import freshness
from tests.test_program_state import _add_program


TARGET = date(2026, 7, 6)


def _fresh_sleep(session):
    session.add(Sleep(day=TARGET, total_s=7.5 * 3600, score=82))
    freshness.record_signal(session, freshness.SLEEP, TARGET, freshness.FRESH, "get_sleep_data")
    freshness.record_signal(session, freshness.SLEEP_SCORE, TARGET, freshness.FRESH, "get_sleep_data")


def _fresh_readiness(session, score):
    freshness.note_capability_observed(session, observed_at=datetime(2026, 7, 6, 7, 30))
    freshness.record_signal(
        session, freshness.TRAINING_READINESS, TARGET, freshness.FRESH, "get_training_readiness"
    )
    session.add(DailyHealth(day=TARGET, training_readiness=score))


@pytest.mark.parametrize(
    "score,category,decision_type",
    [
        (1, "Poor", "REST_RECOMMENDED"),
        (24, "Poor", "REST_RECOMMENDED"),
        (25, "Low", "KEEP_SELECTED_WORKOUT_WITH_WARNING"),
        (49, "Low", "KEEP_SELECTED_WORKOUT_WITH_WARNING"),
        (50, "Moderate", "KEEP_SELECTED_WORKOUT"),
        (74, "Moderate", "KEEP_SELECTED_WORKOUT"),
        (75, "High", "KEEP_SELECTED_WORKOUT"),
        (94, "High", "KEEP_SELECTED_WORKOUT"),
        (95, "Prime", "KEEP_SELECTED_WORKOUT"),
        (100, "Prime", "KEEP_SELECTED_WORKOUT"),
    ],
)
def test_official_garmin_readiness_boundaries(session, score, category, decision_type):
    _add_program(session)
    session.add(PlannedSession(title="Selected", activity_type="strength_training", target_date=TARGET, status="planned", source="coach"))
    _fresh_sleep(session)
    _fresh_readiness(session, score)
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )

    assert training_readiness_category(score) == category
    assert result.readiness_category == category
    assert result.decision_type == decision_type
    assert all("modifications" not in action for action in result.permitted_actions)


def test_supported_missing_readiness_waits_then_uses_no_fallback(session):
    _add_program(session)
    session.add(PlannedSession(title="Selected", activity_type="strength_training", target_date=TARGET, status="planned", source="coach"))
    _fresh_sleep(session)
    freshness.note_capability_observed(session, observed_at=datetime(2026, 7, 5, 8))
    freshness.record_signal(
        session, freshness.TRAINING_READINESS, TARGET, freshness.MISSING, "get_training_readiness"
    )
    session.commit()

    waiting = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 10)
    )
    deadline = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 11, 30)
    )
    anyway = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 11, 31), allow_incomplete=True
    )

    assert waiting.decision_type == deadline.decision_type == anyway.decision_type == "NO_BIOMETRIC_AUTHORITY"
    assert anyway.workout_outcome != "PROPOSE_NEXT_SESSION"
    assert anyway.best_effort is False
    assert anyway.readiness_score is None
    assert "TRAINING_READINESS_MISSING" in anyway.reason_codes


def test_program_rest_day_precedes_prime_readiness(session):
    program, source_sessions = _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, 100)
    cursor = initialize_program_cursor(session, program, activated_at=datetime(2026, 7, 1))
    cursor.last_completed_program_session_id = source_sessions[0].id
    cursor.last_completed_at = datetime(2026, 7, 5, 9)
    cursor.next_program_session_id = source_sessions[1].id
    session.add(PlannedSession(title="Selected", activity_type="strength_training", target_date=TARGET, status="planned", source="coach"))
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )

    assert result.decision_type == "PROGRAM_REST_RECOMMENDED"
    assert result.readiness_category is None
    assert result.permitted_actions[0]["choices"] == ["original", "active_recovery", "rest"]
    assert result.permitted_actions[0]["recommended_choice"] == "rest"


def test_plan_only_render_omits_sleep_and_optional_recovery_details(session):
    program, source_sessions = _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, 100)
    cursor = initialize_program_cursor(session, program, activated_at=datetime(2026, 7, 1))
    cursor.last_completed_program_session_id = source_sessions[0].id
    cursor.last_completed_at = datetime(2026, 7, 5, 9)
    cursor.next_program_session_id = source_sessions[1].id
    session.add(PlannedSession(title="Selected", activity_type="strength_training", target_date=TARGET, status="planned", source="coach"))
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )
    text, _markup, _ids = render_morning(session, result, plan_only=True)

    assert text == "Program rest is recommended; Selected remains selected and pending."
    assert "sleep" not in text.lower()
    assert "Optional" not in text


def test_poor_readiness_with_calendar_conflict_offers_keep_cancel_and_new_date(session, monkeypatch):
    _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, 20)
    session.add(PlannedSession(
        title="Workout A",
        activity_type="strength_training",
        target_date=TARGET,
        suggested_time="18:00",
        duration_min=60,
        status="planned",
        source="coach",
    ))
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=2: {
            "events": [{"title": "Appointment", "start": "2026-07-06 17:45", "end": "18:30"}],
            "state": "fresh",
            "error": None,
        },
    )

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )

    assert result.decision_type == "REST_RECOMMENDED"
    assert result.permitted_actions[0]["choices"] == ["original", "active_recovery", "rest"]
    assert result.permitted_actions[0]["recommended_choice"] == "rest"


def test_unsupported_device_has_no_metric_only_warning_or_skip(session):
    _fresh_sleep(session)
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )

    assert result.decision_type == "NO_SELECTED_WORKOUT"
    assert result.readiness_score is None
    assert result.applied_rules == []


def test_phase3b_recovery_facts_never_change_the_official_decision(session):
    _add_program(session)
    session.add(PlannedSession(title="Selected", activity_type="strength_training", target_date=TARGET, status="planned", source="coach"))
    _fresh_sleep(session)
    _fresh_readiness(session, 74)
    session.commit()
    baseline = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8))

    health = session.get(DailyHealth, TARGET)
    health.hrv_status = "UNBALANCED"
    health.recovery_time_minutes = 0
    health.body_battery_charged = 99
    health.body_battery_drained = 99
    session.flush()
    changed = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 9))

    assert (changed.decision_type, changed.workout_outcome, changed.readiness_score) == (
        baseline.decision_type, baseline.workout_outcome, baseline.readiness_score,
    )


def test_decision_record_is_idempotent_for_identical_facts(session):
    _add_program(session)
    session.add(PlannedSession(title="Selected", activity_type="strength_training", target_date=TARGET, status="planned", source="coach"))
    _fresh_sleep(session)
    _fresh_readiness(session, 74)
    session.commit()

    first = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8))
    second = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8))

    assert second.decision_id == first.decision_id
    assert session.query(DecisionRecord).count() == 1


def test_morning_render_keeps_overnight_facts_with_positive_planned_workouts(session, monkeypatch):
    _add_program(session)
    session.add(Sleep(
        day=TARGET,
        sleep_start_time=datetime(2026, 7, 5, 23, 37),
        sleep_end_time=datetime(2026, 7, 6, 6, 45),
        total_s=7.1 * 3600,
        score=86,
    ))
    session.add(PlannedSession(
        title="Day 1",
        activity_type="strength_training",
        target_date=TARGET,
        suggested_time="18:00",
        duration_min=60,
        status="approved",
        source="coach",
    ))
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    freshness.record_signal(session, freshness.SLEEP, TARGET, freshness.FRESH, "get_sleep_data")
    freshness.record_signal(session, freshness.SLEEP_SCORE, TARGET, freshness.FRESH, "get_sleep_data")
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=2: {"events": [], "state": "fresh", "error": None},
    )
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )
    text, _markup, _ids = render_morning(session, result)

    assert "Day 1 at 18:00 remains selected" in text
    assert "No workout action" not in text
