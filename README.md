# GarminCoach

GarminCoach is a personal, single-user Garmin training assistant. It syncs
Garmin data into a local SQLite database, presents the data in a FastAPI web
app, manages a source-reviewed strength program, and sends concise Telegram
recommendations through a deterministic evidence-rule engine.

The product is deliberately not a simulated human coach. It communicates in a
cold, short, factual style; explains the decisive data; and requires explicit
confirmation before scheduling, cancelling, or rescheduling a workout.

## Goals

1. Turn Garmin observations and the selected source program into one short,
   actionable daily answer without overwhelming the athlete.
2. Consider the complete available context—including program recovery rules,
   planned sessions, calendar constraints, observation history, device support,
   and relevant personal facts—without converting demographics into broad
   assumptions.
3. Make every consequential recommendation traceable to synced facts and a
   reviewed rule, with explicit missing-data behavior.
4. Preserve athlete authority: the system warns or advises, while the athlete
   confirms scheduling and may choose the unchanged original workout.
5. Eliminate unnecessary manual logging by obtaining training performance from
   Garmin sync whenever the watch recorded it.

## Product contract

- Recommendations use synced observations, their freshness and provenance,
  device capabilities, the active program, calendar state, and versioned rules.
- Telegram chat uses a closed deterministic catalog. A language model does not
  route, fill slots, answer, or execute chat requests.
- Metrics never silently change exercises, sets, repetitions, or target
  weights. Low readiness adds a warning; Poor Garmin readiness recommends rest
  while keeping the approved program workout pending.
- Garmin is the source of completed sets, repetitions, weights, and available
  RPE. The bot never asks for manual post-workout logging and sends no proactive
  post-workout questionnaire.
- ACWR is descriptive UI data only. The retired custom readiness composite is
  neither computed nor shown. Supported devices use Garmin Training Readiness;
  unsupported devices show individual observations without a synthetic score.
- This is not a medical device and does not diagnose illness, injury, or
  recovery status.

The complete authority model and decision order are documented in
[`docs/coach_product_architecture.md`](docs/coach_product_architecture.md).

## Implemented capabilities

### Garmin sync and data quality

- Cached Garmin authentication with interactive password/MFA only when needed.
- Initial history backfill plus scheduled and manual incremental sync.
- Activities, strength exercise sets, sleep, HRV, resting heart rate, Body
  Battery, stress, steps, VO2 max, Fitness Age, and Garmin Training Readiness
  when the connected device supports it.
- Priority morning sync fetches briefing-critical overnight observations first;
  slower history and workout synchronization continues afterward.
- Per-signal freshness states distinguish fresh, pending, stale, missing,
  endpoint errors, and unsupported device capability. Missing data alone never
  proves that a device lacks a metric.
- Garmin rate-limit cooldowns and a shared guard prevent overlapping manual and
  scheduled syncs.

### Web application

- Dashboard trends for sleep, HRV, resting heart rate, and steps.
- Garmin readiness score and official category on supported devices; a clear
  unsupported-device state otherwise.
- Descriptive ACWR tile, Fitness Age, VO2 max, and cached sync status.
- Type-aware activity pages: strength sets and volume; cardio distance, pace,
  speed, elevation, laps, intensity minutes, training effect, and HR zones when
  those fields apply.
- Monthly calendar combining completed activities and planned sessions.
- Public read-only ICS feed for confirmed coach workouts.
- Optional application login with signed session cookies.

### Strength programs and Garmin workouts

- Ten selectable Muscle & Strength routines covering two through six training
  days per cycle.
- Source-reviewed exercise order, rolling session sequence, required recovery
  intervals, optional recovery guidance, exclusions, and between-set timers.
- Source ranges resolve to concrete Garmin timers using the documented upper-
  bound convention; timers can be skipped early on the watch.
- Warm-up steps are added deterministically by movement and joint exposure.
- Program proposals are reviewable and undated. Approval activates the rolling
  program cursor; each actual workout date/time still requires confirmation.
- Structured strength workouts compile to Garmin exercise identifiers with
  sets, repetitions or duration, weight when known, warm-ups, and rest steps.
- Synced activities reconcile to the active program by exact Garmin workout
  provenance or a guarded unique exercise fingerprint. Unrelated routines do
  not advance the cursor.
- Source templates can be edited, but metric-driven decisions never rewrite
  them. Customized rest timers are preserved by catalog migrations.

The complete ten-routine audit is in
[`docs/routine_source_audit.md`](docs/routine_source_audit.md).

### Deterministic Telegram coach

- Sends the morning result as soon as required data is available.
- At 11:30 local time, the bot stops waiting and automatically sends a clearly
  labeled best-effort workout/rest briefing that names missing data.
- Applies program eligibility before biometrics: a source-required rest day is
  not overridden by a readiness metric.
- Garmin Training Readiness categories have limited authority: Prime, High, and
  Moderate do not change the session; Low adds a warning; Poor recommends rest
  without marking the program workout skipped.
- On devices without Garmin Training Readiness, the coach does not invent a
  replacement score. Until individual-metric rules are separately approved,
  program and calendar rules determine workout eligibility.
- Source-approved, evidence-reviewed recovery activity may be suggested
  verbally on a rest day. It is never uploaded to Garmin.
- No evening workout is created before the following night's sleep and morning
  observations are available.

### Confirmations and notifications

- Telegram buttons carry exact pending actions, expiry, and program/sync/
  calendar versions. Every click reloads and revalidates current state.
- Guided scheduling offers state-bound buttons for eligible dates and all valid
  times, with paging, Back, and Cancel controls. Stale controls cannot alter a
  newer conversation.
- Free text may initiate a scheduling or change request; a button confirmation
  is still required before mutation. A generic “yes” never executes the latest
  message.
- One-line pre-workout reminder exactly one hour before the confirmed start.
- Deterministic weekly summary every Saturday at 20:00 with matched program
  completion, unmatched strength activity, missed sessions, synced progression,
  sleep comparison, safety reports, and next-session state when available.
- Calendar conflicts offer `Keep workout`, `Set another date`, and
  `Cancel workout` actions.
- Notifications are stored in a durable SQLite outbox, deduplicated, retried,
  and deferred during 22:00-07:00 quiet hours.
- A late same-day update is sent only when fresh Garmin readiness materially
  changes a prior recommendation to Poor.

### Closed deterministic chat routing

- A reviewed English alias grammar maps free text into a closed interaction
  catalog. Negated, ambiguous, and unsupported messages fail closed.
- Deterministic handlers resolve every referenced session, date, metric, and
  calendar slot. All state changes require versioned Telegram confirmation buttons.
- The router has no AI or rollout mode. Unsupported messages show the persistent
  menu when idle and redraw the current choices during an active guided flow.
- Incomplete requests use typed, semantically revalidated dialogue state and
  merge every recognized slot from each reply. Combined date/time replies
  advance directly to confirmation. A generic text
  reply such as “yes” never approves an action.

## Technical architecture

- Python 3.12
- FastAPI and Jinja2
- SQLAlchemy 2.0 with SQLite
- APScheduler for local jobs
- `garminconnect` for Garmin Connect synchronization
- Chart.js for dashboard charts
- Telegram Bot API for messages and confirmation buttons
- Optional Ollama, Claude, or Gemini provider for legacy non-chat generation

Important modules:

| Area | Module |
| --- | --- |
| Garmin client and synchronization | `sync/garmin_client.py`, `sync/sync_service.py`, `sync/sync_runner.py` |
| Freshness and derived metrics | `metrics/freshness.py`, `metrics/engine.py` |
| Program templates and policy | `coach/programs.py`, `coach/program_policy.py`, `coach/program_state.py` |
| Decisions and interactions | `coach/decision_engine.py`, `coach/renderer.py`, `coach/interactions.py` |
| Garmin workout compilation | `coach/garmin_compiler.py` |
| Telegram and durable notifications | `notify/telegram.py`, `notify/morning.py`, `notify/outbox.py`, `notify/weekly.py` |

Production runs exactly one application worker and one service replica
(`APP_WORKER_COUNT=1`, Uvicorn `--workers 1`). The local `garmincoach.lock`
advisory lock prevents a second process on the same host; it cannot detect a
replica on another host, so deployment verification must also confirm one
running service replica.

## Setup

```bash
cd garmincoach
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

On Windows, install Python 3.12 and use `.venv\Scripts\pip` and
`.venv\Scripts\python` instead.

At minimum, set `GARMIN_EMAIL`. Password and MFA are entered through the app on
first connection and are not written to `.env`; Garmin tokens are cached in
`GARMIN_TOKEN_STORE`.

For Telegram coaching, set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and a
strong `TELEGRAM_WEBHOOK_SECRET`. `ICS_CALENDAR_URL` is optional and may point
to a private iCloud or Google Calendar ICS feed.

If `APP_USERNAME` is set, also set `APP_PASSWORD` and replace the default
`SESSION_SECRET`; the app refuses to start with authentication enabled and the
placeholder signing secret.

Start the app:

```bash
.venv/bin/python app.py
```

Open `http://localhost:8000`. The app binds to `0.0.0.0` by default, so a private
network such as Tailscale can provide remote access without changing the app.

## Scheduled behavior

- `AUTO_SYNC_TIMES` controls normal incremental sync times and accepts
  comma-separated `HH:MM` values; the default is `19:00`.
- Morning readiness polling runs every 15 minutes from 07:00 through 12:00 by
  default and stops after a briefing is sent.
- The missing-data decision deadline is fixed at 11:30 local time.
- Weekly summary is fixed at Saturday 20:00 local time.
- The durable notification outbox is checked every minute.

See [`.env.example`](.env.example) for all supported settings.

## Explicit database reset

Database corruption never triggers an automatic wipe. An operator may perform a
complete reset only while the service is stopped and only with the destructive
confirmation flag:

```bash
sudo systemctl stop garmincoach
.venv/bin/python scripts/reset_all_databases.py --confirm-destroy-all-data
sudo systemctl start garmincoach
```

The command discovers the configured control, single-user, and canonical tenant
databases; moves existing SQLite files and sidecars into a timestamped
`database_quarantine/` directory; recreates clean schemas through the normal
migration and initialization functions; and requires every new database to pass
`PRAGMA integrity_check`. Quarantined corrupt files are retained for forensics
but are explicitly not valid backups.

## Current limitations

- Single-user only; no multi-user preparation is intentionally included yet.
- Garmin Connect access uses an unofficial library and can require dependency
  updates when Garmin changes its authentication flow.
- The Vivoactive 5 does not provide Garmin Training Readiness, so the app
  displays individual recovery observations and does not fabricate a fallback
  readiness number.
- Strength details exist only when Garmin recorded them. Manual UI correction
  remains available for a misdetected set, but the Telegram bot never requests
  manual set logging.
- The five-day source program's supersets and PPL's separate between-exercise
  transition timer are not yet represented structurally; current templates use
  straight sets and one rest field per exercise.
- Program duration, deload prompts, and source progression rules are metadata
  only. The system does not automatically increase target weights.
- Optional LLM answers are informational and may be unavailable without a
  configured provider; deterministic coaching and notifications continue to
  work without them.

## Documentation

- [`docs/coach_product_architecture.md`](docs/coach_product_architecture.md) — product contract, authority boundaries, decisions, sync, and notifications
- [`docs/routine_source_audit.md`](docs/routine_source_audit.md) — all ten routine sources, scheduling rules, recovery guidance, and rest timers
- [`docs/METRICS.md`](docs/METRICS.md) — implemented formulas, evidence status, and product authority
- [`ROADMAP.md`](ROADMAP.md) — completed milestones, active priorities, and deferred scope
- [`CHANGELOG.md`](CHANGELOG.md) — chronological implementation history

The AthleteData documents under `docs/` are dated research artifacts. They
record product discovery input and must not be read as current GarminCoach
capabilities or policy.
