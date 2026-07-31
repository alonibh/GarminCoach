"""Bounded local runtime integration for Phase 4B2 strength progression.

This module deliberately owns only ORM-to-domain mapping and persistence
orchestration. It never imports Garmin, HTTP, notifications, calendars, or
advisory/recovery code; callers retain transaction ownership.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Collection

from sqlalchemy.orm import Session

from coach.strength_progression import (
    AppearanceClassification, AppearanceClassificationResult, AppearanceInput,
    CompletedExerciseGroup, ExercisePrescription, ObservedSet, ReasonCode,
    canonical_json, calculate_proposal, classify_appearance, derive_streak,
    fingerprint, match_exercise_groups, prescription_fingerprint,
)
from coach.strength_progression_store import (
    append_evidence, create_or_replace_pending_proposal, evidence_record,
    load_active_policy, load_current_evidence, mark_pending_proposal_stale,
    stale_pending_proposals_for_exercises, stale_pending_proposals_for_program,
    upsert_streak,
)
from db import (
    Activity, ActivityProgramMatch, ExerciseSet, ProgramSession, SessionExercise,
    StrengthProgressionEvidence, StrengthProgressionEvidenceHead,
    StrengthProgressionProposal, SyncState, TrainingProgram,
)

logger = logging.getLogger(__name__)
_JOURNAL_PREFIX = "strength_progression_recalc_activity:"


class RecalculationCause(str, Enum):
    STRENGTH_SETS_RESOLVED = "strength_sets_resolved"
    ACTIVITY_PROGRAM_MATCH_CREATED = "activity_program_match_created"
    MANUAL_SET_CORRECTED = "manual_set_corrected"
    MANUAL_SET_CORRECTION_REVERTED = "manual_set_correction_reverted"
    RETRY = "retry"


class InvalidationCause(str, Enum):
    TEMPLATE_CHANGED = "template_changed"
    TEMPLATE_REPLACED = "template_replaced"
    EXERCISE_DELETED = "exercise_deleted"
    EXERCISE_BECAME_INELIGIBLE = "exercise_became_ineligible"
    PROGRAM_DEACTIVATED = "program_deactivated"
    ACTIVE_PROGRAM_REPLACED = "active_program_replaced"


@dataclass(frozen=True)
class MaterialProposalChange:
    """An immutable fact emitted only for a new material pending proposal."""
    proposal_id: str
    program_id: int
    program_session_id: int
    session_exercise_id: int
    policy_version: str
    prescription_fingerprint: str
    direction: str
    current_weight_grams: int
    suggested_weight_grams: int
    material_fingerprint: str


@dataclass(frozen=True)
class RecalculationReport:
    activity_id: int
    expected_noop: str | None = None
    evidence_created: int = 0
    evidence_reused: int = 0
    heads_moved: int = 0
    streaks_updated: int = 0
    proposals_created: int = 0
    proposals_preserved: int = 0
    proposals_superseded: int = 0
    proposals_staled: int = 0
    dirty_key_cleared: bool = False
    dirty_key_retained: bool = False
    error: str | None = None
    boundary_id: str | None = None
    material_proposal_changes: tuple[MaterialProposalChange, ...] = ()


@dataclass(frozen=True)
class BatchRecalculationReport:
    processed: int
    reports: tuple[RecalculationReport, ...]
    malformed_keys: int = 0
    boundary_id: str | None = None
    material_proposal_changes: tuple[MaterialProposalChange, ...] = ()


@dataclass(frozen=True)
class InvalidationReport:
    cause: InvalidationCause
    proposals_staled: int


def _journal_key(activity_id: int) -> str:
    return f"{_JOURNAL_PREFIX}{activity_id}"


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="microseconds")


def _activity_boundary_id(session: Session, activity_id: int, cause: RecalculationCause) -> str:
    """Stable across retries of one retained journal request, never random."""
    causes: set[str] | None = None
    first_requested_at: str | None = None
    row = session.get(SyncState, _journal_key(activity_id))
    if row is not None:
        try:
            payload = json.loads(row.value or "")
            if isinstance(payload, dict) and isinstance(payload.get("causes"), list):
                # The persisted request is the boundary authority. RETRY is an
                # execution detail and must not alter its identity.
                causes = {str(item) for item in payload["causes"] if isinstance(item, str)}
                first_requested_at = str(payload.get("first_requested_at") or "") or None
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if causes is None:
        # Direct callers may have no journal. A retry alone carries no new
        # authoritative request identity, so keep that fallback deterministic.
        causes = set() if cause == RecalculationCause.RETRY else {cause.value}
    return fingerprint({"kind": "strength_progression_activity_boundary", "activity_id": activity_id,
                        "causes": sorted(causes), "first_requested_at": first_requested_at,
                        "schema": "v1"})


def _batch_boundary_id(reports: Collection[RecalculationReport]) -> str | None:
    components = sorted({report.boundary_id for report in reports if report.boundary_id})
    return fingerprint({"kind": "strength_progression_batch_boundary", "components": components}) if components else None


def _material_change(row: StrengthProgressionProposal) -> MaterialProposalChange | None:
    if (row.program_id_snapshot is None or row.program_session_id_snapshot is None
            or row.status != "pending"):
        return None
    material = fingerprint({"proposal_id": row.proposal_id, "policy_version": row.policy_version,
        "prescription_fingerprint": row.prescription_fingerprint,
        "session_exercise_id": row.session_exercise_id_snapshot, "direction": row.direction,
        "current_weight_grams": row.current_weight_grams,
        "suggested_weight_grams": row.suggested_weight_grams})
    return MaterialProposalChange(row.proposal_id, row.program_id_snapshot,
        row.program_session_id_snapshot, row.session_exercise_id_snapshot, row.policy_version,
        row.prescription_fingerprint, row.direction, row.current_weight_grams,
        row.suggested_weight_grams, material)


def request_activity_recalculation(
    session: Session, activity_id: int, *, cause: RecalculationCause,
) -> None:
    """Merge one bounded local dirty-work request; never commits."""
    key = _journal_key(activity_id)
    row = session.get(SyncState, key)
    timestamp = _now()
    causes: set[str] = {cause.value}
    first = timestamp
    if row is not None and row.value:
        try:
            saved = json.loads(row.value)
            if isinstance(saved, dict) and isinstance(saved.get("causes"), list):
                causes.update(str(item) for item in saved["causes"] if isinstance(item, str))
                first = str(saved.get("first_requested_at") or timestamp)
            else:
                # Do not overwrite a corrupt retry record: processing will retain
                # it and surface a sanitized result.
                return
        except (TypeError, ValueError):
            return
    payload = canonical_json({
        "causes": sorted(causes), "first_requested_at": first,
        "most_recent_requested_at": timestamp,
    })
    if row is None:
        session.add(SyncState(key=key, value=payload))
    else:
        row.value = payload


def _clear_request(session: Session, activity_id: int) -> None:
    row = session.get(SyncState, _journal_key(activity_id))
    if row is not None:
        session.delete(row)


def _is_strength(activity: Activity) -> bool:
    value = (activity.activity_type or "").casefold()
    return "strength" in value or "weight" in value


def _prescription(row: SessionExercise, program_id: int, program_session_id: int) -> ExercisePrescription:
    return ExercisePrescription(
        program_id=program_id, program_session_id=program_session_id,
        session_exercise_id=row.id, exercise_name=row.exercise_name,
        exercise_key=row.exercise_key, garmin_category=row.garmin_category,
        garmin_name=row.garmin_name, is_generic=bool(row.is_generic),
        prescribed_sets=row.sets, target_reps=row.reps,
        template_weight_kg=row.weight_kg, duration_seconds=row.duration_seconds,
        bodyweight=row.weight_kg is None, warmup_enabled=bool(row.warmup_enabled),
        warmup_reps=row.warmup_reps, warmup_duration_seconds=row.warmup_duration_seconds,
        warmup_weight_kg=row.warmup_weight_kg, order_index=row.order_index,
    )


def prescription_for_session_exercise(
    row: SessionExercise, program_id: int, program_session_id: int,
) -> str:
    """Expose the canonical prescription version for editor change detection."""
    return prescription_fingerprint(_prescription(row, program_id, program_session_id))


def _identity(value: str | None) -> str:
    return "" if not isinstance(value, str) else "".join(char for char in value.upper() if char.isalnum())


def _groups(session: Session, activity_id: int) -> tuple[CompletedExerciseGroup, ...]:
    """Create chronological, contiguous identity blocks without fuzzy routing."""
    rows = session.query(ExerciseSet).filter_by(activity_id=activity_id).order_by(ExerciseSet.set_index, ExerciseSet.id).all()
    blocks: list[dict] = []
    current: dict | None = None
    for row in rows:
        category, name = _identity(row.exercise_category), _identity(row.exercise_name)
        observed = ObservedSet(row.set_index, row.set_type, row.reps, row.weight_kg, row.duration_s, bool(row.edited))
        is_rest = _identity(row.set_type) in {"REST", "RESTSET", "RECOVERY"}
        if is_rest and not category and not name and current is not None and current["identified"]:
            current["sets"].append(observed)
            continue
        identity = (category, name)
        if current is None or current["identity"] != identity or (not is_rest and not current["identified"]):
            current = {"identity": identity, "identified": bool(category or name), "sets": [observed], "order": len(blocks)}
            blocks.append(current)
        else:
            current["sets"].append(observed)
    result: list[CompletedExerciseGroup] = []
    for block in blocks:
        category, name = block["identity"]
        first = block["sets"][0].set_index
        result.append(CompletedExerciseGroup(
            group_id=fingerprint({"activity_id": activity_id, "first_set_index": first, "category": category, "name": name}),
            garmin_category=category or None, garmin_name=name or None,
            exercise_key=name or None, order_index=block["order"],
            sets=tuple(block["sets"]), is_generic=not block["identified"],
        ))
    return tuple(result)


def _source_fingerprint(activity: Activity, match: ActivityProgramMatch, complete: bool,
                        groups: tuple[CompletedExerciseGroup, ...], prescription: ExercisePrescription) -> str:
    return fingerprint({
        "activity_id": activity.id, "activity_start": activity.start_time,
        "match_id": match.id, "match_method": match.match_method,
        "match_policy": match.policy_version, "strength_payload_complete": complete,
        "groups": [{"id": item.group_id, "category": item.garmin_category, "name": item.garmin_name,
                    "order": item.order_index, "generic": item.is_generic,
                    "sets": [{"index": row.set_index, "type": row.set_type, "reps": row.reps,
                              "weight": None if row.weight_kg is None else str(row.weight_kg),
                              "duration": row.duration_seconds, "edited": row.edited} for row in item.sets]}
                   for item in groups],
        "prescription": prescription_fingerprint(prescription),
    })


def _unscorable(reason: ReasonCode, prescription: ExercisePrescription) -> AppearanceClassificationResult:
    from coach.strength_progression import normalize_weight_grams
    try:
        weight = normalize_weight_grams(prescription.template_weight_kg)
    except ValueError:
        weight = None
    return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, weight, None, (), (reason,))


def _process(session: Session, activity_id: int, *, boundary_id: str | None = None) -> RecalculationReport:
    activity = session.get(Activity, activity_id)
    if activity is None:
        return RecalculationReport(activity_id, expected_noop="no_activity", boundary_id=boundary_id)
    if not _is_strength(activity):
        return RecalculationReport(activity_id, expected_noop="non_strength_activity", boundary_id=boundary_id)
    matches = session.query(ActivityProgramMatch).filter_by(activity_id=activity_id).all()
    if len(matches) != 1:
        return RecalculationReport(activity_id, expected_noop="no_confident_match", boundary_id=boundary_id)
    match = matches[0]
    program = session.get(TrainingProgram, match.program_id)
    program_session = session.get(ProgramSession, match.program_session_id)
    if (program is None or not program.active or program.status != "active" or
            program_session is None or program_session.program_id != program.id):
        return RecalculationReport(activity_id, expected_noop="inactive_or_mismatched_program", boundary_id=boundary_id)
    rows = session.query(SessionExercise).filter_by(program_session_id=program_session.id).order_by(SessionExercise.order_index, SessionExercise.id).all()
    if not rows:
        return RecalculationReport(activity_id, expected_noop="no_session_exercises", boundary_id=boundary_id)
    policy = load_active_policy(session)
    complete = session.get(SyncState, f"activity_strength_sets_checked:{activity_id}")
    payload_complete = bool(complete and complete.value == "complete")
    groups = _groups(session, activity_id)
    prescriptions = tuple(_prescription(row, program.id, program_session.id) for row in rows)
    matched = match_exercise_groups(groups, prescriptions)
    by_exercise: dict[int, CompletedExerciseGroup] = {}
    ambiguous: set[int] = set()
    group_by_id = {item.group_id: item for item in groups}
    for outcome in matched:
        if outcome.matched and outcome.session_exercise_id is not None and outcome.group_id:
            by_exercise[outcome.session_exercise_id] = group_by_id[outcome.group_id]
        elif outcome.reason_codes and outcome.reason_codes[0] == ReasonCode.AMBIGUOUS_MATCH:
            # A failed group cannot be guessed; each unmatched prescription is
            # explicitly unscorable below.
            ambiguous.update(item.session_exercise_id for item in prescriptions)
    created = reused = heads = streaks = proposal_created = preserved = superseded = staled = 0
    material_changes: list[MaterialProposalChange] = []
    appearance_at = activity.start_time or match.matched_at
    for prescription in prescriptions:
        # Ineligible rows have no normal evidence and cannot retain a current proposal.
        preliminary = classify_appearance(AppearanceInput(prescription, None, payload_complete, appearance_at))
        if preliminary.reason_codes and preliminary.reason_codes[0].value.startswith("ineligible"):
            staled += stale_pending_proposals_for_exercises(session, [prescription.session_exercise_id])
            continue
        group = by_exercise.get(prescription.session_exercise_id)
        if not payload_complete:
            result = _unscorable(ReasonCode.INCOMPLETE_PAYLOAD, prescription)
        elif group is None:
            result = _unscorable(ReasonCode.AMBIGUOUS_MATCH if prescription.session_exercise_id in ambiguous else ReasonCode.NO_MATCH, prescription)
        else:
            result = classify_appearance(AppearanceInput(prescription, group, payload_complete, appearance_at))
        source = _source_fingerprint(activity, match, payload_complete, groups, prescription)
        old_head = session.get(StrengthProgressionEvidenceHead, (prescription.session_exercise_id, activity.id))
        old_id = old_head.current_evidence_id if old_head else None
        existing = session.query(StrengthProgressionEvidence).filter_by(
            idempotency_key=fingerprint({"session_exercise_id": prescription.session_exercise_id,
                "activity_id": activity.id, "policy_version": policy.policy_version,
                "prescription_fingerprint": prescription_fingerprint(prescription),
                "source_fingerprint": source})
        ).one_or_none()
        evidence = append_evidence(session, session_exercise_id=prescription.session_exercise_id,
            activity_id=activity.id, policy_version=policy.policy_version,
            prescription_fingerprint=prescription_fingerprint(prescription), source_fingerprint=source,
            appearance_at=appearance_at, result=result, program_id=program.id,
            program_session_id=program_session.id, activity_program_match_id=match.id,
            prescribed_sets=prescription.prescribed_sets, target_reps=prescription.target_reps)
        if existing is None:
            created += 1
        else:
            reused += 1
        if old_id != evidence.evidence_id:
            heads += 1
        current = load_current_evidence(session, session_exercise_id=prescription.session_exercise_id,
            policy_version=policy.policy_version, prescription_fingerprint=prescription_fingerprint(prescription))
        records = [evidence_record(row) for row in current]
        streak = derive_streak(policy, records, session_exercise_id=prescription.session_exercise_id,
            prescription=prescription_fingerprint(prescription), as_of=appearance_at)
        upsert_streak(session, session_exercise_id=prescription.session_exercise_id,
            policy_version=policy.policy_version, prescription_fingerprint=prescription_fingerprint(prescription), result=streak)
        streaks += 1
        proposal = calculate_proposal(policy, prescription, streak, records)
        before = session.query(StrengthProgressionProposal).filter_by(
            current_pending_key=f"{prescription.session_exercise_id}:{policy.policy_version}:{prescription_fingerprint(prescription)}").one_or_none()
        proposal_row = create_or_replace_pending_proposal(session, session_exercise_id=prescription.session_exercise_id,
            proposal=proposal, program_id=program.id, program_session_id=program_session.id)
        if proposal_row is None:
            if mark_pending_proposal_stale(session, session_exercise_id=prescription.session_exercise_id,
                    policy_version=policy.policy_version, prescription_fingerprint=prescription_fingerprint(prescription)):
                staled += 1
        elif before is None:
            proposal_created += 1
        elif before.proposal_id == proposal_row.proposal_id:
            preserved += 1
        else:
            superseded += 1
        if proposal_row is not None and (before is None or before.proposal_id != proposal_row.proposal_id):
            material = _material_change(proposal_row)
            if material is not None:
                material_changes.append(material)
    return RecalculationReport(activity_id, evidence_created=created, evidence_reused=reused,
        heads_moved=heads, streaks_updated=streaks, proposals_created=proposal_created,
        proposals_preserved=preserved, proposals_superseded=superseded, proposals_staled=staled,
        boundary_id=boundary_id, material_proposal_changes=tuple(material_changes))


def process_activity_recalculation(session: Session, activity_id: int, *, cause: RecalculationCause) -> RecalculationReport:
    """Process one requested activity in a savepoint; retain its key on failures."""
    boundary_id = _activity_boundary_id(session, activity_id, cause)
    try:
        with session.begin_nested():
            report = _process(session, activity_id, boundary_id=boundary_id)
            _clear_request(session, activity_id)
        return RecalculationReport(**{**report.__dict__, "dirty_key_cleared": True})
    except Exception:
        logger.exception("strength progression recalculation failed for activity_id=%s", activity_id)
        return RecalculationReport(activity_id, dirty_key_retained=True, error="recalculation_failed",
                                   boundary_id=boundary_id)


def process_pending_activity_recalculations(session: Session, *, limit: int = 50,
                                            activity_ids: Collection[int] | None = None) -> BatchRecalculationReport:
    if limit < 1:
        return BatchRecalculationReport(0, ())
    rows = session.query(SyncState).filter(SyncState.key.like(f"{_JOURNAL_PREFIX}%")).order_by(SyncState.key).all()
    wanted = {int(item) for item in activity_ids} if activity_ids is not None else None
    reports: list[RecalculationReport] = []
    malformed = 0
    for row in rows:
        if len(reports) >= limit:
            break
        try:
            activity_id = int(row.key.removeprefix(_JOURNAL_PREFIX))
            value = json.loads(row.value or "")
            if not isinstance(value, dict) or not isinstance(value.get("causes"), list):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            malformed += 1
            reports.append(RecalculationReport(-1, dirty_key_retained=True, error="malformed_journal"))
            continue
        if wanted is not None and activity_id not in wanted:
            continue
        reports.append(process_activity_recalculation(session, activity_id, cause=RecalculationCause.RETRY))
    changes = tuple(change for report in reports for change in report.material_proposal_changes)
    return BatchRecalculationReport(len(reports), tuple(reports), malformed,
        _batch_boundary_id(reports), changes)


def invalidate_session_exercises(session: Session, session_exercise_ids: Collection[int], *, cause: InvalidationCause) -> InvalidationReport:
    return InvalidationReport(cause, stale_pending_proposals_for_exercises(session, session_exercise_ids))


def invalidate_program_proposals(session: Session, program_id: int, *, cause: InvalidationCause) -> InvalidationReport:
    return InvalidationReport(cause, stale_pending_proposals_for_program(session, program_id))
