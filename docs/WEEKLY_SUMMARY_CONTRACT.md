# Weekly summary contract

Phase 4G provides one informational, plain-text weekly summary. It is a local read model and has no coaching, workout, progression, calendar, Garmin, or LLM authority.

## Window and sources

The immutable payload `week_end` is the athlete-local scheduled Saturday. The report covers exactly `week_end - 6 days` through `week_end`, inclusive: activity and planned-session timestamps are compared to local `00:00:00` through `23:59:59.999999`. `week_end` is an exact Saturday `date`; generation receives a naive athlete-local datetime and rejects future or stale weeks. Retries keep the same payload and may reread newly synced local facts only inside that window. Same-Saturday delivery uses existing overnight freshness; a later local delivery treats Saturday overnight facts as historical and includes any stored valid observations without requiring current-day freshness.

The bounded tenant-local builder reads `Activity`, `ActivityProgramMatch`, `ExerciseSet`, `DailyHealth`, active program/session/cursor facts, and the approved Phase 4E and 4F report builders. It writes and commits nothing. Missing observations remain missing; no source is fetched or inferred.

## Aggregate semantics

Program completion counts distinct matched activity IDs by activity local start time, only for the current active program. The target is positive `days_per_week`, otherwise non-addon `coach_strength` sessions, otherwise unknown. Normal planned sessions in the window whose status is not terminal, activity type is not Rest, intensity is not recovery, and linked session role is not `optional_recovery` are described as incomplete. Unmatched strength/weight activities have no `ActivityProgramMatch` at all; an activity matched to an inactive program is not labelled unmatched. All activity counts include matched and unmatched rows; valid non-negative duration is summed only when observed. Domains are bounded to four and stable-sorted by count then label.

Steps and daily moderate/vigorous minutes come only from `DailyHealth` for the seven dates. Finite non-negative integer-like values, including zero, count as observed. Steps are summed without imputation; intensity fields are summed separately and a day is covered when either is observed.

Strength highlights compare the week to the preceding seven days using active, valid stored `ExerciseSet` rows joined to stored activities. They compare maximum weight for the same canonical exercise and exact reps, show only an increase, and retain at most two stable-sorted highlights. Display weights are independently rounded half-up to the 250-gram grid before comparing/displaying current, prior, and delta values. Rest, warmup, cooldown, recovery, malformed, and nonpositive sets are excluded. They are descriptive evidence, not progression actions or e1RM estimates.

Recovery highlights reuse `build_recovery_health_trend_report` unchanged with `as_of_day=week_end`; Saturday overnight data is included only when same-day freshness says it is ready. Only sufficiently covered approved sleep duration, Sleep Score, overnight HRV, resting HR, stress, and Body Battery high trends can appear. At most three lines use sleep-first priority and neutral higher/lower/similar language.

Fitness highlights reuse `build_slow_metric_history_report` unchanged. Only a weekly change in running or cycling VO₂ max, Fitness Age, or target Fitness Age is shown; legacy VO₂ is never shown. Current-device Training Status appears only as `SUPPORTED_WITH_DATA`. Unsupported, unknown, previous-device, and no-data states are omitted. At most three lines are stable-prioritized.

## Render and delivery

The renderer produces plain text only: a short date-range header, Training, optional Movement, Strength, Recovery trends, Fitness, Next, and the fixed informational footer. Empty sections are omitted. Dynamic names have controls removed, whitespace collapsed, and a 48-character display bound. The result has no links, HTML, Markdown, buttons, raw IDs, JSON, source keys, statuses, or errors; it is limited to 18 nonempty lines and 3,500 Unicode characters. Optional lines are removed deterministically before the header, required training line, or footer.

`send_weekly_summary()` only enqueues the established `weekly:<date>` durable identity and no-ops when invoked on a non-Saturday. The existing minute outbox poller is the sole delivery path and materializes the report at delivery as `(text, None, None)`, requiring plain Telegram transport with no reply markup. The row is cancelled for a malformed payload shape, noncanonical/future/non-Saturday date, or delivery-date-stale payload (`local date > week_end + 6 days`); retries within that boundary retain the original week. Existing leases, quiet hours, attempt limits, and idempotency remain unchanged.

## Acceptance boundaries

All calculations are tenant-session-local, bounded, deterministic, and read-only. Next-session display reads only an already-existing cursor and its referenced active-program session; missing or stale cursor state is omitted and never repaired. The summary never changes a selected workout, cursor, planned session, progression proposal, compiled/scheduled Garmin workout, notification batch, or recovery interaction. It does not touch Ask Coach snapshots/prompts, CoachMessage data, morning briefings, databases outside the supplied tenant, or a private calendar. No new endpoint, fetch, sync worker, table, migration, dashboard, or UI request is part of this contract.
