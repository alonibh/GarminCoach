from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text

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
from db import StrengthProgressionEvidence, StrengthProgressionEvidenceHead, StrengthProgressionPolicy, StrengthProgressionProposal, StrengthProgressionStreak


def _result(classification=AppearanceClassification.INCREASE_QUALIFIED, candidate=72500):
    return AppearanceClassificationResult(classification, 70000, candidate, ({"set_index": 0, "edited": True},), ())


def _proposal(direction=ProposalDirection.INCREASE, suggested=72500, key="proposal"):
    return ProposalResult(direction, 70000, suggested, ("evidence-one", "evidence-two"),
        "strength-progression-v1", "fp", key, ())


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
    # The proposal FK evidence IDs are deliberately supplied as nullable audit IDs in a real populated database;
    # SQLite's fixture connection does not enable FK checks, matching the standard session fixture.
    first = create_or_replace_pending_proposal(session, session_exercise_id=3, proposal=_proposal(key="key-one"))
    assert create_or_replace_pending_proposal(session, session_exercise_id=3, proposal=_proposal(key="key-two")) is first
    replacement = create_or_replace_pending_proposal(session, session_exercise_id=3, proposal=_proposal(suggested=75000, key="key-three"))
    assert replacement.proposal_id == "key-three"
    assert first.status == "superseded" and first.current_pending_key is None
    assert session.query(StrengthProgressionProposal).filter_by(status="pending").count() == 1
    assert mark_pending_proposal_stale(session, session_exercise_id=3, policy_version="strength-progression-v1", prescription_fingerprint="fp").status == "stale"


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
        indexes = {item[1] for item in conn.execute(text("PRAGMA index_list('strength_progression_proposals')"))}
        assert "ix_strength_progression_proposals_current_pending_key" in indexes
    assert "strength_progression_evidence" in inspect(engine).get_table_names()
    engine.dispose()
