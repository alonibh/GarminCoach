"""Pure, deterministic Phase 4B1 strength-progression rules.

This module intentionally knows nothing about SQLAlchemy, Garmin clients, web
routes, telemetry, calendars, or subjective/biometric inputs.  Its public
inputs and results are frozen dataclasses so a later integration can make its
database boundary explicit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import hashlib
import json
import math
from typing import Any, Iterable


STRENGTH_PROGRESSION_POLICY_VERSION = "strength-progression-v1"
WEIGHT_QUANTUM_GRAMS = 250


class AppearanceClassification(str, Enum):
    INCREASE_QUALIFIED = "increase_qualified"
    MATERIALLY_UNDER_TARGET = "materially_under_target"
    NEUTRAL = "neutral"
    UNSCORABLE = "unscorable"


class ProposalDirection(str, Enum):
    INCREASE = "increase"
    DECREASE = "decrease"


class ReasonCode(str, Enum):
    INELIGIBLE_WEIGHT = "ineligible_weight"
    INELIGIBLE_BODYWEIGHT = "ineligible_bodyweight"
    INELIGIBLE_DURATION = "ineligible_duration"
    INELIGIBLE_GENERIC = "ineligible_generic"
    INELIGIBLE_PRESCRIPTION = "ineligible_prescription"
    AMBIGUOUS_MATCH = "ambiguous_match"
    NO_MATCH = "no_match"
    AMBIGUOUS_WARMUP = "ambiguous_warmup"
    INCOMPLETE_PAYLOAD = "incomplete_payload"
    MISSING_WORKING_SET = "missing_working_set"
    INVALID_REPS = "invalid_reps"
    INVALID_WEIGHT = "invalid_weight"
    BELOW_TEMPLATE_WEIGHT = "below_template_weight"
    MISSED_TARGET_REPS = "missed_target_reps"
    QUALIFIED = "qualified"
    NEUTRAL_PERFORMANCE = "neutral_performance"
    STREAK_NOT_READY = "streak_not_ready"
    DECREASE_FLOOR = "decrease_floor"
    EVIDENCE_MISMATCH = "evidence_mismatch"


@dataclass(frozen=True)
class ProgressionPolicy:
    policy_version: str = STRENGTH_PROGRESSION_POLICY_VERSION
    global_increment_grams: int = 2500
    weight_quantum_grams: int = WEIGHT_QUANTUM_GRAMS
    required_consecutive: int = 2
    evidence_window_days: int = 35


@dataclass(frozen=True)
class ExercisePrescription:
    program_id: int | None
    program_session_id: int | None
    session_exercise_id: int
    exercise_name: str
    exercise_key: str
    garmin_category: str | None
    garmin_name: str | None
    is_generic: bool
    prescribed_sets: int | None
    target_reps: int | None
    template_weight_kg: object | None
    duration_seconds: int | None = None
    bodyweight: bool = False
    warmup_enabled: bool = False
    warmup_reps: int | None = None
    warmup_duration_seconds: int | None = None
    warmup_weight_kg: object | None = None
    order_index: int = 0


@dataclass(frozen=True)
class ObservedSet:
    set_index: int
    set_type: str | None = None
    reps: int | None = None
    weight_kg: object | None = None
    duration_seconds: int | None = None
    edited: bool = False


@dataclass(frozen=True)
class CompletedExerciseGroup:
    group_id: str
    garmin_category: str | None
    garmin_name: str | None
    exercise_key: str | None
    order_index: int
    sets: tuple[ObservedSet, ...]
    is_generic: bool = False


@dataclass(frozen=True)
class MatchResult:
    session_exercise_id: int | None
    group_id: str | None
    matched: bool
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True)
class PreparedSetsResult:
    working_sets: tuple[ObservedSet, ...]
    decisive_sets: tuple[dict[str, Any], ...]
    reason_codes: tuple[ReasonCode, ...]
    unscorable: bool = False


@dataclass(frozen=True)
class AppearanceInput:
    prescription: ExercisePrescription
    group: CompletedExerciseGroup | None
    strength_payload_complete: bool
    appearance_at: datetime


@dataclass(frozen=True)
class AppearanceClassificationResult:
    classification: AppearanceClassification
    current_weight_grams: int | None
    candidate_weight_grams: int | None
    decisive_sets: tuple[dict[str, Any], ...]
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    session_exercise_id: int
    policy_version: str
    prescription_fingerprint: str
    appearance_at: datetime
    classification: AppearanceClassification
    candidate_weight_grams: int | None = None


@dataclass(frozen=True)
class StreakResult:
    increase_count: int
    decrease_count: int
    last_classification: AppearanceClassification | None
    last_relevant_appearance_at: datetime | None
    decisive_evidence_ids: tuple[str, ...]
    expired: bool = False


@dataclass(frozen=True)
class ProposalResult:
    direction: ProposalDirection | None
    current_weight_grams: int
    suggested_weight_grams: int | None
    decisive_evidence_ids: tuple[str, ...]
    policy_version: str
    prescription_fingerprint: str
    idempotency_key: str | None
    reason_codes: tuple[ReasonCode, ...]


def _canonical(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            return item.isoformat(timespec="microseconds")
        if isinstance(item, Decimal):
            return format(item, "f")
        raise TypeError(f"not canonical JSON: {type(item)!r}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)


def canonical_json(value: Any) -> str:
    """Return stable JSON for persisted audit payloads and fingerprints."""
    return _canonical(value)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalize_weight_grams(value_kg: object, *, positive: bool = True) -> int:
    """Normalize kg to the nearest quarter-kilogram using decimal half-up."""
    if isinstance(value_kg, bool) or value_kg is None:
        raise ValueError("weight must be a finite number")
    try:
        value = Decimal(str(value_kg))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("weight must be a finite number") from exc
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError("weight must be positive and finite")
    grams = value * Decimal("1000")
    quantum = Decimal(WEIGHT_QUANTUM_GRAMS)
    normalized = int((grams / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum)
    if positive and normalized <= 0:
        raise ValueError("weight normalizes to a non-positive value")
    return normalized


def _identity(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value.upper() if char.isalnum())


def prescription_fingerprint(prescription: ExercisePrescription) -> str:
    try:
        weight = normalize_weight_grams(prescription.template_weight_kg)
    except ValueError:
        weight = None
    try:
        warmup_weight = normalize_weight_grams(prescription.warmup_weight_kg)
    except ValueError:
        warmup_weight = None
    return fingerprint({
        "program_id": prescription.program_id,
        "program_session_id": prescription.program_session_id,
        "session_exercise_id": prescription.session_exercise_id,
        "exercise_name": prescription.exercise_name,
        "exercise_key": prescription.exercise_key,
        "garmin_category": prescription.garmin_category,
        "garmin_name": prescription.garmin_name,
        "is_generic": prescription.is_generic,
        "prescribed_sets": prescription.prescribed_sets,
        "target_reps": prescription.target_reps,
        "template_weight_grams": weight,
        "duration_seconds": prescription.duration_seconds,
        "bodyweight": prescription.bodyweight,
        "warmup_enabled": prescription.warmup_enabled,
        "warmup_reps": prescription.warmup_reps,
        "warmup_duration_seconds": prescription.warmup_duration_seconds,
        "warmup_weight_grams": warmup_weight,
        "order_index": prescription.order_index,
    })


def match_exercise_groups(
    groups: Iterable[CompletedExerciseGroup], candidates: Iterable[ExercisePrescription]
) -> tuple[MatchResult, ...]:
    """Map groups only where identity is strong; order only breaks strong ties."""
    candidates = tuple(candidates)
    results: list[MatchResult] = []
    for group in groups:
        if group.is_generic:
            results.append(MatchResult(None, group.group_id, False, (ReasonCode.AMBIGUOUS_MATCH,)))
            continue
        category, name = _identity(group.garmin_category), _identity(group.garmin_name)
        exact: list[ExercisePrescription] = []
        if category and name:
            exact = [candidate for candidate in candidates if not candidate.is_generic
                     and _identity(candidate.garmin_category) == category
                     and _identity(candidate.garmin_name) == name]
        if len(exact) > 1:
            ordered = [candidate for candidate in exact if candidate.order_index == group.order_index]
            exact = ordered if len(ordered) == 1 else []
        if len(exact) == 1:
            results.append(MatchResult(exact[0].session_exercise_id, group.group_id, True, ()))
            continue
        # Key fallback is intentionally available only when strong Garmin IDs
        # are unavailable on the group, and must remain unique.
        key = _identity(group.exercise_key)
        if not (category and name) and key:
            keyed = [candidate for candidate in candidates if not candidate.is_generic and _identity(candidate.exercise_key) == key]
            if len(keyed) == 1:
                results.append(MatchResult(keyed[0].session_exercise_id, group.group_id, True, ()))
                continue
            results.append(MatchResult(None, group.group_id, False, (ReasonCode.AMBIGUOUS_MATCH if keyed else ReasonCode.NO_MATCH,)))
            continue
        results.append(MatchResult(None, group.group_id, False, (ReasonCode.AMBIGUOUS_MATCH if category or name else ReasonCode.NO_MATCH,)))
    # A candidate cannot silently be assigned to two completed groups.  Turn
    # both assignments into explicit ambiguity instead of retaining whichever
    # happened to be iterated first.
    counts: dict[int, int] = {}
    for result in results:
        if result.matched and result.session_exercise_id is not None:
            counts[result.session_exercise_id] = counts.get(result.session_exercise_id, 0) + 1
    return tuple(
        MatchResult(None, result.group_id, False, (ReasonCode.AMBIGUOUS_MATCH,))
        if result.matched and counts[result.session_exercise_id] > 1 else result
        for result in results
    )


_REST_TYPES = {"REST", "RESTSET", "RECOVERY"}
_WARMUP_TYPES = {"WARMUP", "WARMUPSET", "WARMUPSET"}


def _set_payload(item: ObservedSet, *, excluded: str | None = None) -> dict[str, Any]:
    payload = {
        "set_index": item.set_index, "set_type": item.set_type or "", "reps": item.reps,
        "weight_kg_source": None if item.weight_kg is None else str(item.weight_kg),
        "duration_seconds": item.duration_seconds, "edited": item.edited,
    }
    try:
        payload["weight_grams"] = normalize_weight_grams(item.weight_kg)
    except ValueError:
        payload["weight_grams"] = None
    if excluded:
        payload["excluded"] = excluded
    return payload


def prepare_working_sets(prescription: ExercisePrescription, group: CompletedExerciseGroup) -> PreparedSetsResult:
    ordered = sorted(group.sets, key=lambda item: item.set_index)
    working: list[ObservedSet] = []
    payloads: list[dict[str, Any]] = []
    explicit_warmup = False
    for item in ordered:
        set_type = _identity(item.set_type)
        if set_type in _REST_TYPES:
            payloads.append(_set_payload(item, excluded="rest"))
        elif set_type in _WARMUP_TYPES:
            explicit_warmup = True
            payloads.append(_set_payload(item, excluded="warmup"))
        else:
            working.append(item)
    if prescription.warmup_enabled and not explicit_warmup and working:
        first = working[0]
        has_template_measure = prescription.warmup_reps is not None or prescription.warmup_duration_seconds is not None
        matches_reps = prescription.warmup_reps is None or first.reps == prescription.warmup_reps
        matches_duration = prescription.warmup_duration_seconds is None or first.duration_seconds == prescription.warmup_duration_seconds
        matches_measure = has_template_measure and matches_reps and matches_duration
        try:
            leading_weight = normalize_weight_grams(first.weight_kg)
            template_weight = normalize_weight_grams(prescription.template_weight_kg)
        except ValueError:
            return PreparedSetsResult((), tuple(payloads), (ReasonCode.INVALID_WEIGHT,), True)
        # A leading set at working weight (or above it) is deterministically a
        # working attempt, even if a malformed template happens to describe a
        # matching "warm-up" at that same/heavier weight.
        if leading_weight >= template_weight:
            pass
        else:
            try:
                warmup_weight = normalize_weight_grams(prescription.warmup_weight_kg)
            except ValueError:
                warmup_weight = None
            if matches_measure and leading_weight == warmup_weight:
                working.pop(0)
                payloads.append(_set_payload(first, excluded="inferred_warmup"))
            else:
                # A sub-template leading set that failed the exact warm-up match
                # cannot safely be assigned as either a warm-up or a working set.
                # It must not manufacture decrease evidence from a modified warm-up.
                return PreparedSetsResult((), tuple(payloads), (ReasonCode.AMBIGUOUS_WARMUP,), True)
    selected = tuple(working[: prescription.prescribed_sets or 0])
    payloads.extend(_set_payload(item) for item in selected)
    return PreparedSetsResult(selected, tuple(sorted(payloads, key=lambda item: item["set_index"])), ())


def classify_appearance(appearance: AppearanceInput) -> AppearanceClassificationResult:
    prescription = appearance.prescription
    if prescription.is_generic:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, None, None, (), (ReasonCode.INELIGIBLE_GENERIC,))
    if prescription.bodyweight:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, None, None, (), (ReasonCode.INELIGIBLE_BODYWEIGHT,))
    if prescription.duration_seconds is not None:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, None, None, (), (ReasonCode.INELIGIBLE_DURATION,))
    if not prescription.prescribed_sets or prescription.prescribed_sets <= 0 or not prescription.target_reps or prescription.target_reps <= 0:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, None, None, (), (ReasonCode.INELIGIBLE_PRESCRIPTION,))
    try:
        current = normalize_weight_grams(prescription.template_weight_kg)
    except ValueError:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, None, None, (), (ReasonCode.INELIGIBLE_WEIGHT,))
    if appearance.group is None or appearance.group.is_generic:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, current, None, (), (ReasonCode.AMBIGUOUS_MATCH,))
    prepared = prepare_working_sets(prescription, appearance.group)
    payload = prepared.decisive_sets
    if prepared.unscorable:
        return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, current, None, payload, prepared.reason_codes)
    attempts = prepared.working_sets
    if len(attempts) < prescription.prescribed_sets:
        reason = ReasonCode.MISSING_WORKING_SET if appearance.strength_payload_complete else ReasonCode.INCOMPLETE_PAYLOAD
        classification = AppearanceClassification.MATERIALLY_UNDER_TARGET if appearance.strength_payload_complete else AppearanceClassification.UNSCORABLE
        return AppearanceClassificationResult(classification, current, None, payload, (reason,))
    weights: list[int] = []
    for attempt in attempts:
        if not isinstance(attempt.reps, int) or isinstance(attempt.reps, bool) or attempt.reps <= 0:
            return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, current, None, payload, (ReasonCode.INVALID_REPS,))
        try:
            weights.append(normalize_weight_grams(attempt.weight_kg))
        except ValueError:
            return AppearanceClassificationResult(AppearanceClassification.UNSCORABLE, current, None, payload, (ReasonCode.INVALID_WEIGHT,))
    if all(item.reps >= prescription.target_reps and weight >= current for item, weight in zip(attempts, weights)):
        return AppearanceClassificationResult(AppearanceClassification.INCREASE_QUALIFIED, current, min(weights), payload, (ReasonCode.QUALIFIED,))
    misses = sum(item.reps < prescription.target_reps for item in attempts)
    if any(weight < current for weight in weights) or misses >= math.ceil(prescription.prescribed_sets / 2):
        reasons: list[ReasonCode] = []
        if any(weight < current for weight in weights): reasons.append(ReasonCode.BELOW_TEMPLATE_WEIGHT)
        if misses >= math.ceil(prescription.prescribed_sets / 2): reasons.append(ReasonCode.MISSED_TARGET_REPS)
        return AppearanceClassificationResult(AppearanceClassification.MATERIALLY_UNDER_TARGET, current, None, payload, tuple(reasons))
    return AppearanceClassificationResult(AppearanceClassification.NEUTRAL, current, None, payload, (ReasonCode.NEUTRAL_PERFORMANCE,))


def derive_streak(policy: ProgressionPolicy, evidence: Iterable[EvidenceRecord], *, session_exercise_id: int, prescription: str, as_of: datetime) -> StreakResult:
    unique: dict[str, EvidenceRecord] = {}
    for row in evidence:
        if row.policy_version != policy.policy_version or row.prescription_fingerprint != prescription or row.session_exercise_id != session_exercise_id:
            raise ValueError("evidence policy, prescription, or exercise mismatch")
        unique.setdefault(row.evidence_id, row)
    rows = sorted(unique.values(), key=lambda row: (row.appearance_at, row.evidence_id))
    increase = decrease = 0
    decisive: list[str] = []
    last_relevant: datetime | None = None
    last_classification: AppearanceClassification | None = None
    for row in rows:
        last_classification = row.classification
        if last_relevant is not None and row.appearance_at - last_relevant > timedelta(days=policy.evidence_window_days):
            increase = decrease = 0; decisive = []
        if row.classification == AppearanceClassification.INCREASE_QUALIFIED:
            increase += 1; decrease = 0; decisive = (decisive + [row.evidence_id])[-policy.required_consecutive:]; last_relevant = row.appearance_at
        elif row.classification == AppearanceClassification.MATERIALLY_UNDER_TARGET:
            decrease += 1; increase = 0; decisive = (decisive + [row.evidence_id])[-policy.required_consecutive:]; last_relevant = row.appearance_at
        elif row.classification == AppearanceClassification.NEUTRAL:
            increase = decrease = 0; decisive = []; last_relevant = row.appearance_at
        # An unscorable result deliberately leaves the reference timestamp intact.
    expired = bool(last_relevant and as_of - last_relevant > timedelta(days=policy.evidence_window_days))
    if expired:
        increase = decrease = 0; decisive = []
    return StreakResult(increase, decrease, last_classification, last_relevant, tuple(decisive), expired)


def calculate_proposal(policy: ProgressionPolicy, prescription: ExercisePrescription, streak: StreakResult, evidence: Iterable[EvidenceRecord]) -> ProposalResult:
    current = normalize_weight_grams(prescription.template_weight_kg)
    expected = ProposalDirection.INCREASE if streak.increase_count >= policy.required_consecutive else ProposalDirection.DECREASE if streak.decrease_count >= policy.required_consecutive else None
    if expected is None or len(streak.decisive_evidence_ids) != policy.required_consecutive:
        return ProposalResult(None, current, None, (), policy.policy_version, prescription_fingerprint(prescription), None, (ReasonCode.STREAK_NOT_READY,))
    by_id = {row.evidence_id: row for row in evidence}
    decisive = tuple(by_id.get(row_id) for row_id in streak.decisive_evidence_ids)
    if any(row is None or row.policy_version != policy.policy_version or row.prescription_fingerprint != prescription_fingerprint(prescription) for row in decisive):
        return ProposalResult(None, current, None, streak.decisive_evidence_ids, policy.policy_version, prescription_fingerprint(prescription), None, (ReasonCode.EVIDENCE_MISMATCH,))
    if expected == ProposalDirection.INCREASE:
        if any(row.classification != AppearanceClassification.INCREASE_QUALIFIED or row.candidate_weight_grams is None for row in decisive):
            return ProposalResult(None, current, None, streak.decisive_evidence_ids, policy.policy_version, prescription_fingerprint(prescription), None, (ReasonCode.EVIDENCE_MISMATCH,))
        proven = min(row.candidate_weight_grams for row in decisive)
        suggested = proven if proven > current else current + policy.global_increment_grams
    else:
        if any(row.classification != AppearanceClassification.MATERIALLY_UNDER_TARGET for row in decisive):
            return ProposalResult(None, current, None, streak.decisive_evidence_ids, policy.policy_version, prescription_fingerprint(prescription), None, (ReasonCode.EVIDENCE_MISMATCH,))
        suggested = current - policy.global_increment_grams
        if suggested <= 0:
            return ProposalResult(None, current, None, streak.decisive_evidence_ids, policy.policy_version, prescription_fingerprint(prescription), None, (ReasonCode.DECREASE_FLOOR,))
    key = fingerprint({"session_exercise_id": prescription.session_exercise_id, "policy_version": policy.policy_version, "prescription_fingerprint": prescription_fingerprint(prescription), "direction": expected.value, "current_weight_grams": current, "suggested_weight_grams": suggested, "decisive_evidence_ids": streak.decisive_evidence_ids})
    return ProposalResult(expected, current, suggested, streak.decisive_evidence_ids, policy.policy_version, prescription_fingerprint(prescription), key, ())
