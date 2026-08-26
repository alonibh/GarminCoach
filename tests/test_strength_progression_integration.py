from datetime import datetime, timedelta

from coach.strength_progression_integration import (
    RecalculationCause, process_activity_recalculation,
    process_pending_activity_recalculations, request_activity_recalculation,
)
from db import (
    Activity, ActivityProgramMatch, ExerciseSet, ProgramSession, SessionExercise,
    StrengthProgressionEvidence, StrengthProgressionEvidenceHead,
    StrengthProgressionPolicy, StrengthProgressionProposal, SyncState,
    TrainingProgram,
)


def _activity(session, identifier, when):
    row = Activity(id=identifier, activity_type="strength_training", start_time=when)
    session.add(row)
    return row


def _seed(session):
    policy = StrengthProgressionPolicy(
        policy_version="strength-progression-v1", global_increment_grams=2500,
        weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35,
        is_active=True,
    )
    program = TrainingProgram(name="P", active=True, status="active")
    session.add_all((policy, program)); session.flush()
    planned = ProgramSession(program_id=program.id, name="A")
    session.add(planned); session.flush()
    exercise = SessionExercise(
        program_session_id=planned.id, exercise_name="Bench", exercise_key="BENCH",
        garmin_category="BENCH", garmin_name="BENCH", sets=2, reps=8,
        weight_kg=70, order_index=0,
    )
    session.add(exercise); session.flush()
    return program, planned, exercise


def _match_with_sets(session, activity, program, planned, *, weight=70, reps=8):
    match = ActivityProgramMatch(activity_id=activity.id, program_id=program.id,
        program_session_id=planned.id, match_method="test", policy_version="test",
        matched_at=activity.start_time)
    session.add(match)
    for index in range(2):
        session.add(ExerciseSet(activity_id=activity.id, set_index=index,
            set_type="ACTIVE", exercise_category="BENCH", exercise_name="BENCH",
            reps=reps, weight_kg=weight, edited=False))
    session.add(SyncState(key=f"activity_strength_sets_checked:{activity.id}", value="complete"))
    session.flush()


def test_two_explicit_activity_events_create_one_pending_proposal(session):
    program, planned, exercise = _seed(session)
    first = _activity(session, 100, datetime(2026, 1, 1))
    second = _activity(session, 101, datetime(2026, 1, 10))
    _match_with_sets(session, first, program, planned)
    _match_with_sets(session, second, program, planned, weight=72.5)

    first_report = process_activity_recalculation(session, first.id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    assert first_report.evidence_created == 1
    assert session.query(StrengthProgressionProposal).count() == 0
    second_report = process_activity_recalculation(session, second.id, cause=RecalculationCause.ACTIVITY_PROGRAM_MATCH_CREATED)
    assert second_report.evidence_created == 1
    assert session.query(StrengthProgressionProposal).filter_by(status="pending").count() == 1
    assert session.query(StrengthProgressionEvidenceHead).count() == 2

    again = process_activity_recalculation(session, second.id, cause=RecalculationCause.RETRY)
    assert again.evidence_reused == 1
    assert session.query(StrengthProgressionEvidence).count() == 2
    assert session.query(StrengthProgressionProposal).filter_by(status="pending").count() == 1
    assert exercise.weight_kg == 70


def test_authoritative_correction_creates_immutable_revision_and_moves_head(session):
    program, planned, exercise = _seed(session)
    activity = _activity(session, 102, datetime(2026, 1, 1))
    _match_with_sets(session, activity, program, planned)
    process_activity_recalculation(session, activity.id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    prior = session.get(StrengthProgressionEvidenceHead, (exercise.id, activity.id)).current_evidence_id
    edited = session.query(ExerciseSet).filter_by(activity_id=activity.id, set_index=0).one()
    edited.reps, edited.edited = 7, True
    session.flush()
    request_activity_recalculation(session, activity.id, cause=RecalculationCause.MANUAL_SET_CORRECTED)
    report = process_activity_recalculation(session, activity.id, cause=RecalculationCause.MANUAL_SET_CORRECTED)
    assert report.evidence_created == 1
    assert session.query(StrengthProgressionEvidence).count() == 2
    assert session.get(StrengthProgressionEvidenceHead, (exercise.id, activity.id)).current_evidence_id != prior
    process_activity_recalculation(session, activity.id, cause=RecalculationCause.MANUAL_SET_CORRECTED)
    assert session.query(StrengthProgressionEvidence).count() == 2


def test_journal_merges_requests_and_no_unrequested_rows_are_backfilled(session):
    program, planned, _ = _seed(session)
    old = _activity(session, 103, datetime(2025, 1, 1))
    _match_with_sets(session, old, program, planned)
    assert process_pending_activity_recalculations(session).processed == 0
    assert session.query(StrengthProgressionEvidence).count() == 0
    request_activity_recalculation(session, old.id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    request_activity_recalculation(session, old.id, cause=RecalculationCause.MANUAL_SET_CORRECTED)
    value = session.get(SyncState, f"strength_progression_recalc_activity:{old.id}").value
    assert "manual_set_corrected" in value and "strength_sets_resolved" in value
    report = process_pending_activity_recalculations(session, limit=1)
    assert report.processed == 1
    assert session.get(SyncState, f"strength_progression_recalc_activity:{old.id}") is None


def test_retained_journal_boundary_is_stable_for_retry_and_ignores_retry_cause(session, monkeypatch):
    import coach.strength_progression_integration as integration

    request_activity_recalculation(session, 999, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    first = process_activity_recalculation(session, 999, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    # A no-activity request completes normally, so create one retained request
    # by forcing the derived processing savepoint to fail.
    request_activity_recalculation(session, 998, cause=RecalculationCause.ACTIVITY_PROGRAM_MATCH_CREATED)
    original = integration._process
    monkeypatch.setattr(integration, "_process", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("test")))
    failed = process_activity_recalculation(session, 998, cause=RecalculationCause.ACTIVITY_PROGRAM_MATCH_CREATED)
    monkeypatch.setattr(integration, "_process", original)
    retried = process_activity_recalculation(session, 998, cause=RecalculationCause.RETRY)
    assert first.boundary_id
    assert failed.dirty_key_retained and failed.boundary_id == retried.boundary_id
    journal = session.get(SyncState, "strength_progression_recalc_activity:998")
    assert journal is None


def test_journal_boundary_uses_merged_persisted_causes_in_request_order_independent_form(session):
    import coach.strength_progression_integration as integration

    request_activity_recalculation(session, 997, cause=RecalculationCause.MANUAL_SET_CORRECTED)
    request_activity_recalculation(session, 997, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    first = integration._activity_boundary_id(session, 997, RecalculationCause.RETRY)
    value = session.get(SyncState, "strength_progression_recalc_activity:997").value
    session.get(SyncState, "strength_progression_recalc_activity:997").value = value.replace(
        '["manual_set_corrected","strength_sets_resolved"]',
        '["strength_sets_resolved","manual_set_corrected"]',
    )
    assert integration._activity_boundary_id(session, 997, RecalculationCause.RETRY) == first
