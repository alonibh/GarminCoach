# GarminCoach Roadmap

Updated 2026-07-27. The approved target metric and sync policy is
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

## Next — Phase 2A: Garmin compatibility and contract safety

This is the next Codex task. It must not refactor sync or alter coaching behavior.

- [ ] Record production Python and installed `garminconnect` versions.
- [ ] Move the supported runtime to Python 3.12.
- [ ] Pin and test `garminconnect[typed]==0.3.7`.
- [ ] Verify encrypted token restore, fresh login, MFA resume, token refresh, and
  the expected one-time reauthentication for pre-0.3 tokens.
- [ ] Add sanitized fixtures/contract tests for daily stats, sleep, HRV, Body
  Battery, Training Readiness, Recovery Time, activities, body composition,
  Fitness Age, VO2 max, and Training Status.
- [ ] Fix Training Readiness snapshot selection behind a stable GarminCoach adapter.
- [ ] Evaluate upstream strength helpers, exercise catalog, and `update_workout`
  without replacing curated GarminCoach program policy.
- [ ] Run the complete test suite and provide a compatibility report before deployment.

**Gate:** existing production behavior remains unchanged and all required payload
shapes are covered by fixtures.

## Phase 2B: Resource-aware sync foundation

- [ ] Replace the global sync cursor with per-resource cursors and gaps.
- [ ] Implement Stage 1 usable bootstrap and bounded Stage 2 backfill.
- [ ] Prefer combined/range endpoints over per-day duplicate requests.
- [ ] Add endpoint telemetry and immediate low-priority circuit break on first 429.
- [ ] Make activity enrichment explicit and new/incomplete-activity only.
- [ ] Remove all ordinary UI-triggered Garmin calls.
- [ ] Make manual sync current/incremental rather than a full rebuild.

**Gate:** a fresh database becomes usable quickly, resumes safely after failure,
and never restarts a monolithic 90-day health crawl.

## Phase 3: Approved metrics and recovery flow

- [ ] Store HRV Status, seven-day average, and coverage.
- [ ] Store Recovery Time and richer Body Battery summaries.
- [ ] Add intensity minutes and conditional body-composition support.
- [ ] Make capability device/account/scale/activity scoped.
- [ ] Evaluate recovery only after a workout is selected in the website.
- [ ] Use Training Readiness as the only biometric with direct V1 authority.
- [ ] Show unsupported-device sleep/HRV/Recovery Time warnings without inventing
  a replacement score or automatic fallback outcome.
- [ ] Add the fixed 30-minute Active Recovery Garmin workout.
- [ ] Add Telegram-only original/walk/rest actions with pending-session semantics.

**Gate:** no biometric except fresh Training Readiness can automatically change
the selected workout, and every mutation requires a current Telegram confirmation.

## Phase 4: Progress and UI

- [ ] Add meaningful 28-day recovery/health trends.
- [ ] Keep Fitness Age and VO2 max with weekly current-value refresh and local history.
- [ ] Show Training Status only when account/device capability supports it.
- [ ] Add weight/body-fat trends only when useful account data exists.
- [ ] Improve weekly summary with meaningful changes, coverage, program adherence,
  and strength progression.
- [ ] Keep AI context compact and exclude raw sensor histories.

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
