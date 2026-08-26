from datetime import datetime, timedelta

import pytest

from coach.strength_progression import (
    AppearanceClassification, AppearanceInput, CompletedExerciseGroup, EvidenceRecord,
    ExercisePrescription, ObservedSet, ProgressionPolicy, ProposalDirection,
    ReasonCode, calculate_proposal, classify_appearance, derive_streak,
    match_exercise_groups, normalize_weight_grams, prescription_fingerprint,
)


def _prescription(**overrides):
    values = dict(program_id=1, program_session_id=2, session_exercise_id=3,
        exercise_name="Bench Press", exercise_key="BENCH_PRESS", garmin_category="BENCH_PRESS",
        garmin_name="BARBELL_BENCH_PRESS", is_generic=False, prescribed_sets=3,
        target_reps=10, template_weight_kg="72.5", warmup_enabled=False, order_index=1)
    values.update(overrides)
    return ExercisePrescription(**values)


def _group(*sets, **overrides):
    values = dict(group_id="g", garmin_category="BENCH_PRESS", garmin_name="BARBELL_BENCH_PRESS",
                  exercise_key="BENCH_PRESS", order_index=1, sets=tuple(sets))
    values.update(overrides)
    return CompletedExerciseGroup(**values)


@pytest.mark.parametrize(("value", "grams"), [
    ("72.30", 72250), ("72.40", 72500), ("72.62", 72500), ("72.625", 72750),
    ("72.63", 72750), (72.25, 72250), ("72.75", 72750),
])
def test_weight_normalization_is_decimal_half_up(value, grams):
    assert normalize_weight_grams(value) == grams


@pytest.mark.parametrize("value", [None, True, 0, -1, "0.001", "NaN", "Infinity", "bad"])
def test_weight_normalization_rejects_invalid_inputs(value):
    with pytest.raises(ValueError):
        normalize_weight_grams(value)


def test_prescription_fingerprint_covers_warmup_and_identity():
    baseline = _prescription(warmup_enabled=True, warmup_reps=8, warmup_weight_kg="40")
    assert prescription_fingerprint(baseline) != prescription_fingerprint(_prescription(warmup_enabled=True, warmup_reps=8, warmup_weight_kg="42.5"))
    assert prescription_fingerprint(baseline) != prescription_fingerprint(_prescription(exercise_key="OTHER", warmup_enabled=True, warmup_reps=8, warmup_weight_kg="40"))


def test_exact_match_uses_order_only_to_disambiguate_strong_identity():
    first, second = _prescription(session_exercise_id=10, order_index=0), _prescription(session_exercise_id=11, order_index=1)
    match = match_exercise_groups([_group(order_index=1)], [first, second])[0]
    assert match.matched and match.session_exercise_id == 11
    assert not match_exercise_groups([_group(garmin_category=None, garmin_name=None, exercise_key=None)], [first])[0].matched


def test_key_fallback_must_be_unique_and_generic_fails_closed():
    candidate = _prescription(garmin_category=None, garmin_name=None)
    assert match_exercise_groups([_group(garmin_category=None, garmin_name=None)], [candidate])[0].matched
    duplicate = _prescription(session_exercise_id=4, garmin_category=None, garmin_name=None)
    assert not match_exercise_groups([_group(garmin_category=None, garmin_name=None)], [candidate, duplicate])[0].matched
    assert not match_exercise_groups([_group(is_generic=True)], [_prescription()])[0].matched


def test_classification_excludes_rest_and_exact_warmup_then_uses_first_attempts():
    prescription = _prescription(warmup_enabled=True, warmup_reps=8, warmup_weight_kg=40)
    group = _group(ObservedSet(0, "REST"), ObservedSet(1, "WARM_UP", 8, 40),
        ObservedSet(2, "ACTIVE", 10, 72.5), ObservedSet(3, "ACTIVE", 10, 72.5),
        ObservedSet(4, "ACTIVE", 10, 72.5), ObservedSet(5, "ACTIVE", 3, 1))
    result = classify_appearance(AppearanceInput(prescription, group, True, datetime(2026, 1, 1)))
    assert result.classification == AppearanceClassification.INCREASE_QUALIFIED
    assert result.candidate_weight_grams == 72500


def test_inferred_warmup_requires_all_fields_and_invalid_early_set_is_not_replaced():
    p = _prescription(warmup_enabled=True, warmup_reps=8, warmup_weight_kg=40)
    inferred = _group(ObservedSet(0, "ACTIVE", 8, 40), *[ObservedSet(i, "ACTIVE", 10, 72.5) for i in range(1, 4)])
    assert classify_appearance(AppearanceInput(p, inferred, True, datetime.now())).classification == AppearanceClassification.INCREASE_QUALIFIED
    lighter_only = _group(ObservedSet(0, "ACTIVE", 7, 40), *[ObservedSet(i, "ACTIVE", 10, 72.5) for i in range(1, 4)])
    lighter_result = classify_appearance(AppearanceInput(p, lighter_only, True, datetime.now()))
    assert lighter_result.classification == AppearanceClassification.UNSCORABLE
    assert ReasonCode.AMBIGUOUS_WARMUP in lighter_result.reason_codes
    bad_early = _group(ObservedSet(0, "ACTIVE", None, 72.5), *[ObservedSet(i, "ACTIVE", 10, 72.5) for i in range(1, 4)])
    assert classify_appearance(AppearanceInput(_prescription(), bad_early, True, datetime.now())).classification == AppearanceClassification.UNSCORABLE


def test_warmup_ambiguity_does_not_turn_a_modified_warmup_into_decrease_evidence():
    p = _prescription(warmup_enabled=True, warmup_reps=8, warmup_weight_kg=40)
    successful_tail = [ObservedSet(index, "ACTIVE", 10, 72.5) for index in range(1, 4)]
    ambiguous = classify_appearance(AppearanceInput(p, _group(ObservedSet(0, "ACTIVE", 7, 40), *successful_tail), True, datetime.now()))
    assert ambiguous.classification == AppearanceClassification.UNSCORABLE
    assert ReasonCode.AMBIGUOUS_WARMUP in ambiguous.reason_codes
    template_weight = classify_appearance(AppearanceInput(p, _group(ObservedSet(0, "ACTIVE", 7, 72.5), *successful_tail), True, datetime.now()))
    assert template_weight.classification == AppearanceClassification.NEUTRAL
    missing_weight = classify_appearance(AppearanceInput(p, _group(ObservedSet(0, "ACTIVE", 8, None), *successful_tail), True, datetime.now()))
    assert missing_weight.classification == AppearanceClassification.UNSCORABLE
    assert ReasonCode.INVALID_WEIGHT in missing_weight.reason_codes


def test_classification_missing_data_is_not_underperformance_and_neutral_resets():
    p = _prescription()
    incomplete = _group(ObservedSet(0, "ACTIVE", 10, 72.5), ObservedSet(1, "ACTIVE", 10, 72.5))
    assert classify_appearance(AppearanceInput(p, incomplete, False, datetime.now())).classification == AppearanceClassification.UNSCORABLE
    complete = classify_appearance(AppearanceInput(p, incomplete, True, datetime.now()))
    assert complete.classification == AppearanceClassification.MATERIALLY_UNDER_TARGET
    neutral = _group(ObservedSet(0, "ACTIVE", 10, 72.5), ObservedSet(1, "ACTIVE", 10, 72.5), ObservedSet(2, "ACTIVE", 9, 72.5))
    assert classify_appearance(AppearanceInput(p, neutral, True, datetime.now())).classification == AppearanceClassification.NEUTRAL


def _evidence(identifier, when, classification, candidate=None):
    return EvidenceRecord(identifier, 3, "strength-progression-v1", "fp", when, classification, candidate)


def test_streaks_sort_deduplicate_preserve_unscorable_and_expire():
    policy = ProgressionPolicy()
    start = datetime(2026, 1, 1)
    first = _evidence("a", start, AppearanceClassification.INCREASE_QUALIFIED, 72500)
    second = _evidence("b", start + timedelta(days=35), AppearanceClassification.INCREASE_QUALIFIED, 72500)
    streak = derive_streak(policy, [second, first, first], session_exercise_id=3, prescription="fp", as_of=second.appearance_at)
    assert streak.increase_count == 2 and streak.decisive_evidence_ids == ("a", "b")
    stale = derive_streak(policy, [first, _evidence("u", start + timedelta(days=30), AppearanceClassification.UNSCORABLE)], session_exercise_id=3, prescription="fp", as_of=start + timedelta(days=36))
    assert stale.expired and stale.increase_count == 0
    reset = derive_streak(policy, [first, _evidence("n", start + timedelta(days=1), AppearanceClassification.NEUTRAL)], session_exercise_id=3, prescription="fp", as_of=start + timedelta(days=1))
    assert reset.increase_count == reset.decrease_count == 0


def test_streak_boundaries_and_mismatches_fail_closed():
    policy, start = ProgressionPolicy(), datetime(2026, 1, 1)
    first = _evidence("a", start, AppearanceClassification.INCREASE_QUALIFIED, 72500)
    exact = _evidence("b", start + timedelta(days=35), AppearanceClassification.INCREASE_QUALIFIED, 72500)
    assert derive_streak(policy, [first, exact], session_exercise_id=3, prescription="fp", as_of=exact.appearance_at).increase_count == 2
    late = _evidence("c", start + timedelta(days=35, seconds=1), AppearanceClassification.INCREASE_QUALIFIED, 72500)
    assert derive_streak(policy, [first, late], session_exercise_id=3, prescription="fp", as_of=late.appearance_at).increase_count == 1
    with pytest.raises(ValueError):
        derive_streak(policy, [EvidenceRecord("x", 3, "other", "fp", start, AppearanceClassification.NEUTRAL)], session_exercise_id=3, prescription="fp", as_of=start)
    with pytest.raises(ValueError):
        derive_streak(policy, [EvidenceRecord("x", 3, policy.policy_version, "other", start, AppearanceClassification.NEUTRAL)], session_exercise_id=3, prescription="fp", as_of=start)


def test_proposal_formulas_and_validation():
    policy, p = ProgressionPolicy(), _prescription()
    first, second = _evidence("a", datetime(2026, 1, 1), AppearanceClassification.INCREASE_QUALIFIED, 72500), _evidence("b", datetime(2026, 1, 2), AppearanceClassification.INCREASE_QUALIFIED, 75000)
    streak = derive_streak(policy, [first, second], session_exercise_id=3, prescription="fp", as_of=datetime(2026, 1, 2))
    # Use the exact prescription fingerprint expected by the calculator.
    first = EvidenceRecord("a", 3, policy.policy_version, prescription_fingerprint(p), first.appearance_at, first.classification, first.candidate_weight_grams)
    second = EvidenceRecord("b", 3, policy.policy_version, prescription_fingerprint(p), second.appearance_at, second.classification, second.candidate_weight_grams)
    streak = derive_streak(policy, [first, second], session_exercise_id=3, prescription=prescription_fingerprint(p), as_of=second.appearance_at)
    proposal = calculate_proposal(policy, p, streak, [first, second])
    assert proposal.direction == ProposalDirection.INCREASE and proposal.suggested_weight_grams == 75000
    under = [EvidenceRecord("c", 3, policy.policy_version, prescription_fingerprint(p), datetime(2026, 1, 3), AppearanceClassification.MATERIALLY_UNDER_TARGET, 70000), EvidenceRecord("d", 3, policy.policy_version, prescription_fingerprint(p), datetime(2026, 1, 4), AppearanceClassification.MATERIALLY_UNDER_TARGET, 70000)]
    down = calculate_proposal(policy, p, derive_streak(policy, under, session_exercise_id=3, prescription=prescription_fingerprint(p), as_of=datetime(2026, 1, 4)), under)
    assert down.direction == ProposalDirection.DECREASE and down.suggested_weight_grams == 70000


def test_proposal_boundaries_and_invalid_working_sets():
    policy, p, fp = ProgressionPolicy(), _prescription(), prescription_fingerprint(_prescription())
    current = [EvidenceRecord("a", 3, policy.policy_version, fp, datetime(2026, 1, 1), AppearanceClassification.INCREASE_QUALIFIED, 72500), EvidenceRecord("b", 3, policy.policy_version, fp, datetime(2026, 1, 2), AppearanceClassification.INCREASE_QUALIFIED, 72500)]
    current_streak = derive_streak(policy, current, session_exercise_id=3, prescription=fp, as_of=datetime(2026, 1, 2))
    held = calculate_proposal(policy, p, current_streak, current)
    assert held.direction is None and ReasonCode.NO_HIGHER_COMMON_WEIGHT in held.reason_codes
    higher = [EvidenceRecord("c", 3, policy.policy_version, fp, datetime(2026, 1, 3), AppearanceClassification.INCREASE_QUALIFIED, 77500), EvidenceRecord("d", 3, policy.policy_version, fp, datetime(2026, 1, 4), AppearanceClassification.INCREASE_QUALIFIED, 75000)]
    high_streak = derive_streak(policy, higher, session_exercise_id=3, prescription=fp, as_of=datetime(2026, 1, 4))
    assert calculate_proposal(policy, p, high_streak, higher).suggested_weight_grams == 75000
    floor_p = _prescription(template_weight_kg="2.5")
    floor_fp = prescription_fingerprint(floor_p)
    under = [EvidenceRecord("e", 3, policy.policy_version, floor_fp, datetime(2026, 1, 5), AppearanceClassification.MATERIALLY_UNDER_TARGET, 0), EvidenceRecord("f", 3, policy.policy_version, floor_fp, datetime(2026, 1, 6), AppearanceClassification.MATERIALLY_UNDER_TARGET, 0)]
    floor = calculate_proposal(policy, floor_p, derive_streak(policy, under, session_exercise_id=3, prescription=floor_fp, as_of=datetime(2026, 1, 6)), under)
    assert floor.direction is None and ReasonCode.DECREASE_FLOOR in floor.reason_codes
    invalid_reps = _group(*[ObservedSet(index, "ACTIVE", 0, 72.5) for index in range(3)])
    assert classify_appearance(AppearanceInput(p, invalid_reps, True, datetime.now())).classification == AppearanceClassification.UNSCORABLE
    for invalid_weight in ("NaN", "Infinity"):
        bad_weight = _group(ObservedSet(0, "ACTIVE", 10, invalid_weight), ObservedSet(1, "ACTIVE", 10, 72.5), ObservedSet(2, "ACTIVE", 10, 72.5))
        assert classify_appearance(AppearanceInput(p, bad_weight, True, datetime.now())).classification == AppearanceClassification.UNSCORABLE


def test_actual_weights_drive_changes_and_above_template_misses_hold():
    p = _prescription(template_weight_kg="14")
    first = _group(*[ObservedSet(index, "ACTIVE", 10, 14) for index in range(3)])
    second = _group(*[ObservedSet(index, "ACTIVE", 10, 16) for index in range(3)])
    first_result = classify_appearance(AppearanceInput(p, first, True, datetime(2026, 1, 1)))
    second_result = classify_appearance(AppearanceInput(p, second, True, datetime(2026, 1, 2)))
    fp = prescription_fingerprint(p)
    evidence = [
        EvidenceRecord("first", 3, "strength-progression-v1", fp, datetime(2026, 1, 1), first_result.classification, first_result.candidate_weight_grams),
        EvidenceRecord("second", 3, "strength-progression-v1", fp, datetime(2026, 1, 2), second_result.classification, second_result.candidate_weight_grams),
    ]
    policy = ProgressionPolicy()
    proposal = calculate_proposal(policy, p, derive_streak(policy, evidence,
        session_exercise_id=3, prescription=fp, as_of=datetime(2026, 1, 2)), evidence)
    assert proposal.direction == ProposalDirection.INCREASE and proposal.suggested_weight_grams == 16000

    press = _prescription(template_weight_kg="12", prescribed_sets=4)
    above_template_misses = _group(
        ObservedSet(0, "ACTIVE", 10, 14), ObservedSet(1, "ACTIVE", 10, 14),
        ObservedSet(2, "ACTIVE", 7, 14), ObservedSet(3, "ACTIVE", 6, 14),
    )
    held = classify_appearance(AppearanceInput(press, above_template_misses, True, datetime(2026, 1, 2)))
    assert held.classification == AppearanceClassification.NEUTRAL
    assert ReasonCode.ABOVE_TEMPLATE_HOLD in held.reason_codes


def test_powerbuilding_source_rep_goal_tiers_and_strict_weight():
    p = _prescription(prescribed_sets=5, target_reps=3, order_index=0,
        progression_rule_key="powerbuilding_rep_goal_15_v1")
    low = _group(*[ObservedSet(index, "ACTIVE", 3, 72.5) for index in range(5)], order_index=0)
    result = classify_appearance(AppearanceInput(p, low, True, datetime.now()))
    assert (result.classification, result.observed_total_reps, result.source_increment_grams) == (
        AppearanceClassification.INCREASE_QUALIFIED, 15, 1250)
    high = _group(*[ObservedSet(index, "ACTIVE", reps, 72.5) for index, reps in enumerate((4, 4, 4, 4, 3))], order_index=0)
    assert classify_appearance(AppearanceInput(p, high, True, datetime.now())).source_increment_grams == 2250
    mismatch = _group(*[ObservedSet(index, "ACTIVE", 3, 72.5 if index else 70) for index in range(5)], order_index=0)
    assert classify_appearance(AppearanceInput(p, mismatch, True, datetime.now())).classification == AppearanceClassification.UNSCORABLE


def test_powerbuilding_source_proposal_uses_lower_of_two_tiers():
    policy = ProgressionPolicy()
    p = _prescription(prescribed_sets=5, target_reps=3, order_index=0,
        progression_rule_key="powerbuilding_rep_goal_15_v1")
    fp = prescription_fingerprint(p)
    rows = [
        EvidenceRecord("a", 3, policy.policy_version, fp, datetime(2026, 1, 1), AppearanceClassification.INCREASE_QUALIFIED, 73750, p.progression_rule_key, 1250, 72500, 15, 15),
        EvidenceRecord("b", 3, policy.policy_version, fp, datetime(2026, 1, 2), AppearanceClassification.INCREASE_QUALIFIED, 74750, p.progression_rule_key, 2250, 72500, 19, 15),
    ]
    streak = derive_streak(policy, rows, session_exercise_id=3, prescription=fp, as_of=datetime(2026, 1, 2))
    proposal = calculate_proposal(policy, p, streak, rows)
    assert (proposal.direction, proposal.suggested_weight_grams, proposal.source_increment_grams) == (ProposalDirection.INCREASE, 73750, 1250)
