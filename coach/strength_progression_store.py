"""Explicit SQLAlchemy persistence helpers for the Phase 4B1 engine.

The helpers require a caller-owned session and never commit, call Garmin, or
change a workout/program template.  They are intentionally not imported by
runtime sync or route code.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from coach.strength_progression import (
    AppearanceClassificationResult,
    EvidenceRecord,
    ProgressionPolicy,
    ProposalResult,
    StreakResult,
    canonical_json,
    fingerprint,
)
from db import (
    StrengthProgressionEvidence,
    StrengthProgressionEvidenceHead,
    StrengthProgressionPolicy,
    StrengthProgressionProposal,
    StrengthProgressionStreak,
)


def load_active_policy(session: Session) -> ProgressionPolicy:
    rows = session.query(StrengthProgressionPolicy).filter_by(is_active=True).all()
    if len(rows) != 1:
        raise RuntimeError("strength progression requires exactly one active policy")
    row = rows[0]
    return ProgressionPolicy(row.policy_version, row.global_increment_grams, row.weight_quantum_grams, row.required_consecutive, row.evidence_window_days)


def append_evidence(
    session: Session, *, session_exercise_id: int, activity_id: int | None,
    policy_version: str, prescription_fingerprint: str, source_fingerprint: str,
    appearance_at: datetime, result: AppearanceClassificationResult,
    program_id: int | None = None, program_session_id: int | None = None,
    activity_program_match_id: int | None = None, evidence_id: str | None = None,
    prescribed_sets: int | None = None, target_reps: int | None = None,
) -> StrengthProgressionEvidence:
    """Append a revision only once for a deterministic source input."""
    key = fingerprint({"session_exercise_id": session_exercise_id, "activity_id": activity_id,
        "policy_version": policy_version, "prescription_fingerprint": prescription_fingerprint,
        "source_fingerprint": source_fingerprint})
    existing = session.query(StrengthProgressionEvidence).filter_by(idempotency_key=key).one_or_none()
    if existing:
        return existing
    prior = None
    if activity_id is not None:
        head = session.get(StrengthProgressionEvidenceHead, (session_exercise_id, activity_id))
        prior = head.current_evidence_id if head else None
    row = StrengthProgressionEvidence(
        evidence_id=evidence_id or key, activity_id=activity_id,
        activity_program_match_id=activity_program_match_id, program_id=program_id,
        program_session_id=program_session_id, session_exercise_id=session_exercise_id,
        activity_id_snapshot=activity_id, activity_program_match_id_snapshot=activity_program_match_id,
        program_id_snapshot=program_id, program_session_id_snapshot=program_session_id,
        session_exercise_id_snapshot=session_exercise_id, policy_version=policy_version,
        prescription_fingerprint=prescription_fingerprint, source_fingerprint=source_fingerprint,
        appearance_at=appearance_at, classification=result.classification.value,
        current_weight_grams=result.current_weight_grams, candidate_weight_grams=result.candidate_weight_grams,
        prescribed_sets=prescribed_sets, target_reps=target_reps, decisive_sets_json=canonical_json(result.decisive_sets),
        reason_codes_json=canonical_json([reason.value for reason in result.reason_codes]),
        idempotency_key=key, supersedes_evidence_id=prior,
    )
    session.add(row)
    session.flush()
    if activity_id is not None:
        head = session.get(StrengthProgressionEvidenceHead, (session_exercise_id, activity_id))
        if head is None:
            session.add(StrengthProgressionEvidenceHead(session_exercise_id=session_exercise_id, activity_id=activity_id, current_evidence_id=row.evidence_id))
        else:
            head.current_evidence_id = row.evidence_id
    return row


def evidence_record(row: StrengthProgressionEvidence) -> EvidenceRecord:
    from coach.strength_progression import AppearanceClassification
    return EvidenceRecord(row.evidence_id, row.session_exercise_id_snapshot, row.policy_version,
        row.prescription_fingerprint, row.appearance_at, AppearanceClassification(row.classification), row.candidate_weight_grams)


def upsert_streak(session: Session, *, session_exercise_id: int, policy_version: str,
                  prescription_fingerprint: str, result: StreakResult) -> StrengthProgressionStreak:
    row = session.get(StrengthProgressionStreak, (session_exercise_id, policy_version, prescription_fingerprint))
    if row is None:
        row = StrengthProgressionStreak(session_exercise_id=session_exercise_id, policy_version=policy_version,
            prescription_fingerprint=prescription_fingerprint)
        session.add(row)
    row.increase_count = result.increase_count
    row.decrease_count = result.decrease_count
    row.last_classification = result.last_classification.value if result.last_classification else "unscorable"
    row.last_relevant_appearance_at = result.last_relevant_appearance_at
    row.decisive_evidence_ids_json = canonical_json(result.decisive_evidence_ids)
    return row


def _pending_key(session_exercise_id: int, policy_version: str, prescription_fingerprint: str) -> str:
    return f"{session_exercise_id}:{policy_version}:{prescription_fingerprint}"


def create_or_replace_pending_proposal(
    session: Session, *, session_exercise_id: int, proposal: ProposalResult,
    program_id: int | None = None, program_session_id: int | None = None,
) -> StrengthProgressionProposal | None:
    """Preserve unchanged pending rows; supersede a material replacement."""
    if proposal.direction is None or proposal.suggested_weight_grams is None or len(proposal.decisive_evidence_ids) != 2:
        return None
    key = _pending_key(session_exercise_id, proposal.policy_version, proposal.prescription_fingerprint)
    existing = session.query(StrengthProgressionProposal).filter_by(current_pending_key=key).one_or_none()
    if existing and existing.direction == proposal.direction.value and existing.suggested_weight_grams == proposal.suggested_weight_grams:
        return existing
    if existing:
        existing.status = "superseded"
        existing.current_pending_key = None
        existing.resolved_at = datetime.utcnow()
    row = session.query(StrengthProgressionProposal).filter_by(idempotency_key=proposal.idempotency_key).one_or_none()
    if row:
        return row
    row = StrengthProgressionProposal(
        proposal_id=proposal.idempotency_key, program_id=program_id, program_session_id=program_session_id,
        session_exercise_id=session_exercise_id, program_id_snapshot=program_id,
        program_session_id_snapshot=program_session_id, session_exercise_id_snapshot=session_exercise_id,
        policy_version=proposal.policy_version, prescription_fingerprint=proposal.prescription_fingerprint,
        direction=proposal.direction.value, current_weight_grams=proposal.current_weight_grams,
        suggested_weight_grams=proposal.suggested_weight_grams, status="pending",
        decisive_evidence_one_id=proposal.decisive_evidence_ids[0], decisive_evidence_two_id=proposal.decisive_evidence_ids[1],
        reason_codes_json=canonical_json([reason.value for reason in proposal.reason_codes]),
        idempotency_key=proposal.idempotency_key, current_pending_key=key,
        supersedes_proposal_id=existing.proposal_id if existing else None,
    )
    session.add(row)
    return row


def mark_pending_proposal_stale(session: Session, *, session_exercise_id: int,
                                policy_version: str, prescription_fingerprint: str,
                                status: str = "stale") -> StrengthProgressionProposal | None:
    if status not in {"stale", "superseded"}:
        raise ValueError("only derived stale/superseded states are permitted")
    row = session.query(StrengthProgressionProposal).filter_by(
        current_pending_key=_pending_key(session_exercise_id, policy_version, prescription_fingerprint)
    ).one_or_none()
    if row:
        row.status, row.current_pending_key, row.resolved_at = status, None, datetime.utcnow()
    return row
