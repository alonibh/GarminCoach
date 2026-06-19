# Changelog

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
