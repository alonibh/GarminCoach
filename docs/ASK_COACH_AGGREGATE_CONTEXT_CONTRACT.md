# Ask Coach aggregate context contract

`ask-coach-v3` is the privacy-bounded, advisory-only local context supplied to
Google Gemini when an athlete has explicitly consented to
`ask-coach-v2` / `ask-coach-categories-v2`. GarminCoach sets interaction
storage to disabled. This does not make a claim about Google's processing.

## Sources, dates, and authority

The builder reads only the bound tenant's existing local `Activity`,
`ExerciseSet`, `ActivityProgramMatch`, `DailyHealth`, `Sleep`, program/cursor,
planned-session, decision, profile, capability/freshness, and approved Phase
4E/4F read models. It performs no network, Garmin, Gemini, private-calendar,
sync, scheduler, notification, compiler, decision, or mutation operation.

The supplied UTC generation instant is converted to the tenant timezone (UTC
on invalid/missing timezone). `local_day` is explicit. Windows are inclusive:
recent seven days is day-6..day, prior seven is day-13..day-7, and recent 28
is day-27..day. Future observations are excluded. Overnight facts use existing
readiness; full-day facts never pretend a partial day is complete. Capability,
freshness, and analytical coverage remain separate labels.

The model receives facts only. It cannot select, schedule, modify, upload,
cancel, complete, progress, or otherwise mutate workouts, programs, calendars,
or Garmin data. Dynamic names and conversation text are untrusted data, never
instructions. Context and model output are never logged as health text, and
the context is never persisted.

## Exact schema and bounds

Top-level serialization order is: `snapshot_version`,
`privacy_contract_version`, `generated_at`, `timezone`, `date_context`,
`official_recommendation`, `data_freshness`, `profile`, `current_recovery`,
`training_aggregates`, `recovery_trends_28_days`, `slow_fitness_summary`,
`recent_activity_facts_7_days`, `active_program`, and
`planned_sessions_next_7_days`. Versions are `ask-coach-v3` and
`ask-coach-aggregate-context-v1`.

Current recovery wrappers are exactly `{value, observed_at, capability,
freshness}`. Capability is `supported`, `unsupported`, `unknown`, or
`not_applicable`; freshness is `fresh`, `stale`, `missing`,
`expected_pending`, `error`, or `unknown`. Non-fresh values are absent rather
than leaked from old rows. Recovery trends reuse Phase 4E's unchanged 7/21
median, direction, and coverage semantics. Slow fitness facts reuse Phase 4F
current/previous facts for Fitness Age, running/cycling VO2, and current-device
Training Status only; legacy VO2 is excluded.

Training contains aggregate counts, valid-duration counts, domain counts,
program completion, unmatched strength, and movement totals/coverage for the
three windows. Strength uses Phase 4G exact identity, active/work sets, exact
reps, and 250-gram half-up display rounding; it has at most three highlights.
Recent activity facts have no title, ID, source, calories, HR, load, GPS, lap,
split, sample, or route and are capped at five. Planned sessions are capped at
ten and next-session exercises at twenty; both exclude notes. Dynamic strings
are control-stripped and capped at 96 characters (exercise labels at 64).

No raw series, chart points, historical lists, arbitrary activity title,
free-form note, private-calendar event, endpoint/source key, fingerprint,
ACWR, acute/chronic load, custom readiness/recovery/health/fatigue/injury score,
or mutation authority is serialized. The current-session conversation remains
outside this snapshot.

Serialization is compact JSON with a non-overridable 16,000-character ceiling.
If necessary it removes, deterministically: recent activity facts, planned
sessions, next-session exercises, strength highlights, recovery aggregates,
then slow-fitness aggregates. Mandatory schema/decisive facts survive; a
snapshot whose mandatory facts cannot fit fails before provider use.

## Consent, revocation, and isolation

Canonical category identifiers are stored sorted as JSON with their SHA-256
hash; Telegram shows only the human descriptions. Re-consent stores the exact
version, provider, category version, canonical JSON, and hash. Old rows are
never migrated or auto-consented. Revocation immediately closes the active
session. Consent is checked before snapshot work, immediately before Gemini,
and before every output chunk; invalid/revoked in-flight work is not delivered
or recorded. Retries require valid current consent.

The canonical categories are: `profile.coaching_relevant`,
`data.freshness_and_coverage`, `recommendation.official`,
`recovery.current_facts`, `recovery.trends_28_days_aggregate`,
`training.activity_movement_aggregates_28_days`,
`training.strength_highlights_14_days`,
`training.recent_activity_facts_7_days`, `fitness.slow_metrics_aggregate`,
`program.active_structure`, `sessions.garmincoach_next_7_days`, and
`conversation.current_session`. They respectively describe compact profile,
labels, stored official decision, current recovery, recovery trend, movement,
strength, minimized recent activity, slow fitness, active structure, upcoming
GarminCoach sessions, and transient current conversation.

Acceptance requires deterministic bounded tenant-local reads, no writes, no
cross-tenant context, no private calendar, no external request during building,
and no authority change when aggregates change while the official decision is
held fixed.

Unsupported capability always suppresses its current value and observation time,
even when a stored freshness state is fresh or stale. Recovery Time uses its
persisted Connect/account source capability when available; an indistinguishable
source remains unknown. Strength context retains the shared candidate total so
its bounded wrapper reports exact omissions, while privacy trimming adds to
that count. Shared movement validation treats zero as observed and never
imputes missing days.

The slow summary includes Fitness Age, target Fitness Age, running/cycling VO2,
and neutral current-device Training Status (never legacy VO2). Recovery trends
are limited to sleep duration/score, overnight HRV, resting HR, stress, Body
Battery high, and typed sleep-timing variability. Both weekly and Ask Coach
use `metrics.training_aggregates` for identity, duration, domain, set, rounding,
and stable strength ordering. Activity, program, and planned-session lists are
bounded wrappers with exact omission counts. The effective ceiling is the lower
of configured maximum and 16,000; compact sorted-key serialization trims oldest
activity, farthest planned, next exercises, extra sessions, recovery items,
slow previous values, then strength highlights.
