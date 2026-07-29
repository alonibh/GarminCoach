from datetime import date, datetime

import pytest

from coach.decision_engine import evaluate_selected_workout_recovery
from db import DailyHealth, DecisionRecord, PlannedSession, ProgramCursor
from metrics import freshness
from tests.test_program_state import _add_program


TARGET = date(2026, 7, 6)


def _planned(session, **values):
    payload = {"title": "Full Body", "activity_type": "strength_training", "target_date": TARGET,
               "suggested_time": "18:00", "duration_min": 60, "status": "planned", "source": "coach"}
    payload.update(values)
    row = PlannedSession(**payload)
    session.add(row)
    session.flush()
    return row


def _readiness(session, score, state=freshness.FRESH):
    freshness.note_capability_observed(session, observed_at=datetime(2026, 7, 6, 7))
    freshness.record_signal(session, freshness.TRAINING_READINESS, TARGET, state, "get_training_readiness")
    session.add(DailyHealth(day=TARGET, training_readiness=score))


def _evaluate(session, planned_id=None):
    return evaluate_selected_workout_recovery(session, planned_session_id=planned_id, target=TARGET,
                                               evaluated_at=datetime(2026, 7, 6, 8))


def test_no_workout_has_no_recovery_or_schedule_action(session):
    result = _evaluate(session)
    assert result.decision_type == "NO_SELECTED_WORKOUT"
    assert result.permitted_actions == []
    assert result.workout_outcome != "PROPOSE_NEXT_SESSION"


def test_multiple_workouts_require_explicit_selection(session):
    first, second = _planned(session), _planned(session, title="Run", suggested_time="07:00")
    assert _evaluate(session).decision_type == "WORKOUT_SELECTION_REQUIRED"
    selected = _evaluate(session, second.id)
    assert selected.planned_session_id == second.id
    assert selected.decision_type == "NO_BIOMETRIC_AUTHORITY"
    assert first.status == second.status == "planned"


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_ineligible_status_is_not_selected(session, status):
    _planned(session, status=status)
    assert _evaluate(session).decision_type == "NO_SELECTED_WORKOUT"


@pytest.mark.parametrize("kwargs", [
    {"activity_type": "rest"}, {"intensity": "recovery"},
])
def test_rest_and_recovery_replacements_are_not_selected(session, kwargs):
    _planned(session, **kwargs)
    assert _evaluate(session).decision_type == "NO_SELECTED_WORKOUT"


@pytest.mark.parametrize("score,category,outcome", [
    (1, "Poor", "REST_RECOMMENDED"), (24, "Poor", "REST_RECOMMENDED"),
    (25, "Low", "KEEP_SELECTED_WORKOUT_WITH_WARNING"), (49, "Low", "KEEP_SELECTED_WORKOUT_WITH_WARNING"),
    (50, "Moderate", "KEEP_SELECTED_WORKOUT"), (74, "Moderate", "KEEP_SELECTED_WORKOUT"),
    (75, "High", "KEEP_SELECTED_WORKOUT"), (94, "High", "KEEP_SELECTED_WORKOUT"),
    (95, "Prime", "KEEP_SELECTED_WORKOUT"), (100, "Prime", "KEEP_SELECTED_WORKOUT"),
])
def test_readiness_boundaries_are_selected_workout_advice(session, score, category, outcome):
    row = _planned(session)
    _readiness(session, score)
    result = _evaluate(session, row.id)
    assert (result.readiness_category, result.decision_type, result.permitted_actions) == (category, outcome, [])
    assert row.status == "planned"


@pytest.mark.parametrize("score", [0, 101, None])
def test_invalid_readiness_has_no_authority(session, score):
    row = _planned(session)
    _readiness(session, score)
    assert _evaluate(session, row.id).decision_type == "NO_BIOMETRIC_AUTHORITY"


def test_boolean_readiness_is_rejected_before_storage():
    from sync.garmin_client import normalize_training_readiness
    assert normalize_training_readiness({"trainingReadiness": True}, TARGET) is None


@pytest.mark.parametrize("state", [freshness.STALE, freshness.EXPECTED_PENDING, freshness.ERROR])
def test_nonfresh_readiness_has_no_authority(session, state):
    row = _planned(session)
    _readiness(session, 70, state)
    assert _evaluate(session, row.id).decision_type == "NO_BIOMETRIC_AUTHORITY"


def test_missing_sleep_does_not_block_fresh_readiness(session):
    row = _planned(session)
    _readiness(session, 70)
    assert _evaluate(session, row.id).decision_type == "KEEP_SELECTED_WORKOUT"


def test_unsupported_and_unknown_are_distinct(session):
    row = _planned(session)
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    assert "UNSUPPORTED" in _evaluate(session, row.id).reason_codes[0]
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, None)
    assert "UNVERIFIED" in _evaluate(session, row.id).reason_codes[0]


def test_program_rest_precedes_prime_without_mutation(session):
    program, source = _add_program(session)
    row = _planned(session)
    _readiness(session, 100)
    cursor = ProgramCursor(program_id=program.id, next_program_session_id=source[1].id,
                           last_completed_program_session_id=source[0].id,
                           last_completed_at=datetime(2026, 7, 5, 9), policy_version="v1",
                           created_at=datetime(2026, 7, 1), updated_at=datetime(2026, 7, 5))
    session.add(cursor)
    result = _evaluate(session, row.id)
    assert result.decision_type == "PROGRAM_REST_RECOMMENDED"
    assert (row.status, cursor.next_program_session_id, result.permitted_actions) == ("planned", source[1].id, [])


def test_identity_is_per_selected_session_and_changes_with_score(session):
    first, second = _planned(session), _planned(session, title="Run", suggested_time="07:00")
    _readiness(session, 74)
    one = _evaluate(session, first.id)
    assert _evaluate(session, first.id).decision_id == one.decision_id
    two = _evaluate(session, second.id)
    session.get(DailyHealth, TARGET).training_readiness = 75
    three = _evaluate(session, first.id)
    assert len({one.decision_id, two.decision_id, three.decision_id}) == 3
    assert session.query(DecisionRecord).count() == 3
