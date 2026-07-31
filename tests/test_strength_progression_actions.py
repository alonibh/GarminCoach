from datetime import datetime, timedelta

import pytest
from coach.strength_progression import (
    AppearanceClassification, AppearanceClassificationResult, calculate_proposal,
    derive_streak,
)
from coach.strength_progression_actions import (
    ProgressionActionOutcome, approve_progression_proposal, list_progression_review,
    reject_progression_proposal,
)
from coach.strength_progression_integration import _prescription
from coach.strength_progression_store import (
    append_evidence, create_or_replace_pending_proposal, evidence_record,
    load_current_evidence,
)
from db import (
    Activity, ProgramSession, SessionExercise, StrengthProgressionEvidenceBoundary,
    StrengthProgressionEvidence, StrengthProgressionPolicy, TrainingProgram,
)


def _pending(session):
    policy = StrengthProgressionPolicy(policy_version="review-v1", global_increment_grams=2500,
        weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35, is_active=True)
    program = TrainingProgram(name="Active", active=True, status="active")
    session.add_all((policy, program)); session.flush()
    program_session = ProgramSession(program_id=program.id, name="Push")
    session.add(program_session); session.flush()
    exercise = SessionExercise(program_session_id=program_session.id, exercise_name="Bench",
        exercise_key="BENCH", garmin_category="BENCH", garmin_name="BENCH", sets=3, reps=10,
        weight_kg=70.0)
    session.add(exercise); session.flush()
    activities = (Activity(id=8101), Activity(id=8102))
    session.add_all(activities); session.flush()
    prescription = _prescription(exercise, program.id, program_session.id)
    now = datetime(2026, 7, 31, 12)
    result = AppearanceClassificationResult(AppearanceClassification.INCREASE_QUALIFIED, 70000, 70000, (
        {"set_index": 0, "set_type": "REST", "reps": None, "weight_kg_source": None,
         "weight_grams": None, "duration_seconds": 60, "edited": False, "excluded": "rest"},
        {"set_index": 1, "set_type": "WORK", "reps": 10, "weight_kg_source": "70.0",
         "weight_grams": 70000, "duration_seconds": None, "edited": True},
    ), ())
    evidence = [append_evidence(session, session_exercise_id=exercise.id, activity_id=activity.id,
        policy_version=policy.policy_version,
        prescription_fingerprint=__import__("coach.strength_progression", fromlist=["prescription_fingerprint"]).prescription_fingerprint(prescription),
        source_fingerprint=f"source-{index}", appearance_at=now - timedelta(days=2-index), result=result,
        program_id=program.id, program_session_id=program_session.id, prescribed_sets=3, target_reps=10)
        for index, activity in enumerate(activities)]
    records = [evidence_record(row) for row in evidence]
    streak = derive_streak(policy, records, session_exercise_id=exercise.id,
        prescription=records[0].prescription_fingerprint, as_of=now)
    proposal = create_or_replace_pending_proposal(session, session_exercise_id=exercise.id,
        program_id=program.id, program_session_id=program_session.id,
        proposal=calculate_proposal(policy, prescription, streak, records))
    session.flush()
    return now, exercise, proposal


def test_approved_weight_is_the_only_template_mutation_and_is_idempotent(session):
    now, exercise, proposal = _pending(session)
    original = {name: getattr(exercise, name) for name in (
        "exercise_name", "sets", "reps", "duration_seconds", "rest_seconds", "warmup_enabled", "order_index",
    )}
    result = approve_progression_proposal(session, proposal.proposal_id, entered_weight_kg="72.625", now=now)
    assert result.outcome == ProgressionActionOutcome.APPLIED
    assert exercise.weight_kg == 72.75
    assert {name: getattr(exercise, name) for name in original} == original
    assert proposal.status == "applied" and proposal.approved_weight_grams == 72750 and proposal.current_pending_key is None
    assert approve_progression_proposal(session, proposal.proposal_id, entered_weight_kg="72.625", now=now).outcome == ProgressionActionOutcome.ALREADY_APPLIED
    assert approve_progression_proposal(session, proposal.proposal_id, entered_weight_kg="75", now=now).outcome == ProgressionActionOutcome.CONFLICT


def test_rejection_creates_one_cutoff_and_filters_old_evidence(session):
    now, exercise, proposal = _pending(session)
    assert reject_progression_proposal(session, proposal.proposal_id, now=now).outcome == ProgressionActionOutcome.REJECTED
    assert exercise.weight_kg == 70.0
    assert session.query(StrengthProgressionEvidenceBoundary).count() == 1
    assert load_current_evidence(session, session_exercise_id=exercise.id, policy_version=proposal.policy_version,
        prescription_fingerprint=proposal.prescription_fingerprint) == []
    assert reject_progression_proposal(session, proposal.proposal_id, now=now).outcome == ProgressionActionOutcome.ALREADY_REJECTED
    assert session.query(StrengthProgressionEvidenceBoundary).count() == 1


def test_review_uses_typed_set_presentation_and_rejection_excludes_cutoff_revisions(session):
    now, exercise, proposal = _pending(session)
    page = list_progression_review(session, now=now)
    row = page.pending[0].evidence[0].decisive_sets
    assert row[0].excluded == "rest" and row[0].normalized_weight == "Unavailable"
    assert row[1].source_weight == "70.0 kg" and row[1].normalized_weight == "70 kg" and row[1].edited
    assert reject_progression_proposal(session, proposal.proposal_id, now=now).outcome == ProgressionActionOutcome.REJECTED
    original = session.get(__import__("db", fromlist=["StrengthProgressionEvidence"]).StrengthProgressionEvidence,
        proposal.decisive_evidence_two_id)
    revised = append_evidence(session, session_exercise_id=exercise.id, activity_id=original.activity_id,
        policy_version=proposal.policy_version, prescription_fingerprint=proposal.prescription_fingerprint,
        source_fingerprint="post-rejection-correction", appearance_at=original.appearance_at,
        result=AppearanceClassificationResult(AppearanceClassification.INCREASE_QUALIFIED, 70000, 70000, (), ()),
        program_id=original.program_id, program_session_id=original.program_session_id)
    assert revised.evidence_id != original.evidence_id
    assert load_current_evidence(session, session_exercise_id=exercise.id, policy_version=proposal.policy_version,
        prescription_fingerprint=proposal.prescription_fingerprint) == []


@pytest.mark.parametrize("decisive_attribute", ["decisive_evidence_one_id", "decisive_evidence_two_id"])
def test_harmless_correction_refreshes_pending_support_before_approval(session, decisive_attribute):
    now, exercise, proposal = _pending(session)
    original_id = getattr(proposal, decisive_attribute)
    original = session.get(StrengthProgressionEvidence, original_id)
    result = AppearanceClassificationResult(AppearanceClassification.INCREASE_QUALIFIED, 70000, 70000, (), ())
    revised = append_evidence(session, session_exercise_id=exercise.id, activity_id=original.activity_id,
        policy_version=proposal.policy_version, prescription_fingerprint=proposal.prescription_fingerprint,
        source_fingerprint=f"harmless-correction-{original.activity_id}", appearance_at=original.appearance_at,
        result=result, program_id=original.program_id, program_session_id=original.program_session_id)
    assert revised.evidence_id != original.evidence_id
    page = list_progression_review(session, now=now)
    assert [item.proposal_id for item in page.pending] == [proposal.proposal_id]
    assert revised.evidence_id in {proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id}
    assert session.get(StrengthProgressionEvidence, original_id) is not None
    assert approve_progression_proposal(session, proposal.proposal_id, entered_weight_kg="72.5", now=now).outcome == ProgressionActionOutcome.APPLIED
    assert revised.evidence_id in {proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id}
    assert session.query(__import__("db", fromlist=["StrengthProgressionProposal"]).StrengthProgressionProposal).count() == 1


def test_material_correction_stales_instead_of_refreshing_pending_support(session):
    now, exercise, proposal = _pending(session)
    original = session.get(StrengthProgressionEvidence, proposal.decisive_evidence_one_id)
    neutral = AppearanceClassificationResult(AppearanceClassification.NEUTRAL, 70000, None, (), ())
    append_evidence(session, session_exercise_id=exercise.id, activity_id=original.activity_id,
        policy_version=proposal.policy_version, prescription_fingerprint=proposal.prescription_fingerprint,
        source_fingerprint="material-correction", appearance_at=original.appearance_at, result=neutral,
        program_id=original.program_id, program_session_id=original.program_session_id)
    page = list_progression_review(session, now=now)
    assert not page.pending and proposal.status == "stale" and proposal.current_pending_key is None
    assert proposal.decisive_evidence_one_id == original.evidence_id
