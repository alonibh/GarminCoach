# Strength-progression proposal contract

**Status:** Phase 4A–4D complete. All progression stages are implemented.
**Scope:** Deterministic, exercise-level proposals to change a local template weight

This is the canonical contract for Phase 4 strength progression. Phase 4B
persistence/recalculation, Phase 4C local web review/actions, and Phase 4D
deduplicated Telegram notification flow are all implemented.

Ordinary rows remain governed by this generic contract. The sole source-specific
exception is the server-owned Powerbuilding PPL 15-rep rule, whose additional
requirements and safety overlay are authoritative in
[`SOURCE_PROGRESSION_CONTRACT.md`](SOURCE_PROGRESSION_CONTRACT.md).

## 1. Product boundary and authority

The web application is the sole review surface. It will show all current proposals on one page, while every `SessionExercise` is reviewed, approved, rejected, edited, and mutated independently. There is no batch approval or “Approve all” action.

Telegram MAY send one notification that proposals are ready or materially changed, directing the athlete to the web review page. Telegram MUST NOT expose progression approval or rejection buttons.

The engine is deterministic. An LLM MUST NOT match or classify an activity/exercise, propose or choose a weight, approve or reject a proposal, or execute a mutation. Garmin RPE, Garmin Feel, subjective feedback, Training Readiness, sleep, HRV, Recovery Time, Body Battery, stress, ACWR, and every other biometric have zero progression authority. Training Readiness remains the sole biometric with direct recovery authority under the separate recovery contract; that does not give it any strength-progression authority.

Progression MAY change only `SessionExercise.weight_kg`. It MUST NOT change an exercise identity or order, sets, reps, duration, rest, warm-up, or Garmin mapping. In particular, quarter-kilogram weights and a 60-second warm-up rest remain representable and must be preserved. Already scheduled Garmin workouts MUST remain unchanged. An approved local template weight applies only when a future workout is compiled. Evaluation is local and MUST make zero Garmin calls.

Private external calendars remain outside Ask Coach and any AI snapshot.

## 2. Existing domain ownership

The implementation uses these entities:

| Entity | Contract role |
| --- | --- |
| `TrainingProgram` | Owns the active program whose completed work can provide evidence. |
| `ProgramSession` | Defines the exact programmed session matched to an activity. |
| `SessionExercise` | The exact progression owner and the only local field that an approved proposal may change. |
| `Activity` | A locally stored completed Garmin activity; it is never queried from Garmin by this engine. |
| `ExerciseSet` | Locally stored set facts, including Garmin values and protected manual corrections. |
| `ActivityProgramMatch` | The existing authoritative deterministic link from an `Activity` to an active-program `ProgramSession`. |
| `PlannedSession` | May describe a scheduled/compiled workout, but its already scheduled Garmin workout is not retroactively changed. |

Progression belongs to the exact `SessionExercise` row, not a display name or exercise type. The same named exercise in two `ProgramSession` rows has separate evidence, streaks, proposals, prescription versions, and audit history. A duplicate name therefore fails closed unless the exact owning `SessionExercise` can be resolved uniquely.

Evidence is permitted only for an `Activity` confidently linked by the existing deterministic `ActivityProgramMatch` policy to the exact `ProgramSession` of the active `TrainingProgram`. Unmatched, ambiguous, unrelated, inactive-program, or legacy-only activities MUST create no evidence. An eligible exercise MUST have a positive, unambiguous numeric template `weight_kg`. Bodyweight, duration-only, missing-weight, ambiguous-weight, and unverified assisted/bodyweight semantics are excluded. This phase has no rep, duration, set, substitution, source-specific, or deload progression.

## 3. Exercise identity and set preparation

Within an already confidently matched program session, a completed exercise group is matched to a `SessionExercise` in this order:

1. Its primary identity is the Garmin category/name matched to the stored `SessionExercise.garmin_category`/`garmin_name`.
2. Exercise order is secondary disambiguation only; it cannot rescue a weak or generic identity.
3. A normalized exercise key is a fallback only when exact identifiers are unavailable and the result is unique.

Generic, unknown, duplicate, or ambiguous identity fails closed. The engine MUST accept only an exact one-to-one completed exercise-group-to-`SessionExercise` match. Uncertain matching MUST NOT create, reset, or otherwise modify evidence. For example, two “Row” exercises in one session cannot be assigned by order alone when their stored identities are not uniquely resolvable.

Recorded and athlete-entered progression weights are normalized to the nearest 0.25 kg with decimal **round-half-up**, never binary floating-point rounding or banker’s rounding. Valid classified fractions are `.00`, `.25`, `.50`, and `.75`. Examples: `72.30 -> 72.25`, `72.40 -> 72.50`, `72.62 -> 72.50`, and `72.63 -> 72.75`. Future evidence and audit records retain the original source value as well as the normalized value used for classification.

Rest sets never count. Warm-up sets never count. The engine SHOULD prefer a reliable explicit Garmin set classification. If a warm-up is not explicitly labeled, it MAY identify a leading warm-up only when all of the following are uniquely true: it belongs to the matched exercise, is leading in chronological order, matches the template warm-up reps or duration, and has the matching normalized warm-up weight. It MUST NOT infer a warm-up merely because the set is lighter. If warm-up and working sets cannot be distinguished, the appearance is unscorable.

After exclusions, the engine evaluates only the first prescribed number of valid working sets in chronological order. Later sets are informational extras: they cannot replace a missing or failed prescribed set, and the engine MUST never select the best sets.

## 4. Appearance classification

For an eligible `SessionExercise`, let `N` be prescribed sets, `R` prescribed reps, and `W` its current normalized template weight. An appearance is a matched, exercise-specific group within one eligible `Activity`.

### Increase-qualified appearance

An appearance is increase-qualified only if exactly the first `N` valid working sets are evaluable, and each has valid reps and normalized weight, reps `>= R`, and weight `>= W`. Its candidate weight is:

```text
candidate_weight = min(normalized weight of each of the first N working sets)
```

This is the largest weight proven across every prescribed set. At a `3 x 10` prescription, `72.5/72.5/70` proves `70`; `75/72.5/72.5` proves `72.5`. Extra reps and later extra sets do not invalidate success.

### Materially under-target appearance

A fully observed, scorable appearance is materially under-target if any of the following is true:

- at least `ceil(N / 2)` prescribed sets miss target reps at current weight;
- any prescribed working set is below current weight; or
- fewer than `N` working sets were actually completed.

For an odd `N`, the threshold is rounded up: with `N = 3`, two misses are required, so `10/10/9` at target is neutral rather than under-target. Fewer sets completed applies only when a known-complete strength payload deterministically shows fewer real working attempts. Missing, malformed, partially fetched, unresolved, or ambiguous data is unscorable, never failed performance.

### Neutral and unscorable

Neutral means scorable but neither increase-qualified nor materially under-target. It resets both increase and decrease streaks, and later supersedes an incompatible pending proposal. For example, `3 x 10` completed as `10/10/9` at current weight is neutral.

Unscorable includes missing/invalid reps or weight, incomplete strength details, ambiguous activity/session/exercise matching, ambiguous warm-up separation, and unsupported exercise semantics. It does not count as increase, decrease, or neutral and preserves a prior streak subject to its 35-day expiry. It MAY be recalculated after a manual correction. Missing data MUST NEVER be treated as failed performance.

## 5. Streaks and proposal formulas

There is one global configurable increment `I`, default **2.5 kg**. It MUST normalize to a positive quarter-kilogram value; V1 has no per-exercise increments. If it changes, the system MUST reset every increase/decrease streak, supersede pending proposals, preserve immutable history, leave approved weights and scheduled workouts unchanged, and require two entirely new qualifying appearances under the new policy version.

Two consecutive classifications of the same exact `SessionExercise` are required. “Consecutive” means consecutive appearances of that exact exercise in confidently matched active-program workouts; matched workouts without that exercise are ignored. The appearances must be no more than 35 days apart. Exactly 35 days remains valid; more than 35 days expires the old streak. An unscorable appearance does not itself reset a streak, but it cannot avoid the 35-day expiry boundary.

After two consecutive increase-qualified appearances with candidates `C1` and `C2`, calculate:

```text
common_proven_weight = min(C1, C2)
suggested_weight = common_proven_weight, if common_proven_weight > W
                   = W + I, otherwise
```

Normalize the result to 0.25 kg. A higher weight proven in both workouts may exceed one increment and MUST NOT be capped: with `W = 70`, `I = 2.5`, and `C1/C2 = 75/75`, propose `75`, not `72.5`. If both qualifying workouts merely prove `W`, propose `W + I`; for `W = 72.5`, this is `75.0` at the default increment.

After two consecutive materially under-target appearances, propose exactly `W - I`, normalized to 0.25 kg. Do not derive a reduction from the lowest performed weight. The result MUST remain positive: if subtraction is zero or negative, create no automatic proposal and require an ordinary manual editor change instead.

## 6. Corrections and prescription versions

Existing manually corrected `ExerciseSet` values are authoritative and their protection from Garmin re-sync overwrite MUST be preserved. Future evidence and audit records distinguish Garmin-origin and manually corrected values. Editing or reverting a correction recalculates the affected exact `SessionExercise`; it may make evidence scorable, change classification, reset/advance a streak, or create, supersede, invalidate, or stale a proposal. A correction never approves a template mutation.

The implementation versions a `SessionExercise` prescription. A prescription change to target weight, sets, reps, identity/key, Garmin mapping, eligibility, or program/session ownership resets both streaks and supersedes incompatible pending proposals. Old activities MUST NOT be reinterpreted under a changed prescription. An approved proposal or ordinary manual editor weight change starts with zero evidence; it follows the same reset and audit boundary.

## 7. Proposal lifecycle and web review

The conceptual proposal states are `pending`, `applied`, `rejected`, `superseded`, and `stale`. Terminal history is immutable. At most one `pending` proposal may exist for an exact `SessionExercise` and its current prescription and policy version.

Recalculation evaluates new evidence against the unchanged current template. If direction and suggested value are unchanged, it keeps the current proposal without a duplicate state or notification. A material direction/value change supersedes the old proposal and creates one new current proposal. Neutral, opposite, expired, corrected, or template-change evidence supersedes or stales an incompatible proposal. Generation and recalculation MUST be idempotent.

Every proposal identifies the exact program, session, `SessionExercise`, prescription version, policy version, source activities, decisive sets, current weight, suggested weight, direction, timestamps, and correction provenance. Before approval/rejection it reloads and revalidates this decisive state, failing closed if it has become stale.

The web review page will show all current proposals. Each independently shows the program/session/exercise, current and proposed weight/direction, global increment, two decisive appearances with their sets/reps/weights, manual correction provenance, evidence age/expiry, and policy/prescription versions.

Independent actions are approve, reject, and edit-final-weight-and-approve. Edited approval must normalize to 0.25 kg; an increase must remain above the current weight and a decrease below it and positive. The athlete may choose a more or less aggressive valid weight than the engine suggestion. Only `SessionExercise.weight_kg` changes; audit retains engine-suggested and athlete-approved values. Approval resets both streaks, affects only future compilation, and never changes an already scheduled Garmin workout.

Reject marks the proposal rejected, leaves the template unchanged, resets both streaks, and MUST NOT reuse the same two source appearances: two entirely new consecutive qualifying appearances are required. There is no dedicated proposal-rollback button. Reversal is an ordinary program-editor weight edit; it follows ordinary audit/reset/supersession rules, affects only future compilation, and does not rewrite scheduled workouts.

## 8. Persistence and notification design

Phase 4B persistence records and Phase 4D notification design are implemented:

- a versioned progression policy, including the global increment;
- a versioned `SessionExercise` prescription fingerprint;
- immutable per-appearance evidence and its classification;
- derived streak state or enough ordered evidence to derive it deterministically;
- pending and historical proposals with source evidence and decisive sets;
- suggested versus athlete-approved values, status transitions, correction provenance, recalculation cause, timestamps, and idempotency keys; and
- notification-dedup records and enough previous state for audit, without a proposal rollback action.

The Telegram outbox summary uses a stable dedup identity
`(policy_version, recalculation_boundary_id, sorted material proposal fingerprints)`. It sends one immediate deduplicated summary for proposals created in the same recalculation/sync boundary, names affected exercises, and directs the athlete to web review. It does not send per-exercise spam or repeat when nothing material changed. It may notify again only for a new proposal, a changed direction, a changed proposed weight, or an exercise becoming eligible from entirely new evidence.

## 9. Evaluation triggers

Evaluation runs after strength sets finish syncing for a matched activity; an activity becomes confidently matched; an authoritative manual correction changes; a `SessionExercise` prescription/mapping changes; a proposal is approved/rejected; or the global increment changes. It recalculates only affected exercises where possible. A global increment change is an explicit whole-population invalidation. All evaluation is idempotent and makes no Garmin call.

| Stage | Status |
| --- | --- |
| Phase 4B | Complete: persistence and read-only deterministic engine; matching, classification, incremental recalculation, and pending proposals. |
| Phase 4C | Complete: web list/detail plus independent edit/approve/reject, exact revalidation, local `SessionExercise.weight_kg` mutation only, immutable audit, no scheduled-workout rewrite. |
| Phase 4D | Complete: deduplicated Telegram outbox summary; bounded sync/match/correction/editor/proposal triggers; stale/duplicate/retry/concurrency hardening; approved local weight consumed by future compilation. |
| Phase 4E–4H | Complete: 28-day recovery/health trends, Fitness Age/VO₂ max history, capability-aware Training Status, weekly-summary improvements, and compact aggregate AI context. |

## 10. Phase 4B2/4C implementation boundary

Phase 4B2 is event-driven and bounded: it evaluates only activities explicitly
dirtied by newly resolved strength sets, a newly confident match, or an
authoritative correction. Untouched pre-B2 historical activity/match/set rows
are not broadly backfilled. Phase 4C provides one tenant-local web review page
with independent approve/edit-final-weight/reject actions. Approval mutates
only the local `SessionExercise.weight_kg`; scheduled Garmin workouts remain
unchanged. Rejection records the greatest current `(appearance_at, evidence_id)`
row for audit, but eligibility is strictly later **appearance timestamps**.
Every evidence revision at or before the cutoff appearance remains excluded and
cannot contribute again under the unchanged prescription, so two entirely new
qualifying appearances are required. Phase 4D records only newly created or
materially replaced pending proposals as immutable, tenant-local material facts.
A deterministic recalculation boundary creates at most one notification batch
and one receipt per proposal material fingerprint; retries, support refreshes,
stale transitions, approval, and rejection do not create a notification. The
batch is bridged to exactly one ordinary outbox row using a stable idempotency
key. The existing tenant-scoped outbox poller is the sole sender and revalidates
each exact proposal at delivery time. Telegram sends plain-text summaries
without progression action buttons; decisions remain web-only. A later
notification is possible only for a genuinely new material proposal row.
Approved local weight is used by future compilation, while already scheduled
workouts remain unchanged.

## 11. Explicit exclusions

Explicitly excluded by design: remote Garmin replacement/update; changes to scheduled Garmin workouts; exercise/set/rep/rest/warm-up mutation; bodyweight, rep, or duration progression; subjective too-easy/too-hard flows; Garmin RPE/Feel authority; biometric/readiness/ACWR authority; LLM matching or decisions; private-calendar access; new Garmin endpoint calls for progression; deload logic; injury, medical, or nutrition claims; and dedicated rollback.

Body Composition remains separately unchecked and gated on its sanitized fixture-and-units contract. This contract does not enable probing, storage, UI, or implementation for it.
