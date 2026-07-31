# Slow Metric History Contract

Phase 4F stores forward-accumulating, tenant-local observations for Garmin-provided Fitness Age, achievable Fitness Age, VO2 max, and Training Status. It is informational only and has no recovery, workout, progression, scheduling, notification, Telegram, weekly-summary, or Ask Coach authority.

## Scope and source

- `fitness_age` and `target_fitness_age` are numeric account facts at `account/account`.
- `vo2max` is numeric activity evidence at exactly `activity/running`, `activity/cycling`, or the migration-only `activity/legacy_unverified`.
- `training_status` is preserved Garmin source text at the current `device/<normalized model>` scope. It is never interpreted.

An observation has a deterministic ID over canonical fields and a unique source identity `(metric, scope, source_kind, source_key)`. Exactly one finite numeric value or non-empty, control-character-free status text is stored. Fitness Age values are `(0, 120]`, VO2 values are `(0, 100]`, and status text is at most 64 characters.

## Forward-only behavior

The writer orders a series by source-local date, source time when known, and stable source key. A retry of the same source fact is idempotent; a changed fact under the same source identity conflicts and fails closed. A value equal to the current head is not inserted. An older source never replaces the head. Values are immutable and are never compacted or deleted with an activity, device, or profile cache row. Same-day activity VO2 uses the activity start time and ID as deterministic source identity/order.

No Garmin history scan, new endpoint, dashboard-time Garmin call, or raw-payload storage is permitted. Fitness Age uses the existing current/weekly fetch and supplied `lastUpdated` date (otherwise the supplied local sync date); VO2 uses existing incremental activity summaries; Training Status uses the existing capability-aware current/weekly path.

## Legacy migration

Only local data is seeded. Valid MetricSnapshot current and previous Fitness Age facts become account observations. Valid MetricSnapshot VO2 facts become `legacy_unverified`, never running or cycling. Historic DailyHealth Training Status facts become `legacy_unverified_device` and are never presented as a current device status. Invalid dates or values are skipped. The migration marker is written only after table/index validation and initialization is idempotent.

## Dashboard and capability behavior

The current `MetricSnapshot` remains the compatibility/current-value cache. The dashboard separately shows local Fitness Age/target and running/cycling VO2 observations; legacy VO2 is explicitly labeled as activity type unverified. It shows current-device Training Status only, with `SUPPORTED_WITH_DATA`, `SUPPORTED_NO_DATA`, `UNSUPPORTED`, `UNKNOWN`, or `NO_DEVICE_IDENTITY`. Source wording remains neutral: “Garmin Training Status: …”. No status receives a score, trend, recommendation, or positive/negative interpretation.
