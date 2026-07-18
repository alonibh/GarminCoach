from datetime import date, datetime

import pytest

from coach.decision_engine import evaluate_morning_decision, training_readiness_category
from coach.program_state import initialize_program_cursor
from db import DailyHealth, DecisionRecord, ProgramCursor, Sleep
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
        (1, "Poor", "ADVISE_SKIP_SESSION"),
        (24, "Poor", "ADVISE_SKIP_SESSION"),
        (25, "Low", "WARN_ORIGINAL_SESSION"),
        (49, "Low", "WARN_ORIGINAL_SESSION"),
        (50, "Moderate", "PROPOSE_NEXT_SESSION"),
        (74, "Moderate", "PROPOSE_NEXT_SESSION"),
        (75, "High", "PROPOSE_NEXT_SESSION"),
        (94, "High", "PROPOSE_NEXT_SESSION"),
        (95, "Prime", "PROPOSE_NEXT_SESSION"),
        (100, "Prime", "PROPOSE_NEXT_SESSION"),
    ],
)
def test_official_garmin_readiness_boundaries(session, score, category, decision_type):
    _add_program(session)
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

    assert waiting.decision_type == "WAITING_FOR_DATA"
    assert deadline.decision_type == "SYNC_REQUIRED"
    assert anyway.workout_outcome == "PROPOSE_NEXT_SESSION"
    assert anyway.best_effort is True
    assert anyway.readiness_score is None
    assert "GARMIN_READINESS_UNAVAILABLE_NO_SUBSTITUTE" in anyway.reason_codes


def test_program_rest_day_precedes_prime_readiness(session):
    program, source_sessions = _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, 100)
    cursor = initialize_program_cursor(session, program, activated_at=datetime(2026, 7, 1))
    cursor.last_completed_program_session_id = source_sessions[0].id
    cursor.last_completed_at = datetime(2026, 7, 5, 9)
    cursor.next_program_session_id = source_sessions[1].id
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )

    assert result.decision_type == "PROGRAM_REST_DAY"
    assert result.readiness_category == "Prime"
    assert result.permitted_actions == []


def test_unsupported_device_has_no_metric_only_warning_or_skip(session):
    _add_program(session)
    _fresh_sleep(session)
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    session.commit()

    result = evaluate_morning_decision(
        session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8)
    )

    assert result.decision_type == "PROPOSE_NEXT_SESSION"
    assert result.readiness_score is None
    assert result.applied_rules == []


def test_decision_record_is_idempotent_for_identical_facts(session):
    _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, 74)
    session.commit()

    first = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8))
    second = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8))

    assert second.decision_id == first.decision_id
    assert session.query(DecisionRecord).count() == 1
