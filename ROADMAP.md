# GarminCoach Roadmap

- [x] Phase 3 foundation: capability registry for one Garmin watch per user, with normalized model detection and versioned, officially sourced mappings. Track Training Readiness, Training Status, Recovery Time, HRV Status, Body Battery, Fitness Age, and VO₂ max independently as supported, unsupported, or unknown; successful observations may promote support, while empty results do not prove unsupported. Use bounded low-frequency probing only for unknown capabilities and skip verified unsupported endpoints.

Updated 2026-07-29. The approved target metric and sync policy is
[`docs/METRIC_SYNC_POLICY.md`](docs/METRIC_SYNC_POLICY.md).

## Completed baseline

- [x] Garmin authentication/MFA and local token persistence.
- [x] Activities, strength sets, sleep, HRV, resting HR, Body Battery, stress,
  steps, Fitness Age, VO2 max, and supported-device Training Readiness.
- [x] Dashboard, activity views, calendar, program editor, Garmin workout
  compilation, deterministic program cursor, and completion reconciliation.
- [x] Per-signal freshness, tri-state capability, typed persisted decisions,
  versioned Telegram confirmations, durable outbox, reminders, calendar conflict
  handling, and deterministic weekly summary.
- [x] Retired custom readiness and limited ACWR to descriptive UI use.
- [x] Approved Phase 1 metric authority, sync windows, and product surfaces.

## Phase 2A: Garmin compatibility and contract safety — complete

Compatibility/runtime closure is complete; Phase 3 product work remains intentionally unstarted.

- [x] Supported runtime is Python 3.12 and requirements pin
  `garminconnect[typed]==0.3.7`.
- [x] Encrypted token restore, fresh login, MFA resume, token refresh, and the
  one-time reauthentication path for pre-0.3 tokens are covered by code and tests.
- [x] Sanitized contract fixtures cover implemented daily stats, sleep, HRV,
  Body Battery, Training Readiness, activities, Fitness Age, VO2 max, and
  Training Status adapters; Training Readiness selection is normalized behind the
  GarminCoach adapter.
- [x] Upstream strength/workout compatibility was evaluated without replacing
  curated GarminCoach program policy.
- [x] Compatibility and regression suites cover the supported runtime contracts.

Account-specific Recovery Time and body-composition payload capability remains
unknown and is intentionally not claimed as product support. Their product work
remains in Phase 3.

## Phase 2B: Resource-aware sync foundation

- [x] Replace the global sync cursor with per-resource cursors.
- [x] Track bounded Stage 2 summary gaps (sleep, daily health, and activity
  summaries), including the bounded strength-detail completion journal.
- [x] Implement the bounded Stage 1 usable bootstrap (device/capability,
  current recovery facts, seven wellness days, 30 activity days, ten strength
  details, and current slow metrics).
- [x] Complete bounded scheduled Stage 2 summary backfill (28 wellness days and
  90 activity-summary days).
- [x] Complete bounded scheduled Stage 2 strength-detail backfill (fixed latest
  20 eligible strength activities within the Stage 2 90-day window).
- [x] Prefer combined/range endpoints over per-day duplicate requests.
- [x] Add endpoint telemetry.
- [x] Immediately circuit-break low-priority Garmin work on the first 429.
- [x] Make general activity enrichment explicit and new/incomplete-activity only.
- [x] Remove all ordinary UI-triggered Garmin calls.
- [x] Make manual sync current/incremental rather than a full rebuild.

**Complete:** Phase 2 is complete. A fresh database becomes usable quickly,
resumes safely after failure, and never restarts a monolithic 90-day health crawl.

## Phase 3: Approved metrics and recovery flow

- [x] Verify HRV Status and Recovery Time payloads with pinned typed fixtures.
- [x] Store HRV Status, Garmin seven-day average, and local seven-night coverage.
- [x] Store Recovery Time and richer Body Battery summaries.
- [x] Store daily moderate/vigorous intensity minutes and local 7-day/28-day totals
  from the verified daily summary.
- [ ] Add conditional body-composition support only after a sanitized pinned
  payload-and-units fixture; see
  [`docs/BODY_COMPOSITION_CONTRACT.md`](docs/BODY_COMPOSITION_CONTRACT.md).
- [x] Make capability device/account/scale/activity scoped.
- [x] Evaluate recovery only after a workout is selected in the website.
- [x] Use Training Readiness as the only biometric with direct V1 authority.
- [x] Show unsupported-device sleep/HRV/Recovery Time warnings without inventing
  a replacement score or automatic fallback outcome.
- [x] Add the fixed 30-minute Active Recovery Garmin workout.
- [x] Add Telegram-only original/walk/rest actions with pending-session semantics.

**Gate:** no biometric except fresh Training Readiness can automatically change
the selected workout, and every mutation requires a current Telegram confirmation.

**Complete:** Phase 3 is complete for all currently verified metrics and recovery actions. Body Composition remains intentionally deferred behind its sanitized fixture-and-units contract gate and is not required for Phase 3 closure.

## Phase 4: Progress and UI

- [x] Phase 4A: deterministic strength-progression contract. The approved
  documentation-only design is
  [`docs/STRENGTH_PROGRESSION_CONTRACT.md`](docs/STRENGTH_PROGRESSION_CONTRACT.md).
- [x] Phase 4B1: durable policy/evidence/streak/proposal foundation and pure
  deterministic read-only progression engine.
- [x] Phase 4B2: bounded recalculation is wired to newly resolved sets, newly
  confident matches, authoritative corrections, template invalidation, and active
  program invalidation. Proposals remain read-only; no UI or notifications exist.
- [x] Phase 4C: one web review page with independent edit/approve/reject,
  exact revalidation, local weight-only mutation, immutable history and
  rejection boundaries; scheduled workouts remain unchanged.
- [x] Phase 4D: material proposal detection, one deduplicated Telegram summary
  per recalculation boundary, durable batch/receipt intent with an outbox bridge,
  delivery-time revalidation, and web-only decisions. Approved local weights are
  consumed by future compilation; already scheduled workouts remain unchanged.
- [x] Phase 4E: meaningful 28-day recovery/health trends. Local-only cards
  compare recent 7-day medians with the preceding 21 calendar days, preserve
  exact missing-data coverage, show sleep-timing variability plus stress and
  separate Body Battery facts, and offer 7d/28d source charts. They make no
  Garmin request and have no workout, notification, or coaching authority.
- [x] Phase 4F: durable Fitness Age, target Fitness Age, activity-scoped VO₂ max,
  and device-scoped Training Status history with capability-aware local UI.
- [x] Phase 4G: deterministic weekly summary using local training, movement,
  approved recovery-trend, and scoped slow-history aggregates with outbox-only
  plain-text delivery.
- [ ] Phase 4H: compact aggregate Ask Coach context under a separate privacy contract.

Phase 4A–4G are complete; Phase 4H remains unchecked.
Progression evidence has no authority from Garmin RPE, Garmin Feel,
subjective feedback, biometrics, readiness, or recovery metrics. Any future
approved change is limited to a local `SessionExercise.weight_kg`; it never
automatically rewrites exercise structure or already scheduled Garmin workouts.


## Later product work

- Source-template superset and transition-timer fidelity.
- Source-specific progression proposals with confirmation.
- Program-duration/deload review prompts.
- Operator health and restore tooling.
- Additional evidence rules only after separate review and boundary tests.

## Deliberately excluded

- Custom readiness scores.
- Metric-driven reductions in exercises, sets, reps, or weights.
- ACWR-based injury or workout decisions.
- Medical, diagnostic, emergency, nutrition, or injury-risk recommendations.
- Raw intraday wellness timelines and per-second activity streams without a
  separately approved product use.
