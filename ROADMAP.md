# GarminCoach Roadmap

Updated 2026-07-18. This roadmap follows the current product contract: concise
data-grounded coaching, deterministic authority for consequential decisions,
no metric-driven workout rewriting, and explicit user confirmation.

## Completed baseline

### Garmin data and web UI

- [x] Cached Garmin authentication with MFA and rate-limit protection.
- [x] Initial backfill, scheduled incremental sync, manual sync, and priority
  morning sync.
- [x] Activities, strength sets, sleep, HRV, resting heart rate, Body Battery,
  stress, steps, Fitness Age, VO2 max, and supported-device Garmin Training
  Readiness.
- [x] Type-aware workout detail, dashboard trends, monthly calendar, PWA assets,
  application authentication, HTTPS deployment, and public coach ICS feed.
- [x] Per-signal freshness and tri-state device-capability records.

### Program engine

- [x] Nine source-reviewed strength templates from two through six sessions.
- [x] Rolling sequence and recovery intervals without forcing weekday names.
- [x] Source-reviewed between-set rest timers and deterministic warm-up steps.
- [x] Reviewable program proposal, explicit activation, and editable sessions.
- [x] Structured Garmin strength workout compilation.
- [x] Completion reconciliation using Garmin workout provenance or a guarded
  unique fingerprint within the active program.
- [x] Program cursor that does not reset at week boundaries or accumulate missed
  sessions as debt.

### Deterministic coach and Telegram

- [x] Typed, persisted morning decisions with versioned evidence rules.
- [x] Garmin Training Readiness category handling on supported devices; no
  fallback composite score on unsupported devices.
- [x] Program-rest precedence and immutable workout content under metric-based
  warnings.
- [x] Immediate morning briefing plus 11:30 manual-sync/answer-anyway flow.
- [x] Versioned confirmation buttons, stale-action rejection, and atomic
  scheduling/rescheduling.
- [x] Durable outbox, 22:00-07:00 quiet hours, one-hour reminder, calendar
  conflict handling, late Poor-readiness update, and Saturday 20:00 summary.
- [x] Informational free-text chat separated from decision and mutation
  authority.

## Active priorities

### P0 — End-to-end operational verification

- [ ] Complete a real watch-to-Garmin-to-app morning cycle after a fresh Garmin
  login and record endpoint/freshness behavior for the connected Vivoactive 5.
- [ ] Exercise Telegram webhook, button confirmation, calendar mutation,
  reminder delivery, restart recovery, and idempotency in one production-like
  scenario.
- [ ] Verify a completed GarminCoach-generated strength workout advances exactly
  one active-program session and retains the linked activity audit trail.
- [ ] Add an operator-facing health view for authentication state, endpoint
  errors, queued notifications, last successful priority sync, and failed jobs.

### P1 — Source-template fidelity

- [ ] Add explicit superset groups so paired exercises compile and reconcile as
  pairs rather than straight sets.
- [ ] Separate between-set rest from between-exercise transition time; this is
  needed for the Planet Fitness PPL 45/90-second rule.
- [ ] Represent source tempos such as three-second negatives when Garmin workout
  steps can carry the instruction reliably.
- [ ] Decide how optional source components, such as the five-day program's ab
  session, should be exposed without implying they are mandatory.
- [ ] Add program-duration review and deload prompts only after each source rule
  and resulting user interaction are approved.

### P1 — Evidence coverage for devices without Training Readiness

- [ ] Review candidate rules for sleep, HRV trend, resting heart rate, and stress
  individually; define populations, required history, missing-data behavior,
  effect size, exclusions, evidence grade, and boundary tests.
- [ ] Add only rules that support a concrete decision without inventing a
  replacement readiness score.
- [ ] Keep unsupported-device behavior program/calendar-driven until a rule
  passes that review.

### P2 — Program progression

- [ ] Encode source-specific progression rules separately for every routine.
- [ ] Use only synced completed sets to prepare a proposed future target.
- [ ] Require confirmation and show the exact source rule and performance facts
  before changing a target weight.
- [ ] Never infer a weight increase from readiness or from a single failed set.

### P2 — Reliability and maintainability

- [ ] Replace deprecated FastAPI startup events with a lifespan handler.
- [ ] Add backup/restore documentation and automated SQLite integrity checks.
- [ ] Add structured operational logging without exposing Garmin tokens,
  calendar URLs, Telegram secrets, or health data.
- [ ] Add deployment smoke tests for application login, public ICS access, and
  authenticated private routes.

## Deliberately deferred

- Multi-user support. The current app remains single-user and does not add
  preparatory tenancy abstractions yet.
- Automatic source-program renewal, deload, or weight progression.
- Metric-adjusted alternate workouts or automatic reductions in sets, reps, or
  weights.
- Uploading recovery activities to Garmin.
- Nutrition, medical, injury-risk, emergency, or diagnostic recommendations.
- General cross-sport plan generation, race planning, and dynamically changing
  heart-rate zones.
- Strava integration and PDF report export.

Deferred items are not promises. They require a new product decision and, when
prescriptive, an evidence review before implementation.
