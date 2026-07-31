# Slow Metric History Contract

Phase 4F stores forward-accumulating, tenant-local observations for Garmin-provided Fitness Age, achievable Fitness Age, VO2 max, and Training Status. It is informational only and has no recovery, workout, progression, scheduling, notification, Telegram, weekly-summary, or Ask Coach authority.

## Scope and source

- `fitness_age` and `target_fitness_age` are numeric account facts at `account/account`.
- `vo2max` is numeric activity evidence at exactly `activity/running`, `activity/cycling`, or the migration-only `activity/legacy_unverified`.
- `training_status` is preserved Garmin source text at the current `device/<normalized model>` scope. It is never interpreted.

An observation has a deterministic ID over canonical fields and a unique source identity `(metric, scope, source_kind, source_key)`. Exactly one finite numeric value or non-empty, control-character-free status text is stored. Fitness Age values are `(0, 120]`, VO2 values are `(0, 100]`, and status text is at most 64 characters.

## Forward-only behavior

Existing numeric-series heads use the same canonical activity ordering as batch
ingestion: local date, local time, numeric activity ID, then source key. The
report applies its 60-point cap only after this canonical ordering. Training
Status uses observed time for chronology, never its status hash; execution
context is not source identity. Repeated identical status text is coalesced to
its first immutable observation for that device/day, while a later changed text
is a distinct transition. Fitness Age normalizes timestamp-shaped `lastUpdated`
values to their source-local ISO date before storage.

The writer orders a series by source-local date, source time when known, and stable source key. A retry of the same source fact is idempotent; a changed fact under the same source identity conflicts and fails closed. A value equal to the current head is not inserted. An older source never replaces the head. Values are immutable and are never compacted or deleted with an activity, device, or profile cache row. Same-day activity VO2 uses the activity start time and ID as deterministic source identity/order.

No Garmin history scan, new endpoint, dashboard-time Garmin call, or raw-payload storage is permitted. Fitness Age uses the existing current/weekly fetch and supplied `lastUpdated` date (otherwise the supplied local sync date); VO2 uses existing incremental activity summaries; Training Status uses the existing capability-aware current/weekly path.

Verified activity VO2 is validated as a batch before mutation, partitioned by
scope, and processed oldest-first by local datetime and numeric activity ID.
Garmin response order therefore cannot affect the durable series or generic
compatibility tile. That tile is the latest accepted verified running/cycling
activity by time, numeric ID, then domain; it is not the largest numeric value.
Stage 1 retains only this bounded canonical source representation for resume.

## Legacy migration

Only local data is seeded. Valid MetricSnapshot current and previous Fitness Age facts become account observations. Valid MetricSnapshot VO2 facts become `legacy_unverified`, never running or cycling. Historic DailyHealth Training Status facts become `legacy_unverified_device` and are never presented as a current device status. Invalid dates or values are skipped. The migration marker is written only after table/index validation and initialization is idempotent.

## Dashboard and capability behavior

Both Training Status fetch paths use one persistence helper: it requires a
persisted device identity and valid source text before writing history, the
daily compatibility field, or capability evidence. A status fingerprint allows
same-day changes while retaining retry idempotency. Target Fitness Age shares
the Fitness Age account capability and has no separate capability key. Public
dates are plain `date` values; source/creation datetimes are naive local values
and future dates are rejected when the caller supplies an as-of day. Migration
validation checks table shape, index ordering, constraints, no foreign keys,
and rolled-back representative probes.

The current `MetricSnapshot` remains the compatibility/current-value cache. The dashboard separately shows local Fitness Age/target and running/cycling VO2 observations; legacy VO2 is explicitly labeled as activity type unverified. It shows current-device Training Status only, with `SUPPORTED_WITH_DATA`, `SUPPORTED_NO_DATA`, `UNSUPPORTED`, `UNKNOWN`, or `NO_DEVICE_IDENTITY`. Source wording remains neutral: “Garmin Training Status: …”. No status receives a score, trend, recommendation, or positive/negative interpretation.
