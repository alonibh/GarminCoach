# Program-duration review contract

Phase 5C creates a neutral **source-duration review point** for exactly these curated policy keys: `dumbbell_full_body_3` (8 weeks), `phul_4` (12), `dumbbell_upper_lower_4` (12), `barbell_no_rack_4` (8), `barbell_upper_lower_4` (10), `maul_5` (12), `dumbbell_split_5` (12), `built_different_ppl_6` (10), and `muscle_mania_6` (10). The reviewed `ProgramPolicy.source_duration_weeks` is the sole duration authority; `NULL` and invalid metadata are ineligible.

## Eligibility, anchor, and durable state

An eligible program is the one unambiguous current active row (`active=true`, `status=active`) resolving to one curated policy with its exact ordered source-session identity. Only `is_custom=true` and `is_addon=true` sessions may coexist and are excluded from source totals. A custom standalone session, custom replacement, non-custom add-on, or any source identity mismatch fails closed and supersedes any live review without repairing the program.

The anchor is `TrainingProgram.activated_at`; only when it is absent may the same program's matching `ProgramCursor.created_at` be used. Naïve database datetimes are UTC; aware values retain their instant. The instant is converted with the athlete-local timezone helper, then `due_on = activated_local_date + timedelta(weeks=source_duration_weeks)`. There is no grace period.

`ProgramDurationReview` snapshots program ID/name/key, policy and duration, activation and local due dates, source-session count, and a SHA-256 fingerprint. The fingerprint includes only program ID, key, policy version, duration, activation instant, and ordered source session IDs/names/roles/custom flags. Its unique idempotency key permits one review per activation fingerprint. Statuses are `scheduled`, `pending`, `snoozed`, `resolved`, and `superseded`; decisions are only `continue_unchanged` and `deload_planned`.

The first local reconciliation backfills an existing active program once: `scheduled` before its due date and `pending` on/after it, with `first_due_at` recorded when it is first observed due. Repeated runs and concurrent insertion reuse the same durable row. A resolved fingerprint never reopens. Replacement, deactivation, or invalidation supersedes only active review state; a genuine reactivation or source-session reset has a new fingerprint and duration window.

## Context, web choices, and notification

The authenticated program page may display factual local context from exact `ActivityProgramMatch` rows for the current review's source session IDs at or after its activation: `matched_source_sessions`, `source_session_count`, `completed_source_cycles = matches // source_session_count`, and the remainder. Custom sessions, planned-only rows, and unrelated programs do not count. This context is not adherence, performance, or completion evidence: elapsed time does not mean the program is complete.

The only web decisions are continue unchanged, record that a deload/recovery week is planned, or snooze for seven local days; edit and choose-another-routine are navigation only. Every POST reloads tenant-local review and active-program facts and rejects stale state. `deload_planned` records athlete intent only: GarminCoach did not change workouts or schedule. GET routes do not create, resolve, snooze, or notify.

Daily athlete-local maintenance reconciles review state even without Telegram linkage; the minute outbox poller is delivery-only. The existing athlete-scoped outbox is the only notification path. A pending review yields one deterministic event per `review_id + reminder_sequence`; a snooze increments the sequence and clears only that sequence's delivery state. Concurrent insertion reuses its unique idempotency key. Delivery revalidates the review, sequence, active program, and fingerprint and settles stale rows without sending. Telegram has no action buttons, source URL, internal IDs, activity detail, biometric data, or calendar data.

## Authority and privacy boundaries

This feature makes no readiness, sleep, HRV, recovery, load, injury, calendar, or AI decision. It does not mutate a workout, `TrainingProgram`, `ProgramCursor`, `PlannedSession`, Garmin workout ID, scheduling interaction, activity match, exercise prescription, or progression evidence/proposal. It is excluded from Ask Coach context/consent, weekly aggregates, and morning logic. All reads and actions use the existing tenant context; snapshots and Telegram text are bounded and sanitized.

## Acceptance criteria

Tests cover policy validation, UTC/aware and DST due dates, migration ledger idempotency, eligibility and replacement backfill, exact match-cycle counts, web action idempotency/staleness, outbox deduplication and delivery suppression, tenant isolation, and the absence of cursor, scheduled-session, Garmin, and progression mutations.
