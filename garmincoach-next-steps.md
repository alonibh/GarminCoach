# GarminCoach — Current State and Next Steps

Updated 2026-07-18.

This file previously described the unimplemented Phase 2-4 plan for an
LLM-driven composite-readiness coach. That design is obsolete. The metrics,
program, decision, interaction, and notification phases are implemented under a
different authority model: deterministic rules decide consequential actions;
an optional LLM can answer informational free text only.

Use these documents as the current sources of truth:

- [`README.md`](README.md) for product capabilities and setup.
- [`docs/coach_product_architecture.md`](docs/coach_product_architecture.md) for
  goals, authority boundaries, timing, missing-data handling, and acceptance
  gates.
- [`docs/routine_source_audit.md`](docs/routine_source_audit.md) for the ten
  selected programs, rolling rest rules, recovery guidance, and rest timers.
- [`docs/METRICS.md`](docs/METRICS.md) for formulas and limits on metric use.
- [`ROADMAP.md`](ROADMAP.md) for active priorities and deferred scope.

## Shipped state

1. Garmin data is cached locally and synchronized through full, incremental,
   and briefing-priority paths.
2. Freshness and device capability are persisted per signal. Missing data is
   not silently converted into unsupported capability or a favorable result.
3. Ten source-reviewed strength routines compile into structured Garmin
   workouts with warm-ups, sets, reps/duration, weights when known, and audited
   rest timers.
4. A rolling program cursor reconciles completed Garmin activities without
   resetting at week boundaries or confusing other programs with the active
   routine.
5. Morning decisions are deterministic, persisted, concise, and revalidated
   before any Telegram button applies a mutation.
6. Garmin Training Readiness is used only on supported devices. The custom
   readiness composite is retired; unsupported devices do not receive a
   fabricated replacement score.
7. Workouts are never automatically modified from biometric data. Low data
   adds a warning; Poor Garmin readiness can advise skipping while leaving the
   original session available by explicit confirmation.
8. Notifications use a durable outbox with quiet hours, one-hour reminders,
   calendar-conflict handling, Saturday summaries, and narrowly defined late
   material updates.

## Immediate next validation cycle

The next work is operational verification, not another design phase:

1. Re-authenticate Garmin and complete a real Vivoactive 5 morning sync.
2. Confirm sleep, HRV, resting heart rate, and other available observations
   receive correct freshness states while Training Readiness remains explicitly
   unsupported.
3. Activate one of the ten routines, schedule its next eligible session by
   Telegram confirmation, and verify the Garmin workout arrives on the watch.
4. Complete the workout on the watch, sync it, and confirm exactly one program
   cursor advancement with provenance retained.
5. Restart the app with queued notifications and verify no duplicate briefing,
   reminder, or weekly summary is delivered.

## Next implementation work

After the operational cycle passes, the highest-value engineering gaps are:

- separate between-set and between-exercise transition timers;
- operator-visible sync/outbox health;
- source-specific duration/deload review prompts; and
- independently evidence-reviewed rules for individual observations on devices
  without Garmin Training Readiness.

Do not reintroduce the retired composite score, LLM-generated workout actions,
ACWR coaching authority, proactive post-workout questions, or automatic metric-
driven changes to exercises, sets, repetitions, or weights.
