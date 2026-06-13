# GarminCoach — Next Steps (Phases 2–4)

## Context

Phase 1 is done and polished: Garmin sync (token cache + MFA + rate-limit circuit
breaker), SQLite cache, a 2×2 trend dashboard with Fitness-Age/VO₂ tiles, and
type-aware workout pages (strength sets, cardio stats, HR zones). The codebase is
clean and the key extension points already exist:

- `metrics/engine.py::recompute_all()` — called after **every** sync
  (`sync_service.py`). Today it only fills `Activity.training_load`. **This is where
  Phase 2 plugs in.**
- Empty/unused tables already in `db.py`: **`DailyMetrics`** (readiness, acute/chronic
  load, ACWR, sleep_debt_h), **`Goal`** (goal + custom_input), **`CoachMessage`**
  (role, content, created_at, data_snapshot). Phases 2–3 fill these — **no schema
  redesign needed.**
- `MetricSnapshot` + `_tile()`/`_fitness_tiles()` in `app.py` establish the pattern
  for **dashboard tiles read instantly from the DB** (no live calls). Phase 2's
  readiness/ACWR tiles follow this exact pattern.
- `config.py` already reads `LLM_PROVIDER`, `OLLAMA_*`, `ANTHROPIC_API_KEY`,
  `CLAUDE_MODEL`. `coach/` is scaffolded but empty.

The two design rules from Phase 1 still hold: **zero required manual logging** (the
only manual input is the goal text), and **hybrid intelligence** — deterministic math
produces the numbers (Phase 2), the LLM only interprets them (Phase 3).

---

## Phase 2 — Metrics Engine (deterministic, no LLM, free)

**Goal:** turn raw rows into the daily scores that ground both the dashboard and the
coach. All pure Python over SQLite, recomputed inside `recompute_all()`, stored in
`DailyMetrics` (one row per day) — same inputs → identical outputs, instant, offline.

### What to build (in `metrics/engine.py`)

1. **Training load (refine existing).** Keep the per-activity `training_load`, but
   prefer **time-in-HR-zone** when available (the client already fetches and caches it
   via `garmin_client.hr_zones()`), falling back to the current avg-HR×duration TRIMP
   when zones are missing. Zone-weighted TRIMP (zone1×1 … zone5×5) is more accurate
   for stop-start sports like your soccer sessions.

2. **Acute / chronic load + ACWR.** For each day with data:
   - `acute_load` = mean daily load over trailing **7 days**
   - `chronic_load` = mean daily load over trailing **28 days**
   - `acwr` = acute / chronic (None if chronic is 0)
   Daily load = sum of that day's activities' `training_load`. Sweet spot ≈ 0.8–1.3;
   >1.5 flags a spike.

3. **Readiness score (0–100).** Blend, each scored against **your own rolling
   baseline** (not population norms):
   - overnight **HRV** vs trailing 60-day mean (heaviest weight)
   - **resting HR** vs baseline (lower = better)
   - **last night's sleep** vs target
   - **Body Battery** high/low
   Default weights HRV-led (~40/20/25/15) — documented as tunable constants at the top
   of the module. Missing inputs → renormalize over present ones, never crash.

4. **Sleep debt.** Accumulated `(target_hours − actual)` over a trailing window
   (target from a constant, default 8h).

5. **Strength progression.** Per-exercise volume (Σ sets×reps×weight) and best-set
   (max weight) over time, from `ExerciseSet`. Exposed as a query helper for the
   workout/dashboard UI; doesn't need a `DailyMetrics` column.

### Where it surfaces (UI)

- **Two new dashboard tiles** — **Readiness** (0–100, color-coded) and **ACWR** (with a
  "balanced / ramping / detraining" word), built exactly like `_fitness_tiles()` and
  rendered with the existing `.tile` CSS. Add a `_readiness_tiles()` reading the latest
  `DailyMetrics` row.
- **A readiness trend chart** in the existing 2×2 `chart-grid` (reuse the Chart.js
  `lineChart` helper already in `dashboard.html`).
- **Strength progression** on the strength workout page: a small "vs. last time" delta
  per exercise.

### Files

- `metrics/engine.py` — main work; add `recompute_daily_metrics(session)` called from
  `recompute_all()`. Keep functions pure + unit-testable.
- `app.py` — add `_readiness_tiles()`, pass to dashboard context.
- `templates/dashboard.html` — render new tiles + readiness chart.

### Verification

- Backfill is already present; after a sync, open the dashboard → Readiness & ACWR
  tiles populate.
- **Determinism check:** call `recompute_all()` twice → `DailyMetrics` identical.
- **Hand-check one day:** manually compute ACWR (7d vs 28d mean) and readiness for a
  recent date; confirm the stored values match.
- Add a tiny `tests/` with a few pure-function asserts (load, ACWR, readiness with a
  synthetic baseline) — fast, no network.

**Effort:** ~half a day. Self-contained, free, no new dependencies.

---

## Phase 3 — Coach (the only LLM use) — swappable Ollama/Claude

**Goal:** proactive daily suggestion + a chat that answers grounded in **your real
Phase-2 numbers**. The LLM never computes/recalls a number — all facts are
pre-computed and passed in the prompt (anti-hallucination), so even a small local model
stays accurate.

### What to build

1. **`coach/llm.py` — provider adapter.** One function
   `generate(system: str, user: str, history: list[dict]) -> str`. Routes on
   `config.LLM_PROVIDER`:
   - `ollama` (default, free): POST to `{OLLAMA_HOST}/api/chat` with the system prompt
     as a `role:"system"` message; model `OLLAMA_MODEL`.
   - `claude` (opt-in): `anthropic` SDK `messages.create`, system as the **top-level
     `system=`** param, `max_tokens` required, model `CLAUDE_MODEL`.
   Normalize both to a plain string; handle "provider unreachable" with a friendly
   message (e.g. "Start Ollama or set LLM_PROVIDER=claude"). Both deps are already in
   `requirements.txt`.

2. **`coach/snapshot.py` — fact builder.** Assemble a compact JSON snapshot from the DB:
   latest `DailyMetrics` (readiness, ACWR, sleep_debt), last 1–2 workouts incl.
   exercise sets, recent sleep/HRV/Body-Battery, and the active `Goal`. This is the
   single source of grounding facts for both features.

3. **`coach/coach.py` — the two features.**
   - **Daily suggestion:** after each sync (hook in `sync_runner` or end of
     `recompute_all()`), generate a short suggestion + one tip, store as a
     `CoachMessage(role="suggestion", data_snapshot=…)`. Shown atop the dashboard.
     Generate at most once/day (guard on `created_at`).
   - **Chat:** `POST /chat` takes a question, loads the snapshot + recent
     `CoachMessage` history, calls `generate()`, stores user+assistant turns. Render a
     simple chat panel (new route + template, or an HTMX section on the dashboard).

4. **Goal UI (the one manual input).** `GET/POST /goal` to edit `Goal.goal` +
   `custom_input` (single row id=1). Small form; this is intent the watch can't know.

### Files

- `coach/llm.py`, `coach/snapshot.py`, `coach/coach.py` (new)
- `app.py` — routes `/chat`, `/goal`, and a daily-suggestion read on the dashboard
- `templates/` — `chat.html` (or dashboard panel) + `goal.html`
- `.env.example` — confirm the LLM keys are documented (mostly present)

### Prerequisite & verification

- Prereq: `ollama pull llama3.1` (free) **or** set `LLM_PROVIDER=claude` + key.
- Verify a daily suggestion generates and **references your real numbers** (e.g. cites
  today's readiness/ACWR). Ask a chat question ("should I lift heavy tomorrow?") and
  confirm the answer uses your data, not generic advice.
- **Provider-swap check:** flip `LLM_PROVIDER` ollama↔claude; identical flow, no code
  change.

**Effort:** ~one day. Depends on Phase 2 (coach reasons over its outputs).

---

## Phase 4 — Run on Boot (ops, small)

**Goal:** the app starts automatically on the always-on machine; optional phone access
away from home.

- **Decide the host machine here** (Windows or Linux) — no design impact, deferred to
  now. `run.bat` already exists for Windows.
- **Linux:** a `systemd` user service running `…/.venv/bin/python app.py`, `Restart=on-failure`.
- **Windows:** Task Scheduler "At log on" → `run.bat` (or a service wrapper).
- **Optional:** Tailscale note in the README so the iPhone can reach it off-LAN; the
  app already binds `0.0.0.0`.
- **Verify:** reboot → dashboard reachable and the scheduler's auto-sync fires.

**Effort:** ~30 min.

---

## Recommended order & open decision

Build **2 → 3 → 4**. Phase 2 is free, self-contained, and makes Phase 3 genuinely
useful (the coach needs real readiness/load to reason about).

**One decision for Phase 2:** the readiness-score weighting. Default is HRV-led
(HRV 40 / sleep 25 / resting-HR 20 / Body-Battery 15), tunable via constants. Confirm
that default or state a preference (recovery-focused vs sleep-focused) before I build.
