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
    calculate_proposal, canonical_json, derive_streak, normalize_weight_grams,
)
from coach.strength_progression_integration import _prescription
from coach.strength_progression_store import (
    append_rejection_boundary, evidence_record, load_active_policy, load_current_evidence,
    pending_key, reset_current_streak, stale_other_pending_proposals, claim_pending_proposal,
    transition_pending_proposal,
)
from db import (
    ProgramSession, SessionExercise, StrengthProgressionEvidence,
    StrengthProgressionEvidenceHead, StrengthProgressionPolicy, StrengthProgressionProposal, TrainingProgram,
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
class EvidenceSetReviewItem:
    set_index: int | None
    set_number: int | None
    set_type: str
    reps: int | None
    source_weight: str
    normalized_weight: str
    duration_seconds: int | None
    edited: bool
    excluded: str | None


@dataclass(frozen=True)
class EvidenceReviewItem:
    appearance_at: datetime
    classification: str
    prescribed_sets: int | None
    target_reps: int | None
    decisive_sets: tuple[EvidenceSetReviewItem, ...]


@dataclass(frozen=True)
class ProposalReviewItem:
    proposal_id: str
    program_name: str
    session_name: str
    exercise_name: str
    direction: str
    current_weight_grams: int
    suggested_weight_grams: int
    approved_weight_grams: int | None
    current_weight: str
    suggested_weight: str
    approved_weight: str
    global_increment_grams: int | None
    global_increment: str
    evidence: tuple[EvidenceReviewItem, ...]
    policy_version: str
    prescription_reference: str
    status: str
    resolved_at: datetime | None
    status_label: str
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


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_sets(raw: str) -> tuple[EvidenceSetReviewItem, ...] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    rows: list[EvidenceSetReviewItem] = []
    for row in parsed:
        if not isinstance(row, dict):
            return None
        source = row.get("weight_kg_source")
        grams = _safe_int(row.get("weight_grams"))
        excluded = row.get("excluded")
        if excluded not in {None, "rest", "warmup", "inferred_warmup"}:
            return None
        set_type = row.get("set_type")
        if set_type is not None and not isinstance(set_type, str):
            return None
        if source is not None and not isinstance(source, str):
            return None
        for key in ("set_index", "reps", "duration_seconds", "weight_grams"):
            if row.get(key) is not None and _safe_int(row.get(key)) is None:
                return None
        if not isinstance(row.get("edited", False), bool):
            return None
        rows.append(EvidenceSetReviewItem(
            set_index=_safe_int(row.get("set_index")),
            set_number=(_safe_int(row.get("set_index")) + 1) if _safe_int(row.get("set_index")) is not None else None,
            set_type=set_type or "Working set", reps=_safe_int(row.get("reps")),
            source_weight="Unavailable" if source is None else f"{source} kg",
            normalized_weight=format_weight_grams(grams),
            duration_seconds=_safe_int(row.get("duration_seconds")), edited=bool(row.get("edited")),
            excluded=excluded,
        ))
    return tuple(rows)


def _policy_increment(session: Session, version: str) -> int | None:
    row = session.get(StrengthProgressionPolicy, version)
    if row is None:
        return None
    values = (row.global_increment_grams, row.weight_quantum_grams, row.required_consecutive, row.evidence_window_days)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        return None
    if row.weight_quantum_grams != 250 or row.global_increment_grams % 250:
        return None
    return row.global_increment_grams


def _review_item(session: Session, proposal: StrengthProgressionProposal) -> ProposalReviewItem:
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
    increment = _policy_increment(session, proposal.policy_version)
    actionable = proposal.status == "pending" and valid_payload and len(evidence) == 2
    status_label = {"applied": "Applied", "rejected": "Rejected", "stale": "Stale", "superseded": "Superseded", "pending": "Pending"}.get(proposal.status, "Unavailable")
    return ProposalReviewItem(
        proposal.proposal_id, program.name if program else "Unavailable program",
        program_session.name if program_session else "Unavailable session",
        exercise.exercise_name if exercise else "Unavailable exercise", proposal.direction,
        proposal.current_weight_grams, proposal.suggested_weight_grams, proposal.approved_weight_grams,
        format_weight_grams(proposal.current_weight_grams), format_weight_grams(proposal.suggested_weight_grams),
        format_weight_grams(proposal.approved_weight_grams), increment, format_weight_grams(increment), tuple(evidence), proposal.policy_version,
        proposal.prescription_fingerprint[:12], proposal.status, proposal.resolved_at, status_label, actionable,
        None if actionable else "evidence_unavailable",
    )


def list_progression_review(session: Session, *, now: datetime, history_limit: int = 50) -> ProgressionReviewPage:
    """Build all current cards and separately capped immutable history."""
    rows = (session.query(StrengthProgressionProposal).filter_by(status="pending")
        .order_by(StrengthProgressionProposal.created_at.asc(), StrengthProgressionProposal.proposal_id.asc()).all())
    pending, newly_stale = [], []
    for row in rows:
        # POST and GET share the same decisive check; stale cards never render
        # as actionable merely because their persisted JSON happens to parse.
        if _revalidate(session, row, now=now) is None:
            _stale(session, row, now)
            newly_stale.append(_review_item(session, row))
        else:
            item = _review_item(session, row)
            if item.actionable:
                pending.append(item)
            else:
                _stale(session, row, now)
                newly_stale.append(_review_item(session, row))
    history_rows = (session.query(StrengthProgressionProposal)
        .filter(StrengthProgressionProposal.status != "pending")
        .order_by(StrengthProgressionProposal.resolved_at.desc(), StrengthProgressionProposal.proposal_id.desc())
        .limit(max(1, min(int(history_limit), 50))).all())
    already_listed = {item.proposal_id for item in newly_stale}
    history = newly_stale + [_review_item(session, row) for row in history_rows if row.proposal_id not in already_listed]
    return ProgressionReviewPage(tuple(pending), tuple(history[:max(1, min(int(history_limit), 50))]))


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
    current = load_current_evidence(session, session_exercise_id=exercise.id, policy_version=policy.policy_version,
                                    prescription_fingerprint=current_fingerprint)
    # The old proposal support is audit history, not a liveness condition: a
    # correction intentionally replaces an activity's evidence head.  Validate
    # every *current* row before deriving the fresh support instead.
    for row in current:
        if (row.session_exercise_id_snapshot != exercise.id or row.policy_version != policy.policy_version
                or row.prescription_fingerprint != current_fingerprint or row.activity_id is None):
            return None
        head = session.get(StrengthProgressionEvidenceHead, (exercise.id, row.activity_id))
        if head is None or head.current_evidence_id != row.evidence_id:
            return None
    current_ids = {row.evidence_id for row in current}
    try:
        records = [evidence_record(row) for row in current]
        streak = derive_streak(policy, records, session_exercise_id=exercise.id,
            prescription=current_fingerprint, as_of=now)
        calculated = calculate_proposal(policy, prescription, streak, records)
    except (ValueError, AttributeError):
        return None
    if (calculated.direction is None or calculated.direction.value != proposal.direction
            or calculated.suggested_weight_grams != proposal.suggested_weight_grams
            or len(calculated.decisive_evidence_ids) != 2
            or not set(calculated.decisive_evidence_ids).issubset(current_ids)):
        return None
    # A preserved pending row may receive newer decisive support. Refresh only
    # while pending; terminal audit rows are never rewritten.
    if tuple(calculated.decisive_evidence_ids) != (proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id):
        proposal.decisive_evidence_one_id, proposal.decisive_evidence_two_id = calculated.decisive_evidence_ids
        proposal.reason_codes_json = canonical_json([reason.value for reason in calculated.reason_codes])
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
    # Persist a harmless-correction support refresh before comparing all of its
    # values in the atomic claim below.
    session.flush()
    if not claim_pending_proposal(session, proposal, status="applied", now=now, approved_weight_grams=grams):
        latest = session.get(StrengthProgressionProposal, proposal_id)
        if latest is not None:
            return _terminal(latest, ProgressionAction.APPROVE, grams) or ProposalActionResult(ProgressionActionOutcome.CONFLICT, proposal_id)
        return ProposalActionResult(ProgressionActionOutcome.NOT_FOUND)
    # Claim succeeds before this sole permitted template mutation.
    exercise.weight_kg = grams / 1000
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
    session.flush()
    if not claim_pending_proposal(session, proposal, status="rejected", now=now):
        latest = session.get(StrengthProgressionProposal, proposal_id)
        if latest is not None:
            return _terminal(latest, ProgressionAction.REJECT) or ProposalActionResult(ProgressionActionOutcome.CONFLICT, proposal_id)
        return ProposalActionResult(ProgressionActionOutcome.NOT_FOUND)
    append_rejection_boundary(session, proposal=proposal, cutoff=cutoff, now=now)
    reset_current_streak(session, session_exercise_id=exercise.id, policy_version=proposal.policy_version,
                         prescription_fingerprint=proposal.prescription_fingerprint)
    return ProposalActionResult(ProgressionActionOutcome.REJECTED, proposal_id)
