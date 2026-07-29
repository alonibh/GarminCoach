from datetime import date, datetime

import pytest

from coach.decision_engine import evaluate_selected_workout_recovery
from coach.renderer import recovery_fact_lines, render_morning
from db import DailyHealth, DecisionRecord, PlannedSession, ProgramCursor, Sleep
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
    health = session.get(DailyHealth, TARGET)
    if health:
        health.training_readiness = score
    else:
        session.add(DailyHealth(day=TARGET, training_readiness=score))


def _evaluate(session, planned_id=None):
    return evaluate_selected_workout_recovery(session, planned_session_id=planned_id, target=TARGET,
                                               evaluated_at=datetime(2026, 7, 6, 8))


def _recovery_action(row, recommendation):
    return [{
        "type": "choose_recovery_outcome", "policy_version": "v1",
        "planned_session_id": row.id, "decision_date": TARGET.isoformat(),
        "choices": ["original", "active_recovery", "rest"],
        "recommended_choice": recommendation,
    }]


def _informational_context(session):
    session.add(Sleep(
        day=TARGET,
        sleep_start_time=datetime(2026, 7, 5, 23, 37),
        sleep_end_time=datetime(2026, 7, 6, 6, 45),
        total_s=7.1 * 3600,
        score=86,
    ))
    session.add(DailyHealth(
        day=TARGET,
        hrv_status="BALANCED",
        hrv_overnight=54,
        recovery_time_minutes=95,
        resting_hr=48,
        stress_avg=22,
    ))
    for signal in (
        freshness.SLEEP, freshness.SLEEP_SCORE, freshness.HRV_STATUS,
        freshness.HRV, freshness.RECOVERY_TIME, freshness.RESTING_HR,
        freshness.STRESS,
    ):
        freshness.record_signal(session, signal, TARGET, freshness.FRESH, "test")


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


def test_linked_optional_recovery_and_another_date_are_ineligible(session):
    _program, source_sessions = _add_program(session)
    source_sessions[0].session_role = "optional_recovery"
    _planned(session, program_session_id=source_sessions[0].id)
    other_day = _planned(session, target_date=date(2026, 7, 7))

    assert _evaluate(session).decision_type == "NO_SELECTED_WORKOUT"
    assert _evaluate(session, other_day.id).reason_codes == ["PLANNED_SESSION_NOT_ELIGIBLE_FOR_DECISION_DATE"]


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
    recommendation = "rest" if category == "Poor" else "original" if category == "Low" else None
    actions = _recovery_action(row, recommendation) if recommendation else []
    assert (result.readiness_category, result.decision_type, result.permitted_actions) == (category, outcome, actions)
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
    unsupported = _evaluate(session, row.id)
    assert "UNSUPPORTED" in unsupported.reason_codes[0]
    assert "Garmin Training Readiness:" not in render_morning(session, unsupported)[0]
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, None)
    unknown = _evaluate(session, row.id)
    assert "UNVERIFIED" in unknown.reason_codes[0]
    assert "Garmin Training Readiness:" not in render_morning(session, unknown)[0]


def test_telegram_uses_canonical_facts_without_private_values(session, monkeypatch):
    row = _planned(session)
    _informational_context(session)
    freshness.set_capability_override(session, freshness.TRAINING_READINESS, "unsupported")
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("calendar access is forbidden")),
    )

    result = _evaluate(session, row.id)
    text, markup, interaction_ids = render_morning(session, result)

    assert "Sleep 23:37-06:45: 7.1h" in text
    assert "Garmin Sleep Score: 86 (Good)" in text
    assert "Garmin HRV Status: BALANCED" in text
    assert "Recovery Time: 1h 35m" in text
    assert "informational only" in text
    assert "this device does not support it" in text
    assert "TRAINING_READINESS_UNSUPPORTED" not in text
    assert "54 ms" not in text and "48 bpm" not in text and "Stress: 22" not in text
    assert markup is None and interaction_ids == []


@pytest.mark.parametrize(
    "score,category,outcome,body",
    [
        (50, "Moderate", "KEEP_SELECTED_WORKOUT", "Planned: Full Body at 18:00."),
        (74, "Moderate", "KEEP_SELECTED_WORKOUT", "Planned: Full Body at 18:00."),
        (75, "High", "KEEP_SELECTED_WORKOUT", "Planned: Full Body at 18:00."),
        (94, "High", "KEEP_SELECTED_WORKOUT", "Planned: Full Body at 18:00."),
        (95, "Prime", "KEEP_SELECTED_WORKOUT", "Planned: Full Body at 18:00."),
        (100, "Prime", "KEEP_SELECTED_WORKOUT", "Planned: Full Body at 18:00."),
        (25, "Low", "KEEP_SELECTED_WORKOUT_WITH_WARNING", "Garmin Training Readiness is Low; this is a warning only."),
        (49, "Low", "KEEP_SELECTED_WORKOUT_WITH_WARNING", "Garmin Training Readiness is Low; this is a warning only."),
        (1, "Poor", "REST_RECOMMENDED", "Garmin Training Readiness is Poor; the selected workout remains pending."),
        (24, "Poor", "REST_RECOMMENDED", "Garmin Training Readiness is Poor; the selected workout remains pending."),
    ],
)
def test_telegram_shows_only_evaluator_granted_training_readiness(session, score, category, outcome, body):
    row = _planned(session)
    _readiness(session, score)

    result = _evaluate(session, row.id)
    text, markup, interaction_ids = render_morning(session, result)

    line = f"Garmin Training Readiness: {score} ({category})"
    assert result.decision_type == outcome
    assert text.count(line) == 1
    assert body in text
    assert markup is None and interaction_ids == []


def test_no_or_multiple_selected_workouts_do_not_show_training_readiness(session):
    _readiness(session, 74)
    no_workout, markup, interaction_ids = render_morning(session, _evaluate(session))

    first, second = _planned(session), _planned(session, title="Run", suggested_time="07:00")
    multiple, multiple_markup, multiple_ids = render_morning(session, _evaluate(session))

    assert "Garmin Training Readiness:" not in no_workout
    assert "Garmin Training Readiness:" not in multiple
    assert first.status == second.status == "planned"
    assert markup is multiple_markup is None
    assert interaction_ids == multiple_ids == []


def test_program_rest_hides_raw_training_readiness(session):
    program, source = _add_program(session)
    row = _planned(session)
    _readiness(session, 100)
    session.add(ProgramCursor(
        program_id=program.id,
        next_program_session_id=source[1].id,
        last_completed_program_session_id=source[0].id,
        last_completed_at=datetime(2026, 7, 5, 9),
        policy_version="v1",
        created_at=datetime(2026, 7, 1),
        updated_at=datetime(2026, 7, 5),
    ))

    result = _evaluate(session, row.id)
    text, markup, interaction_ids = render_morning(session, result)

    assert result.decision_type == "PROGRAM_REST_RECOMMENDED"
    assert "Garmin Training Readiness:" not in text
    assert markup is None and interaction_ids == []


@pytest.mark.parametrize(
    "state,score,expected",
    [
        (freshness.EXPECTED_PENDING, 70, "reading is still pending"),
        (freshness.MISSING, None, "no current reading is available"),
        (freshness.STALE, 70, "reading is stale"),
        (freshness.ERROR, 70, "Garmin returned an error"),
        (freshness.FRESH, 0, "reading is invalid"),
        (freshness.FRESH, 101, "reading is invalid"),
        (freshness.FRESH, None, "reading is invalid"),
    ],
)
def test_no_authority_reasons_are_human_readable(session, state, score, expected):
    row = _planned(session)
    _readiness(session, score, state)
    text, markup, interaction_ids = render_morning(session, _evaluate(session, row.id))

    assert expected in text
    assert "Garmin Training Readiness:" not in text
    assert "TRAINING_READINESS_" not in text
    assert markup is None and interaction_ids == []


def test_plan_only_omits_all_recovery_facts(session):
    row = _planned(session)
    _informational_context(session)
    _readiness(session, 74)

    text, markup, interaction_ids = render_morning(session, _evaluate(session, row.id), plan_only=True)

    assert text == "Planned: Full Body at 18:00."
    assert "Sleep" not in text and "HRV" not in text and "Recovery" not in text
    assert "Garmin Training Readiness:" not in text
    assert markup is None and interaction_ids == []


def test_dashboard_fact_lines_use_the_same_canonical_signals(session):
    row = _planned(session)
    _informational_context(session)
    _readiness(session, 74)

    result = _evaluate(session, row.id)
    facts = recovery_fact_lines(session, result, include_private_facts=True)

    assert facts == [
        "Sleep 23:37-06:45: 7.1h", "Garmin Sleep Score: 86 (Good)",
        "Garmin HRV Status: BALANCED", "Recovery Time: 1h 35m",
        "Overnight HRV: 54 ms", "Resting HR: 48 bpm", "Stress: 22",
    ]
    assert all(item["signal"] != "sleep_duration_hours" for item in result.observations)
    assert isinstance(next(item["value"] for item in result.observations if item["signal"] == "sleep_score"), int)


def test_dashboard_recovery_panel_keeps_one_authoritative_score_and_no_controls(session, monkeypatch):
    import app as app_module

    row = _planned(session)
    _informational_context(session)
    _readiness(session, 74)
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("calendar access is forbidden")),
    )
    result = _evaluate(session, row.id)
    rendered = app_module.templates.get_template("dashboard.html").render(
        recovery_panel={
            "outcome": "Keep Selected Workout",
            "name": result.planned_session_name,
            "time": result.planned_start_time,
            "score": result.readiness_score,
            "category": result.readiness_category,
            "reason": "",
            "facts": recovery_fact_lines(session, result, include_private_facts=True),
        },
        today_label="Monday, Jul 06",
        needs_login=False,
        last_sync_at=None,
        device_last_upload=None,
        sync_running=False,
        sync_summary=None,
        fitness_tiles=[],
        readiness_tiles=[],
        health_series=[],
        sleep_series=[],
        activities=[],
    )

    panel = rendered[rendered.index('id="selected-workout-recovery"'):rendered.index("</section>", rendered.index('id="selected-workout-recovery"'))]
    assert panel.count("Garmin Training Readiness:") == 1
    assert "74 (Moderate)" in panel
    assert "Sleep 23:37-06:45: 7.1h" in panel
    assert "<button" not in panel and "<form" not in panel


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
    assert (row.status, cursor.next_program_session_id, result.permitted_actions) == (
        "planned", source[1].id, _recovery_action(row, "rest")
    )


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
