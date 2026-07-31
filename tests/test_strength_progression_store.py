from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db
from coach.strength_progression import (
    AppearanceClassification, AppearanceClassificationResult, EvidenceRecord,
    ExercisePrescription, ProgressionPolicy, ProposalDirection, ProposalResult,
    StreakResult, fingerprint,
)
from coach.strength_progression_store import (
    append_evidence, create_or_replace_pending_proposal, load_active_policy,
    mark_pending_proposal_stale, upsert_streak,
)
from db import (
    Activity, ActivityProgramMatch, ProgramSession, SessionExercise,
    StrengthProgressionEvidence, StrengthProgressionEvidenceHead,
    StrengthProgressionPolicy, StrengthProgressionProposal,
    StrengthProgressionStreak, TrainingProgram,
)


def _result(classification=AppearanceClassification.INCREASE_QUALIFIED, candidate=72500):
    return AppearanceClassificationResult(classification, 70000, candidate, ({"set_index": 0, "edited": True},), ())


def _proposal(evidence_ids, direction=ProposalDirection.INCREASE, suggested=72500, key="proposal"):
    return ProposalResult(direction, 70000, suggested, evidence_ids,
        "strength-progression-v1", "fp", key, ())


@pytest.fixture
def fk_session():
    engine = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _fk_parents(session):
    policy = StrengthProgressionPolicy(policy_version="strength-progression-v1", global_increment_grams=2500,
        weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35, is_active=True)
    program = TrainingProgram(name="Program")
    session.add_all((policy, program)); session.flush()
    program_session = ProgramSession(program_id=program.id, name="Session")
    session.add(program_session); session.flush()
    exercise = SessionExercise(program_session_id=program_session.id, exercise_name="Bench")
    first_activity, second_activity = Activity(id=101), Activity(id=102)
    session.add_all((exercise, first_activity, second_activity)); session.flush()
    matches = [ActivityProgramMatch(activity_id=activity.id, program_id=program.id, program_session_id=program_session.id,
        match_method="test", policy_version="test", matched_at=datetime(2026, 1, 1)) for activity in (first_activity, second_activity)]
    session.add_all(matches); session.flush()
    evidence = [append_evidence(session, session_exercise_id=exercise.id, activity_id=activity.id,
        activity_program_match_id=match.id, program_id=program.id, program_session_id=program_session.id,
        policy_version=policy.policy_version, prescription_fingerprint="fp", source_fingerprint=f"source-{activity.id}",
        appearance_at=datetime(2026, 1, activity.id - 100), result=_result(), prescribed_sets=3, target_reps=10)
        for activity, match in zip((first_activity, second_activity), matches)]
    session.flush()
    return policy, program, program_session, exercise, (first_activity, second_activity), tuple(matches), tuple(evidence)


def test_store_requires_one_active_policy_and_moves_immutable_evidence_head(session):
    session.add(StrengthProgressionPolicy(policy_version="strength-progression-v1", global_increment_grams=2500,
        weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35, is_active=True))
    session.flush()
    assert load_active_policy(session).global_increment_grams == 2500
    first = append_evidence(session, session_exercise_id=3, activity_id=9, policy_version="strength-progression-v1",
        prescription_fingerprint="fp", source_fingerprint="raw-one", appearance_at=datetime(2026, 1, 1), result=_result(), prescribed_sets=3, target_reps=10)
    assert append_evidence(session, session_exercise_id=3, activity_id=9, policy_version="strength-progression-v1",
        prescription_fingerprint="fp", source_fingerprint="raw-one", appearance_at=datetime(2026, 1, 1), result=_result()) is first
    revised = append_evidence(session, session_exercise_id=3, activity_id=9, policy_version="strength-progression-v1",
        prescription_fingerprint="fp", source_fingerprint="manual-correction", appearance_at=datetime(2026, 1, 1), result=_result(AppearanceClassification.NEUTRAL, None))
    assert session.query(StrengthProgressionEvidence).count() == 2
    assert revised.supersedes_evidence_id == first.evidence_id
    assert session.get(StrengthProgressionEvidenceHead, (3, 9)).current_evidence_id == revised.evidence_id


def test_streak_and_pending_proposal_are_idempotent_and_supersede(session):
    row = upsert_streak(session, session_exercise_id=3, policy_version="strength-progression-v1", prescription_fingerprint="fp",
        result=StreakResult(2, 0, AppearanceClassification.INCREASE_QUALIFIED, datetime(2026, 1, 1), ("a", "b")))
    assert session.get(StrengthProgressionStreak, (3, "strength-progression-v1", "fp")) is row
    assert row.increase_count == 2


def test_pending_proposal_replay_never_reactivates_history(fk_session):
    _, _, _, exercise, _, _, evidence = _fk_parents(fk_session)
    ids = tuple(row.evidence_id for row in evidence)
    first = create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, key="A"))
    fk_session.flush()
    second = create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, suggested=75000, key="B"))
    fk_session.flush()
    assert first.status == "superseded" and second.status == "pending"
    assert create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, key="A")) is second
    assert create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, suggested=75000, key="B")) is second
    assert create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, suggested=75000, key="same-value-new-key")) is second
    assert fk_session.query(StrengthProgressionProposal).filter_by(status="pending").count() == 1
    assert mark_pending_proposal_stale(fk_session, session_exercise_id=exercise.id, policy_version="strength-progression-v1", prescription_fingerprint="fp").status == "stale"
    assert create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, key="A")) is None
    assert first.status == "superseded" and first.current_pending_key is None
    replacement = create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=_proposal(ids, suggested=77500, key="C"))
    fk_session.flush()
    assert replacement.status == "pending" and fk_session.query(StrengthProgressionProposal).filter_by(status="pending").count() == 1


def test_fk_constraints_and_set_null_audit_history(fk_session):
    _, program, program_session, exercise, activities, matches, evidence = _fk_parents(fk_session)
    exercise_id, first_evidence_id = exercise.id, evidence[0].evidence_id
    ids = tuple(row.evidence_id for row in evidence)
    proposal = create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, program_id=program.id,
        program_session_id=program_session.id, proposal=_proposal(ids, key="valid"))
    fk_session.flush()
    assert fk_session.connection().execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    assert proposal.status == "pending"
    invalid = _proposal(("missing-one", "missing-two"), suggested=75000, key="missing")
    with pytest.raises(IntegrityError):
        with fk_session.begin_nested():
            create_or_replace_pending_proposal(fk_session, session_exercise_id=exercise.id, proposal=invalid)
            fk_session.flush()
    duplicate = StrengthProgressionProposal(
        proposal_id="duplicate", session_exercise_id_snapshot=exercise.id,
        policy_version="strength-progression-v1", prescription_fingerprint="fp",
        direction="increase", current_weight_grams=70000, suggested_weight_grams=72500,
        status="pending", decisive_evidence_one_id=ids[0], decisive_evidence_two_id=ids[1],
        reason_codes_json="[]", idempotency_key="duplicate", current_pending_key=proposal.current_pending_key,
    )
    with pytest.raises(IntegrityError):
        with fk_session.begin_nested():
            fk_session.add(duplicate); fk_session.flush()
    with pytest.raises(IntegrityError):
        with fk_session.begin_nested():
            fk_session.add(StrengthProgressionEvidenceHead(session_exercise_id=999, activity_id=999, current_evidence_id="missing"))
            fk_session.flush()
    with pytest.raises(IntegrityError):
        with fk_session.begin_nested():
            fk_session.delete(evidence[0]); fk_session.flush()
    # Mutable ownership deletes retain immutable evidence/proposal rows, with
    # FKs nullified and numeric snapshots still available for audit.
    fk_session.execute(text("DELETE FROM activities WHERE id IN (101, 102)"))
    fk_session.execute(text("DELETE FROM training_programs WHERE id = :id"), {"id": program.id})
    fk_session.flush()
    fk_session.expire_all()
    history = fk_session.get(StrengthProgressionEvidence, first_evidence_id)
    persisted_proposal = fk_session.get(StrengthProgressionProposal, proposal.proposal_id)
    assert history.activity_id is history.activity_program_match_id is history.program_id is history.program_session_id is history.session_exercise_id is None
    assert history.activity_id_snapshot == 101 and history.session_exercise_id_snapshot == exercise_id
    assert persisted_proposal.program_id is persisted_proposal.program_session_id is persisted_proposal.session_exercise_id is None
    assert fk_session.connection().execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


@pytest.mark.parametrize("field,value", [
    ("global_increment_grams", 0), ("weight_quantum_grams", 0),
    ("weight_quantum_grams", 500), ("global_increment_grams", 2601),
    ("required_consecutive", 0), ("evidence_window_days", 0),
])
def test_active_policy_invalid_values_fail_closed(session, field, value):
    values = dict(policy_version="strength-progression-v1", global_increment_grams=2500,
        weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35, is_active=True)
    values[field] = value
    session.add(StrengthProgressionPolicy(**values)); session.flush()
    with pytest.raises(RuntimeError):
        load_active_policy(session)


def test_init_db_migrates_and_seeds_progression_foundation_idempotently(tmp_path):
    path = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    # Representative prior state survives ordinary startup.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE durable_example (value TEXT)"))
        conn.execute(text("INSERT INTO durable_example VALUES ('keep')"))
    db.init_db(engine)
    db.init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("SELECT value FROM durable_example")).scalar_one() == "keep"
        assert conn.execute(text("SELECT COUNT(*) FROM strength_progression_policies WHERE is_active = 1")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM app_migrations WHERE migration_key = 'strength_progression_foundation_2026_07_30_v1'")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM app_migrations WHERE migration_key = 'strength_progression_review_actions_2026_07_31_v1'")).scalar_one() == 1
        indexes = {item[1] for item in conn.execute(text("PRAGMA index_list('strength_progression_proposals')"))}
        assert "ix_strength_progression_proposals_current_pending_key" in indexes
        boundary_indexes = {item[1] for item in conn.execute(text("PRAGMA index_list('strength_progression_evidence_boundaries')"))}
        assert {"ix_strength_boundary_exercise_policy_prescription", "ix_strength_boundary_cutoff"}.issubset(boundary_indexes)
    assert "strength_progression_evidence" in inspect(engine).get_table_names()
    assert "strength_progression_evidence_boundaries" in inspect(engine).get_table_names()
    engine.dispose()
