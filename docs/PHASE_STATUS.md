# GarminCoach Phase Status

**Status date:** 2026-08-06  
**Phases 1–6: complete.**

This document is the canonical per-phase status summary. See
[`../ROADMAP.md`](../ROADMAP.md) for the full roadmap with per-subphase
checkboxes, and [`../CHANGELOG.md`](../CHANGELOG.md) for release history.

---

## Phase 1 — Approved metric authority and sync windows

**Status: complete**

**Goal:** Establish the authority model, metric map, sync windows, and product
surfaces. Define which metrics have direct V1 workout authority (Training
Readiness only), which are informational, and which are excluded.

**Implemented subphases:** Baseline capabilities, authentication, metrics,
capability registry, sync cadence, and approved authority model.

**Main production modules:**
`sync/garmin_client.py`, `sync/sync_service.py`, `metrics/freshness.py`,
`metrics/engine.py`, `coach/decision_engine.py`

**Main contracts:**
[`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md)

**Main test files:**
`tests/test_decision_engine.py`, `tests/test_freshness.py`,
`tests/test_capability_registry.py`

**Authority boundaries:**
- Garmin Training Readiness is the only biometric with direct V1 workout authority.
- No custom readiness score is computed or shown.
- ACWR is descriptive UI data only.
- Missing data never implies unsupported capability.

**Intentional exclusions:**
- Body Composition until its sanitized payload-and-units fixture is approved
  ([`BODY_COMPOSITION_CONTRACT.md`](BODY_COMPOSITION_CONTRACT.md)).
- Custom readiness scoring.
- ACWR-based injury or workout decisions.
- Medical, diagnostic, emergency, nutrition, or injury-risk recommendations.

---

## Phase 2 — Garmin compatibility and resource-aware sync foundation

**Status: complete**

**Goal:** Pinned `garminconnect[typed]==0.3.7`, Python 3.12 support, per-resource
cursors, bounded Stage 1/2 backfill, 429 circuit-breaking, and no UI-triggered
Garmin calls.

**Implemented subphases:** 2A (compatibility/contracts), 2B (sync foundation).

**Main production modules:**
`sync/sync_service.py`, `sync/sync_runner.py`, `sync/garmin_client.py`

**Main contracts:**
[`GARMINCONNECT_037_COMPATIBILITY.md`](GARMINCONNECT_037_COMPATIBILITY.md)

**Main test files:**
`tests/test_garmin_compat_probe.py`, `tests/test_smart_sync.py`,
`tests/test_priority_sync.py`

**Authority boundaries:**
- No UI-triggered Garmin calls.
- 429 circuit-breaks all low-priority work immediately.
- Manual sync is current/incremental, never a full rebuild.

**Intentional exclusions:**
- Body Composition (see Phase 1 exclusions).
- Historical Training Readiness backfill.

---

## Phase 3 — Approved metrics and recovery flow

**Status: complete**

**Goal:** HRV Status, Recovery Time, richer Body Battery, intensity minutes,
capability-scoped evidence, Active Recovery template, and Telegram-only
recovery choices.

**Implemented subphases:** Phase 3A–3H (all subphases).

**Main production modules:**
`coach/decision_engine.py`, `coach/active_recovery.py`, `db.py`,
`metrics/freshness.py`

**Main contracts:**
[`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md),
[`RECOVERY_HEALTH_TRENDS_CONTRACT.md`](RECOVERY_HEALTH_TRENDS_CONTRACT.md)

**Main test files:**
`tests/test_recovery_choices.py`, `tests/test_recovery_choice_mutations.py`,
`tests/test_recovery_fact_outcomes.py`, `tests/test_selected_workout_recovery.py`

**Authority boundaries:**
- Fresh Training Readiness is the only biometric with direct authority.
- HRV Status, Recovery Time, Body Battery are informational only.
- No biometric except fresh Training Readiness can automatically change the
  selected workout; every mutation requires a current Telegram confirmation.

**Intentional exclusions:**
- Body Composition (separate fixture-and-units contract gate).
- Biometric authority beyond fresh Training Readiness.
- Automatic deloads or workout reductions from metrics.

---

## Phase 4 — Progress and UI

**Status: complete**

**Goal:** Deterministic strength-progression proposals, web review/actions,
deduplicated Telegram notifications, 28-day recovery/health trends, Fitness Age,
VO₂ max, Training Status, weekly summary, and Ask Coach aggregate context.

**Implemented subphases:** 4A (contract), 4B (engine/persistence), 4C (web
review), 4D (Telegram notifications), 4E (trends), 4F (slow metrics), 4G
(weekly summary), 4H (Ask Coach context).

**Main production modules:**
`coach/strength_progression.py`, `coach/strength_progression_store.py`,
`coach/strength_progression_integration.py`,
`coach/strength_progression_notifications.py`,
`notify/weekly.py`, `coach/ask_coach_session.py`, `coach/advisory_aggregates.py`

**Main contracts:**
[`STRENGTH_PROGRESSION_CONTRACT.md`](STRENGTH_PROGRESSION_CONTRACT.md),
[`SOURCE_PROGRESSION_CONTRACT.md`](SOURCE_PROGRESSION_CONTRACT.md),
[`WEEKLY_SUMMARY_CONTRACT.md`](WEEKLY_SUMMARY_CONTRACT.md),
[`ASK_COACH_AGGREGATE_CONTEXT_CONTRACT.md`](ASK_COACH_AGGREGATE_CONTEXT_CONTRACT.md),
[`RECOVERY_HEALTH_TRENDS_CONTRACT.md`](RECOVERY_HEALTH_TRENDS_CONTRACT.md),
[`SLOW_METRIC_HISTORY_CONTRACT.md`](SLOW_METRIC_HISTORY_CONTRACT.md)

**Main test files:**
`tests/test_strength_progression.py`, `tests/test_strength_progression_store.py`,
`tests/test_strength_progression_integration.py`,
`tests/test_strength_progression_notifications.py`,
`tests/test_weekly_summary.py`, `tests/test_ask_coach_session.py`,
`tests/test_recovery_health_trends.py`, `tests/test_slow_metric_history.py`,
`tests/test_advisory_aggregates.py`

**Authority boundaries:**
- Progression evidence has no authority from Garmin RPE, Garmin Feel, subjective
  feedback, biometrics, readiness, or recovery metrics.
- Any progression change is limited to a local `SessionExercise.weight_kg`.
- It never automatically rewrites exercise structure or already scheduled Garmin workouts.
- Ask Coach context reads existing local stores only; no Garmin fetch, private
  calendar access, or workout authority.

**Intentional exclusions:**
- Custom readiness scoring.
- Biometric authority beyond fresh Training Readiness.
- Automatic progression approval.
- Automatic deloads.
- Medical/injury/nutrition recommendations.
- Private-calendar access by Ask Coach.

---

## Phase 5 — Source execution and longer-horizon program fidelity

**Status: complete**

**Goal:** Source-template supersets for `muscle_strength_5`, separate
between-exercise transition timers for `ppl_6`, deterministic source rep-goal
progression proposals for the six Powerbuilding PPL main lifts, and durable
source-duration review prompts.

**Implemented subphases:** 5A (supersets/transitions), 5B (source rep-goal
progression), 5C (source-duration review prompts).

**Main production modules:**
`coach/garmin_compiler.py`, `coach/programs.py`, `coach/program_policy.py`,
`coach/program_duration_review.py`, `coach/strength_progression.py`

**Main contracts:**
[`SOURCE_EXECUTION_FIDELITY_CONTRACT.md`](SOURCE_EXECUTION_FIDELITY_CONTRACT.md),
[`SOURCE_PROGRESSION_CONTRACT.md`](SOURCE_PROGRESSION_CONTRACT.md),
[`PROGRAM_DURATION_REVIEW_CONTRACT.md`](PROGRAM_DURATION_REVIEW_CONTRACT.md)

**Main test files:**
`tests/test_compiler.py`, `tests/test_programs.py`,
`tests/test_program_duration_review.py`,
`tests/test_strength_progression.py`

**Authority boundaries:**
- Source fidelity is future-only compilation; existing scheduled workouts are
  unchanged.
- Source rep-goal proposals require two local qualifying appearances and explicit
  web confirmation; no automatic or scheduled-workout mutation.
- Duration review prompts are neutral; a deload choice changes no workout or
  progression state.

**Intentional exclusions:**
- Automatic deloads.
- Automatic workout or progression mutations.
- Source-specific progression rules beyond Powerbuilding PPL 15-rep.

---

## Phase 6 — Operator safety and recovery

**Status: complete**

**Goal:** Read-only operator health, verified SQLite backups, guarded-restore
planning, configured-runtime staging, replacement/rollback/postcheck, and the
operator apply CLI.

**Implemented subphases:** 6A (health/backups), 6B1 (contract/threat model),
6B2A (planning/journal), 6B2B (offline staging), 6B2C (replacement/rollback
on fixtures), 6B3A (planning CLI + inspector), 6B3B1 (configured preparation),
6B3B2 (configured replacement/rollback), 6B3B3 (apply CLI).

**Main production modules:**
`operator_health.py`, `operator_storage.py`, `verified_backup.py`,
`guarded_restore.py`, `guarded_restore_staging.py`,
`guarded_restore_replacement.py`, `guarded_restore_configured.py`,
`guarded_restore_configured_staging.py`,
`guarded_restore_configured_replacement.py`,
`plan_verified_restore.py`, `inspect_restore_operation.py`,
`apply_verified_restore.py`

**Main contracts:**
[`GUARDED_RESTORE_CONTRACT.md`](GUARDED_RESTORE_CONTRACT.md),
[`GUARDED_RESTORE_RUNBOOK.md`](GUARDED_RESTORE_RUNBOOK.md),
[`OPERATOR_RECOVERY_CONTRACT.md`](OPERATOR_RECOVERY_CONTRACT.md)

**Main test files:**
`tests/test_guarded_restore_planning.py`,
`tests/test_guarded_restore_staging.py`,
`tests/test_guarded_restore_replacement.py`,
`tests/test_guarded_restore_configured.py`,
`tests/test_guarded_restore_configured_replacement.py`,
`tests/test_apply_verified_restore.py`,
`tests/test_inspect_restore_operation.py`,
`tests/test_plan_verified_restore.py`,
`tests/test_operator_recovery.py`

**Authority boundaries:**
- The application never automatically performs a production restore.
- Service start/stop is an explicit manual operator action; the restore engine
  never invokes `systemctl` or any service-management command.
- No configured database is mutated without explicit operator confirmation
  arguments.
- All locks must be acquired non-blockingly; any unavailable lock fails safe.

**Intentional exclusions:**
- Automatic or scheduled production restore.
- Web or Telegram restore controls.
- Restore while the application is running.
- Schema downgrade, repair, or automatic migration during restore.
- Production restore drill (not a completion requirement; production use requires
  separately approved operator action following the runbook).

---

## Summary

| Phase | Status |
|---|---|
| Phase 1 | complete |
| Phase 2 | complete |
| Phase 3 | complete |
| Phase 4 | complete |
| Phase 5 | complete |
| Phase 6 | complete |

**All six phases are complete.**

No additional biometric authority, automatic progression approval, automatic
deloads, medical/injury/nutrition recommendations, or production restore
execution is planned or has been implemented.
