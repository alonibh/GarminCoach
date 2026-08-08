# Changelog

## 2026-08-06 - Supported runtime enforcement and routine fidelity audit

- Removed `GarminConnectNotFoundError` and `garminconnect.workout` compatibility
  shims from `coach/active_recovery.py`. Restored direct imports from the
  pinned `garminconnect[typed]==0.3.7` API.
- Removed module-level `_garminconnect_030_available` detection and
  dependency-based `pytest.mark.skipif` from `tests/test_active_recovery_workout.py`
  and `tests/test_recovery_choice_mutations.py`. Tests now execute unconditionally.
- Corrected curated-routine fidelity audit: added all missing source exercises to
  `planet_fitness_full_body_3`, `long_cycle_full_body_3`, `whole_body_toning_3`,
  `planet_fitness_upper_lower_4`, `optimized_volume_4`, `phul_4`,
  `barbell_no_rack_4`, and `muscle_mania_6`.
- Renamed `long_cycle_full_body_3` to "Long Cycle Full Body (Adapted) · 3 days"
  and classified as `garmin_adapted`; source auto-regulation progression is not
  implemented.
- Applied source-defined rest for `whole_body_toning_3` (45 s) and `muscle_mania_6`
  (60 s compound / 45 s isolation). Corrected 60 s for `optimized_volume_4` from
  incorrectly attributed ACSM default to GarminCoach product default.
- Rewrote `docs/CURATED_ROUTINE_AUDIT.md` with honest per-routine classifications,
  required source exercises, 2026-08-06 review dates, and corrected ACSM attribution.
  Added 9 audit-enforcement tests.
- Removed restore drill statement from `ROADMAP.md` and `docs/PHASE_STATUS.md`.
- Full suite: 1223 collected, 1222 passed, 1 skipped (Windows binary descriptor
  regression), 0 failures, 0 errors. Python 3.12, garminconnect 0.3.7.
- **Phase 1–6 status: all phases complete.**

## 2026-08-06 - Phase 1–6 final reconciliation

- Closed guarded-restore descriptor gaps: `_open_nf()` now surfaces `os.close()`
  failure as a bounded `ConfiguredReplacementPreconditionError` with the close
  error as `__cause__` and the original validation failure as `__context__`.
  Added 8 deterministic tests for this path.
- Replaced environment-dependent nondeterministic test skips with deterministic
  mocks: inode substitution uses `_MutatedStat` + `monkeypatch`; WAL sidecar
  test uses `_inject_wal_into_baseline_evidence` + `os.lstat` patch.
- Created `docs/CURATED_ROUTINE_AUDIT.md`: compact audit tables for all 25
  curated routines with source URL, review date, confirmed matches, Garmin
  adaptations, and unresolved facts. Added 10 regression tests.
- Replaced all first-party `datetime.utcnow()` usages with `db.naive_utc()`
  helper across `db.py`, `app.py`, and three coach modules. Added 5 boundary
  tests.
- Added `GarminConnectNotFoundError` and `garminconnect.workout` compatibility
  shims; 54 tests that require garminconnect>=0.3 now skip gracefully with a
  documented platform-specific reason.
- Created `docs/MAINTENANCE_NOTES.md` documenting optional architectural
  restructuring for `sync/sync_service.py` and
  `guarded_restore_configured_replacement.py` that was deferred.
- Documentation reconciliation: updated `ROADMAP.md`, `PHASE_STATUS.md`, all
  phase contracts, and `GUARDED_RESTORE_CONTRACT.md` to remove stale
  future-tense language; created `docs/PHASE_STATUS.md` as the canonical
  per-phase status summary.
- **Phase 1–6 status: all phases complete.**

## 2026-08-01 - Guarded restore staging verification

- Added fixture-only offline staged-copy and strict SQLite verification through
  `REPLACEMENT_READY`, with no destination replacement or rollback.

## 2026-08-01 - Guarded restore journal invariants

- Bound journal identity to its operation path, made update timestamps monotonic,
  and preserved partial rollback facts for manual recovery.

## 2026-08-01 - Guarded restore planning foundation

- Added Phase 6B2A immutable planning, confirmation hashes, strict journal
  state machine, atomic journal persistence, and dedicated restore lock only.

## 2026-08-01 - Guarded restore lock lifecycle

- Corrected the Phase 6B1 design so the public safety backup owns its own
  non-reentrant backup lock before restore takes the long-held backup lock.

## 2026-08-01 - Guarded verified restore design

- Added the Phase 6B1 threat model and guarded-restore contract; no restore
  engine or mutation is implemented.

## 2026-08-01 - Operator recovery acceptance

- Added bounded backup-artifact and lock permission health inspection.

## 2026-08-01 - Operator backup verification hardening

- Split runtime and maintenance discovery, reject symlink/path escapes, and
  require complete manifest, mapping, target-set, and provenance semantics.

## 2026-08-01 - Operator recovery final safety

- Added explicit runtime-mode/target-set manifests and exact current schema,
  migration, mapping, and package compatibility before a dry-run restore plan.

## 2026-08-01 - Operator health and verified backups

- Added fail-closed SQLite integrity preflight, read-only health diagnostics,
  explicit verified online backups, and verification-only restore planning.

## 2026-08-01 - Source-duration review prompts

- Added durable, neutral source-duration review points for eligible curated
  programs, with exact local match-cycle context, web-only decisions, and
  outbox-only notification delivery.

## 2026-08-01 - Source rep-goal progression

- Added deterministic, web-confirmed source rep-goal proposals for the six
  Powerbuilding PPL main lifts.
- Hardened source-rule validation, incomplete-set audit totals, and read-only
  editor/review presentation.

## 2026-08-01 - Source execution fidelity

- Added PPL transition timers for future Garmin compilation, with guarded
  tenant migration.

## 2026-07-22 - Missing-Program Morning Brief Recovery

- Deferred pre-11:30 morning briefs when no active program is temporarily
  available instead of finalizing a misleading `NO_ACTION` recommendation.
- Added one idempotent, clearly labelled update when a program becomes
  available after an earlier `NO_ACTION` brief.
- Re-evaluate the morning recommendation immediately after program approval.

## 2026-07-22 - Morning Brief Metrics and Menu Layout

- Kept available sleep and readiness facts in positive workout morning briefs
  instead of rendering only the workout proposal.
- Moved `My calendar` beside `Start Garmin sync` on the Telegram command menu.
- Kept completed Garmin workouts with coach-prefixed names visible in Telegram's
  recent-activity history while still hiding legacy schedule placeholders.

## 2026-07-22 - Two-Day Specialty Routine

- Added the source-audited intermediate 100-Rep Full Body Shocker as a
  four-week, two-session specialty routine with Garmin-compatible exercise
  mappings and its intra-set pause protocol preserved in exercise notes.
- Tightened catalog eligibility so focus labels and arm isolation cannot stand
  in for twice-weekly lower-body, pressing, and back-pulling exposures.

## 2026-07-21 - AthleteData-Informed Coach Guardrails

- Added on-demand rolling program-state and recorded lift-progression views;
  target weights remain athlete-approved and never change automatically.
- Added an informational short-session view based on configured primary
  movement patterns without replacing or scheduling the approved full workout.
- Confirmed safety reports now block workout timing, scheduling, and
  rescheduling until the athlete explicitly confirms closing the report.

## 2026-07-20 - Button-First Telegram Scheduling

- Fixed guided replies such as "Today at 18:30" so date and time are consumed
  together instead of asking for an already supplied time again.
- Added state-bound date and time buttons, seven eligible date choices, complete
  valid-time lists with paging, and Back/Cancel controls.
- Preserved unfinished flows after unsupported replies, rejected stale or
  duplicate controls, and added exact workout labels for ambiguous changes.
- Versioned the deterministic router as `closed-catalog-v2` and added transition,
  button, completion, abandonment, stale-control, and repeated-prompt metrics.

## 2026-07-19 - Natural Reminders and Cancellation Language

- Reworded the one-hour alert as a natural workout reminder that names the
  upcoming workout and says it starts one hour from now.
- Added `delete` to the shared deterministic cancellation vocabulary while
  preserving fail-closed handling for negated requests such as "don't delete".

## 2026-07-19 - Closed Deterministic Telegram Catalog

- Removed runtime AI classification and informational chat generation in favor
  of a reviewed English interaction grammar and deterministic response templates.
- Added fail-closed negation handling, a persistent action menu, semantic
  dialogue revalidation, and adversarial routing regression coverage.
- Standardized workout proposals on approve/reject/set-another-date and
  cancellations on keep/cancel; removed skip operations from Telegram.
- Changed the 11:30 deadline to automatically send a clearly labeled
  best-effort workout/rest brief and retained material late-data corrections.

## 2026-07-19 - Morning Brief and Telegram Corrections

- Added sleep-start and wake-up times to deterministic morning briefings.
- Reconciled the morning state machine with already-delivered briefings so the
  11:30 deadline cannot prompt or emit a duplicate brief afterward.
- Reworded missing-data controls as a Garmin fetch retry instead of incorrectly
  asking the athlete to sync an already-synced watch.
- Enabled guarded chat routing by default so workout cancellation requests
  produce typed Telegram confirmation buttons.
- Added regressions for planned-workout rendering and safe schedule fallbacks.
- Preserved legacy full-body session-name aliases in rest-period migrations so
  renamed templates still migrate existing rows correctly.

## 2026-07-19 — Guarded Semantic Chat Router

- Added AI-first, closed-schema intent classification with verbatim evidence
  validation, typed clarification state, audit records, and shadow rollout.
- Kept workout decisions and all mutations deterministic and confirmation-only.
- Added approve, different-time, and dismiss controls to schedule proposals.
- Added guarded sync and cancellation actions, including verified Garmin
  unscheduling before local cancellation.
- Prevented actionable Telegram messages from silently losing their buttons.

## 2026-07-18 — Deterministic Coach Redesign

- Added source-audited policies and rolling completion reconciliation for all
  nine supported program templates; removed the incomplete Get RIPPED program.
- Added capability-aware priority sync, observation freshness, and the durable
  11:30 manual-sync or answer-anyway morning flow. Vívoactive 5 is identified
  from Garmin device metadata as not supporting Training Readiness.
- Replaced LLM workout decisions with a persisted evidence-rule engine using
  Garmin Training Readiness categories and program-rest precedence.
- Added versioned Telegram confirmation actions; metrics never rewrite workout
  contents, and stale buttons cannot mutate program or calendar state.
- Added a durable notification outbox, quiet hours, one-hour reminders,
  Saturday summaries, calendar-conflict handling, and daytime Poor-readiness
  corrections. ACWR remains descriptive UI data only.
- Re-audited between-set rest guidance for all nine source routines and aligned
  their Garmin timers, including phase- and exercise-specific rules. Existing
  untouched source defaults migrate once; customized rest values are preserved.
- Reconciled the README, roadmap, next-steps guide, architecture, metrics
  reference, routine audit, research disclaimers, and environment example with
  the implemented deterministic coach. Corrected the documented sync setting
  from obsolete `AUTO_SYNC_HOURS` to `AUTO_SYNC_TIMES`.

## 2026-06-29 — Telegram Integration & Proactive Notifications

- **Telegram Integration:** Built a complete Telegram bot integration (`notify` module).
- **Proactive Push Notifications:** AI Coach now sends proactive daily coaching messages and recovery alerts to Telegram via a background cron scheduler.
- **Interactive Chat Buttons:** Users can now click 'Approve' or 'Dismiss' inline buttons directly in Telegram to schedule or discard suggested workouts.
- **Typing Indicators:** Added native "typing..." action in Telegram while the AI Coach is generating a workout to improve UX.
- **Prompt Formatting Constraints:** Tuned the LLM prompts to produce concise, 3-paragraph summaries (Condition, Calendar, Routine).
- **Hallucination Fixes:** Converted numeric `days_since_last_trained` dict into a static text log in `snapshot.py` to prevent the LLM from hallucinating fake workout history based on past un-executed suggestions.
- **Sync Fixes:** Resolved an `UnboundLocalError` in the Garmin background sync loop and corrected database column references in the rules engine.
- **Agent Guidelines:** Created `.agents/AGENTS.md` to persist behavior rules (like automatic commits) for AI coding assistants.

## 2026-06-20 — Science-based formula overhaul + cleanup

Every computed metric was audited against published sports-science literature
(see `docs/METRICS.md` for formulas and citations). Invented formulas were
replaced with cited ones; where no single validated formula exists, the choice
is now explicitly labelled a documented heuristic.

### Formula changes (`metrics/engine.py`, `app.py`)
- **Training load:** removed the invented `minutes × (avg_hr/100)^2.7`. Now uses
  **Banister TRIMP** (HR-reserve based, gender constants, Tanaka HRmax fallback)
  when HRrest/HRmax are known, else **Edwards zone TRIMP** when zone seconds are
  present, else `None` (no invented number). Wired HRrest/HRmax/gender from the
  synced profile into `recompute_all`.
- **ACWR:** fixed an **off-by-one bug** — the EWMA loop started at `i=1`, so
  *today's* load was never counted and every ratio was a day stale. Now starts
  at `i=0`. Relabelled `detraining → underload`, `ramping → elevated`, and
  documented the thresholds as heuristic.
- **Readiness:** replaced the arbitrary piecewise slopes (280/200/400) and
  60-day baseline with **z-scores vs a 7-day personal baseline**, mapped via
  `50 + 50·tanh(z/2)`. Weights changed to `0.50 HRV / 0.25 RHR / 0.25 Sleep`;
  **dropped Body Battery** from the composite (it double-counts HRV). Sleep
  sub-score now blends duration + efficiency.
- **Sleep debt:** removed the unfounded `× 0.8` exponential decay. Now a linear
  cumulative deficit with target **7.0 h** (was 7.5), window **7 days** (was 14),
  cap **14 h** (was 30); nights with no data are excluded, not imputed as 0.
- **VO₂max norms:** replaced incorrect category boundaries (4–6+ units below the
  real values, only 4 age bands) with the **verified Cooper Institute table**
  (6 age bands, both sexes). On missing age/sex, the raw value shows with no
  category instead of defaulting everyone to a 28-year-old male.
- **Strength:** added **Epley estimated-1RM** per exercise and changed
  progression to compare against the best e1RM over the last 5 sessions
  (was a single-prior-session delta). Volume load (tonnage) was already correct.

### Bug fixes
- `metrics/engine.py`: `Activity.start_time is not None` (a Python identity
  no-op filter) → `Activity.start_time.isnot(None)`.
- `config.py`: refuse to start when app auth is enabled but `SESSION_SECRET` is
  still the default placeholder (forgeable cookies).
- `.env.example`: documented `AUTH_USERNAME/AUTH_PASSWORD` but the code reads
  `APP_USERNAME/APP_PASSWORD` — following the example silently disabled auth.
  Names aligned; added the missing Gemini provider keys.
- `coach/coach.py`: the daily-suggestion prompt referenced a `today_schedule`
  field that doesn't exist in the snapshot → pointed at `upcoming_schedule_14_days`.
- `app.py`: hardened `_time_ago` / timestamp normalization (naive-vs-aware
  datetime mixing and a brittle `"-" in val[-6:]` offset sniff that
  false-matched on date hyphens).

### Removed (dead code / files)
- `scratch_cal.py` (orphaned; **leaked a personal iCloud calendar URL** — rotate
  that URL), `backfill.py`, `run_sync.py`, `fix_db.py` — all standalone, imported
  by nothing.
- `WeeklySummary` model / `weekly_summaries` table — never read or written.
- `setup.sh`: removed the `python3 migrate.py` step (no such file; schema is
  created/migrated on startup).
- `ollama>=0.3` dependency (the code calls Ollama via raw `requests`).

### Tooling / docs
- Rewrote `tests/test_engine.py` against the new formulas (29 tests, all pass);
  the suite is the executable spec for `docs/METRICS.md`.
- Added `pytest` to `requirements.txt` and a CI **test** job (gating deploy) in
  `.github/workflows/deploy.yml`.
- Added `docs/METRICS.md` (per-metric formulas + citations + heuristic tags).
