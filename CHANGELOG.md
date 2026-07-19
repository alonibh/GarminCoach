# Changelog

## 2026-07-19 - Morning Brief and Telegram Corrections

- Added sleep-start and wake-up times to deterministic morning briefings.
- Reconciled the morning state machine with already-delivered briefings so the
  11:30 deadline cannot prompt or emit a duplicate brief afterward.
- Reworded missing-data controls as a Garmin fetch retry instead of incorrectly
  asking the athlete to sync an already-synced watch.
- Enabled guarded chat routing by default so workout cancellation requests
  produce typed Telegram confirmation buttons.
- Added regressions for planned-workout rendering and safe schedule fallbacks.

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
