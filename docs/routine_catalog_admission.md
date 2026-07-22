# Routine catalog admission rules

Last reviewed: 2026-07-22

This document defines when a Muscle & Strength routine may be one of the 25
selectable default routines in GarminCoach. The source database is
[Muscle & Strength Workout Routines](https://www.muscleandstrength.com/workout-routines).

## Source facts are authoritative

The app copies the source page's Workout Summary fields. In particular,
`Training Level` is never inferred from exercise complexity, weekly frequency,
volume, or our opinion of the trainee. `Beginner`, `Intermediate`, and
`Advanced` mean exactly what the source page says. For example, Powerbuilding
PPL remains `Intermediate` because that is its published level.

The source's goal, workout type, days per week, time range, session sequence,
sets, repetitions, rest guidance, and progression rules are also audit facts.
An adaptation must be disclosed; it must not silently change the defining
method.

## Required admission checks

A routine is selectable only when every check below passes.

1. **Repeatable default:** it is a base training template that can continue
   through ordinary load/repetition progression and periodic reassessment. A
   published 8-, 10-, or 12-week duration is a review interval, not by itself a
   rejection, when the source does not require the trainee to stop or enter a
   different phase.
2. **Not a temporary intervention:** challenges, shock methods, deloads,
   peaking blocks, return-from-hiatus blocks, and other transitional programs
   are rejected.
3. **Not dependent on a temporary nutrition phase:** a routine built around a
   calorie-deficit cutting phase, contest preparation, or another temporary
   diet phase is not a general default.
4. **Fully representable:** the app can express the defining session order,
   frequency, exercises, sets, repetitions, rest schedule, and progression
   behavior. A mandatory phase change, rotating weekly prescription, deload,
   circuit, or cardio component cannot be omitted if that changes the program.
5. **Complete resistance routine:** all defining resistance sessions are
   retained. Optional accessories may be omitted only when the audit records
   the omission and the program still preserves its training structure.
6. **Balanced weekly coverage:** the executable catalog gate requires at least
   two exercise-backed lower-body, press, and pull exposures per cycle. Focus
   labels and arm isolation do not count as evidence.
7. **Garmin-compatible:** every retained exercise maps to a Garmin FIT strength
   exercise. The source-facing name remains visible when the closest supported
   Garmin enum is used.
8. **Source-backed operating rules:** session order and required recovery days
   come from the source. Weekday examples become equivalent rolling rest slots
   so a missed workout does not reset the sequence.
9. **Recommendation-safe:** every selectable routine may be ranked from the
   user's observed frequency, split, exercises, and inferred personal training
   history. No routine needs a hidden “specialty” warning to prevent it from
   becoming the default recommendation.

Any failed check excludes the routine from the selectable catalog. It may be
implemented later only after the missing behavior exists and a new source audit
passes.

## 2026-07-22 replacements

| Removed routine | Rejection reason | Replacement | Published level |
| --- | --- | --- | --- |
| 100-Rep Full Body Shocker | Temporary four-week high-fatigue challenge | [3 Day Full Body Dumbbell Workout](https://www.muscleandstrength.com/workouts/3-day-full-body-dumbbell-workout) | Beginner |
| Muscle Rebound | Return-from-hiatus transition block | [4 Day Dumbbell Upper/Lower](https://www.muscleandstrength.com/workouts/dumbbell-only-upper-lower-workout-routine) | Beginner |
| RP-21 | Defining mesocycle/deload behavior is not implemented | [4 Day Barbell Only (No Rack)](https://www.muscleandstrength.com/workouts/4-day-barbell-only-workout) | Intermediate |
| Advanced Upper/Lower Mass | Source explicitly says it is not for long-duration use | [Home or Gym Barbell Routine](https://www.muscleandstrength.com/workouts/home-gym-barbell-workout-routine) | Beginner |
| Body Fat Demolition | Temporary calorie-deficit fat-loss phase | [5 Day Dumbbell Workout Split](https://www.muscleandstrength.com/workouts/5-day-dumbbell-only-workout-split) | Intermediate |

The replacements use standard progressive overload or ordinary rep/load
progression, do not require an unimplemented phase engine, and preserve the
catalog's 25-routine total.

## Enforcement

`coach/program_policy.py` records the source-published training level for every
program, rejects the known temporary/phase-specific keys, and requires a
one-to-one program policy. `coach/programs.py` enforces exercise-backed weekly
coverage. Tests verify the 25-routine count, exact published labels, rejection
set, Garmin mappings, and recommendation scores.

Powerbuilding PPL is intentionally still `Intermediate`. Its source-level fact
is independent from its scheduling rule: the catalog uses the source's
intermediate three-days-on/one-day-off cadence as rolling rest slots while
retaining the published six-days-per-week routine.
