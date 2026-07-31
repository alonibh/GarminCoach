"""Local, transaction-owned review and confirmed-action service for Phase 4C.

This module intentionally contains no HTTP, template, Garmin, notification,
calendar, model, or recovery dependencies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy.orm import Session

from coach.strength_progression import (
    calculate_proposal, derive_streak, normalize_weight_grams,
)
from coach.strength_progression_integration import _prescription
from coach.strength_progression_store import (
    append_rejection_boundary, evidence_record, load_active_policy, load_current_evidence,
    pending_key, reset_current_streak, stale_other_pending_proposals,
    transition_pending_proposal,
)
from db import (
    ProgramSession, SessionExercise, StrengthProgressionEvidence,
    StrengthProgressionEvidenceHead, StrengthProgressionProposal, TrainingProgram,
)

_MAX_WEIGHT_GRAMS = 500_000


class ProgressionAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ProgressionActionOutcome(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    ALREADY_APPLIED = "already_applied"
    ALREADY_REJECTED = "already_rejected"
    STALE = "stale"
    NOT_FOUND = "not_found"
    INVALID_WEIGHT = "invalid_weight"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class EvidenceReviewItem:
    appearance_at: datetime
    classification: str
    prescribed_sets: int | None
    target_reps: int | None
    decisive_sets: tuple[dict, ...]


@dataclass(frozen=True)
class ProposalReviewItem:
    proposal_id: str
    program_name: str
    session_name: str
    exercise_name: str
    direction: str
    current_weight_grams: int
    suggested_weight_grams: int
    global_increment_grams: int
    evidence: tuple[EvidenceReviewItem, ...]
    policy_version: str
    prescription_reference: str
    status: str
    resolved_at: datetime | None
    actionable: bool
    stale_reason: str | None = None


@dataclass(frozen=True)
class ProgressionReviewPage:
    pending: tuple[ProposalReviewItem, ...]
    history: tuple[ProposalReviewItem, ...]


@dataclass(frozen=True)
class ProposalActionResult:
    outcome: ProgressionActionOutcome
    proposal_id: str | None = None


def format_weight_grams(grams: int | None) -> str:
    if grams is None:
        return "Unavailable"
    whole, fraction = divmod(int(grams), 1000)
    suffix = {0: "", 250: ".25", 500: ".5", 750: ".75"}.get(fraction)
    return f"{whole}{suffix if suffix is not None else f'.{fraction:03d}'} kg"


def _safe_sets(raw: str) -> tuple[dict, ...] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        return None
    return tuple(parsed)


def _review_item(session: Session, proposal: StrengthProgressionProposal) -> ProposalReviewItem:
    policy = load_active_policy(session)
    program = session.get(TrainingProgram, proposal.program_id) if proposal.program_id else None
    program_session = session.get(ProgramSession, proposal.program_session_id) if proposal.program_session_id else None
    exercise = session.get(SessionExercise, proposal.session_exercise_id) if proposal.session_exercise_id else None
    evidence_rows = [session.get(StrengthProgressionEvidence, item) for item in (
        proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id,
    )]
    evidence: list[EvidenceReviewItem] = []
    valid_payload = True
    for row in evidence_rows:
        if row is None:
            valid_payload = False
            continue
        sets = _safe_sets(row.decisive_sets_json)
        if sets is None:
            valid_payload = False
            sets = ()
        evidence.append(EvidenceReviewItem(row.appearance_at, row.classification, row.prescribed_sets,
            row.target_reps, sets))
    actionable = proposal.status == "pending" and valid_payload and len(evidence) == 2
    return ProposalReviewItem(
        proposal.proposal_id, program.name if program else "Unavailable program",
        program_session.name if program_session else "Unavailable session",
        exercise.exercise_name if exercise else "Unavailable exercise", proposal.direction,
        proposal.current_weight_grams, proposal.suggested_weight_grams,
        policy.global_increment_grams, tuple(evidence), proposal.policy_version,
        proposal.prescription_fingerprint[:12], proposal.status, proposal.resolved_at, actionable,
        None if actionable else "evidence_unavailable",
    )


def list_progression_review(session: Session, *, now: datetime, history_limit: int = 50) -> ProgressionReviewPage:
    rows = (session.query(StrengthProgressionProposal)
        .order_by(StrengthProgressionProposal.created_at.desc(), StrengthProgressionProposal.proposal_id.desc())
        .limit(max(1, min(int(history_limit) + 50, 100))).all())
    pending, history = [], []
    for row in rows:
        item = _review_item(session, row)
        if row.status == "pending" and not item.actionable:
            transition_pending_proposal(session, row, status="stale", now=now)
            history.append(_review_item(session, row))
        elif row.status == "pending":
            pending.append(item)
        elif len(history) < max(1, min(int(history_limit), 50)):
            history.append(item)
    return ProgressionReviewPage(tuple(pending), tuple(history))


def _stale(session: Session, proposal: StrengthProgressionProposal, now: datetime) -> ProposalActionResult:
    if proposal.status == "pending":
        transition_pending_proposal(session, proposal, status="stale", now=now)
    return ProposalActionResult(ProgressionActionOutcome.STALE, proposal.proposal_id)


def _terminal(proposal: StrengthProgressionProposal, action: ProgressionAction,
              approved_weight_grams: int | None = None) -> ProposalActionResult | None:
    if proposal.status == "applied":
        if action == ProgressionAction.APPROVE and approved_weight_grams == proposal.approved_weight_grams:
            return ProposalActionResult(ProgressionActionOutcome.ALREADY_APPLIED, proposal.proposal_id)
        return ProposalActionResult(ProgressionActionOutcome.CONFLICT, proposal.proposal_id)
    if proposal.status == "rejected":
        if action == ProgressionAction.REJECT:
            return ProposalActionResult(ProgressionActionOutcome.ALREADY_REJECTED, proposal.proposal_id)
        return ProposalActionResult(ProgressionActionOutcome.CONFLICT, proposal.proposal_id)
    if proposal.status != "pending":
        return ProposalActionResult(ProgressionActionOutcome.CONFLICT, proposal.proposal_id)
    return None


def _revalidate(session: Session, proposal: StrengthProgressionProposal, *, now: datetime):
    """Reload every decisive row and derive the exact current proposal."""
    if proposal.current_pending_key is None:
        return None
    program = session.get(TrainingProgram, proposal.program_id) if proposal.program_id else None
    program_session = session.get(ProgramSession, proposal.program_session_id) if proposal.program_session_id else None
    exercise = session.get(SessionExercise, proposal.session_exercise_id) if proposal.session_exercise_id else None
    active_program_count = session.query(TrainingProgram).filter(
        TrainingProgram.active.is_(True), TrainingProgram.status == "active",
    ).count()
    if (program is None or active_program_count != 1 or not program.active or program.status != "active" or program_session is None
            or exercise is None or program_session.program_id != program.id or exercise.program_session_id != program_session.id
            or proposal.program_id_snapshot != program.id or proposal.program_session_id_snapshot != program_session.id
            or proposal.session_exercise_id_snapshot != exercise.id):
        return None
    try:
        policy = load_active_policy(session)
        prescription = _prescription(exercise, program.id, program_session.id)
        from coach.strength_progression import prescription_fingerprint
        current_fingerprint = prescription_fingerprint(prescription)
        current_weight = normalize_weight_grams(exercise.weight_kg)
    except (RuntimeError, ValueError):
        return None
    if (policy.policy_version != proposal.policy_version or current_fingerprint != proposal.prescription_fingerprint
            or current_weight != proposal.current_weight_grams
            or proposal.current_pending_key != pending_key(exercise.id, policy.policy_version, current_fingerprint)
            or proposal.direction not in {"increase", "decrease"}):
        return None
    decisive = [session.get(StrengthProgressionEvidence, key) for key in
                (proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id)]
    if any(row is None or row.session_exercise_id_snapshot != exercise.id or row.policy_version != policy.policy_version
           or row.prescription_fingerprint != current_fingerprint for row in decisive):
        return None
    current = load_current_evidence(session, session_exercise_id=exercise.id, policy_version=policy.policy_version,
                                    prescription_fingerprint=current_fingerprint)
    current_ids = {row.evidence_id for row in current}
    if any(row.evidence_id not in current_ids or row.activity_id is None
           or (session.get(StrengthProgressionEvidenceHead, (exercise.id, row.activity_id)) is None)
           or session.get(StrengthProgressionEvidenceHead, (exercise.id, row.activity_id)).current_evidence_id != row.evidence_id
           for row in decisive):
        return None
    try:
        records = [evidence_record(row) for row in current]
        streak = derive_streak(policy, records, session_exercise_id=exercise.id,
            prescription=current_fingerprint, as_of=now)
        calculated = calculate_proposal(policy, prescription, streak, records)
    except (ValueError, AttributeError):
        return None
    if (calculated.direction is None or calculated.direction.value != proposal.direction
            or calculated.suggested_weight_grams != proposal.suggested_weight_grams
            or len(calculated.decisive_evidence_ids) != 2):
        return None
    # A preserved pending row may receive newer decisive support. Refresh only
    # while pending; terminal audit rows are never rewritten.
    if tuple(calculated.decisive_evidence_ids) != (proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id):
        proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id = calculated.decisive_evidence_ids
        proposal.reason_codes_json = json.dumps([reason.value for reason in calculated.reason_codes], separators=(",", ":"))
    return policy, exercise, current


def _entered_weight(value: object) -> int | None:
    try:
        grams = normalize_weight_grams(value)
    except ValueError:
        return None
    return grams if grams <= _MAX_WEIGHT_GRAMS else None


def approve_progression_proposal(session: Session, proposal_id: str, *, entered_weight_kg: object,
                                 now: datetime) -> ProposalActionResult:
    grams = _entered_weight(entered_weight_kg)
    proposal = session.get(StrengthProgressionProposal, proposal_id)
    if proposal is None:
        return ProposalActionResult(ProgressionActionOutcome.NOT_FOUND)
    terminal = _terminal(proposal, ProgressionAction.APPROVE, grams)
    if terminal:
        return terminal
    if grams is None:
        return ProposalActionResult(ProgressionActionOutcome.INVALID_WEIGHT, proposal_id)
    state = _revalidate(session, proposal, now=now)
    if state is None:
        return _stale(session, proposal, now)
    _, exercise, _ = state
    if ((proposal.direction == "increase" and grams <= proposal.current_weight_grams)
            or (proposal.direction == "decrease" and not 0 < grams < proposal.current_weight_grams)):
        return ProposalActionResult(ProgressionActionOutcome.INVALID_WEIGHT, proposal_id)
    exercise.weight_kg = grams / 1000
    transition_pending_proposal(session, proposal, status="applied", now=now, approved_weight_grams=grams)
    stale_other_pending_proposals(session, session_exercise_id=exercise.id, except_proposal_id=proposal_id, now=now)
    return ProposalActionResult(ProgressionActionOutcome.APPLIED, proposal_id)


def reject_progression_proposal(session: Session, proposal_id: str, *, now: datetime) -> ProposalActionResult:
    proposal = session.get(StrengthProgressionProposal, proposal_id)
    if proposal is None:
        return ProposalActionResult(ProgressionActionOutcome.NOT_FOUND)
    terminal = _terminal(proposal, ProgressionAction.REJECT)
    if terminal:
        return terminal
    state = _revalidate(session, proposal, now=now)
    if state is None:
        return _stale(session, proposal, now)
    _, exercise, current = state
    if not current:
        return _stale(session, proposal, now)
    cutoff = max(current, key=lambda row: (row.appearance_at, row.evidence_id))
    append_rejection_boundary(session, proposal=proposal, cutoff=cutoff, now=now)
    transition_pending_proposal(session, proposal, status="rejected", now=now)
    reset_current_streak(session, session_exercise_id=exercise.id, policy_version=proposal.policy_version,
                         prescription_fingerprint=proposal.prescription_fingerprint)
    return ProposalActionResult(ProgressionActionOutcome.REJECTED, proposal_id)
