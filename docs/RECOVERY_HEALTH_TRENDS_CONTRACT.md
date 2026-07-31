# Recovery and health trends contract

This document is the source of truth for GarminCoach Phase 4E's local,
informational recovery and health trends. It creates no readiness, recovery, or
health score and has no authority over a workout.

## Windows and local dates

For every metric endpoint `E`, the recent window is `E-6` through `E` (seven
calendar days) and the baseline is `E-27` through `E-7` (the preceding 21
calendar days). This is not a selection of the last 28 observed rows. Missing
calendar dates remain missing and are never imputed as zero.

`as_of_day` is supplied by the athlete-local clock. Sleep duration, Sleep
Score, sleep timing, overnight HRV, resting HR, Garmin HRV source facts, and
Recovery Time end on local today only when the existing overnight freshness
contract says today's overnight observations are complete; otherwise they end
on local yesterday. Stress, Body Battery, steps, and daily intensity-minute
totals always end on local yesterday. This prevents a partial current day from
entering a full-day comparison. Timezone-naive stored sleep datetimes remain
application-local values; the trend code does not attach or convert a timezone.

## Inputs, validity, and statistics

The builder makes bounded local SQL reads of `Sleep` and `DailyHealth`; it does
not fetch, write, commit, sync, or call Garmin. It rejects booleans,
non-numerics, NaN, and infinities. It also requires: sleep duration `(0,24]`
hours; Sleep Score `[0,100]`; overnight HRV and resting HR `>0`; stress and
Body Battery high/low/current `[0,100]`; charged/drained and intensity minutes
finite `>=0`; and Recovery Time and steps integer-like `>=0`. Invalid, stale,
incomplete, and missing values are absent observations. Duplicate day inputs
fail closed for that metric rather than depending on source ordering.

Recent and baseline comparisons use the median, with optional min/max source
ranges. Median is robust to a one-day wearable outlier, deterministic,
understandable, and makes no distributional assumption. Existing 7-day and
28-day moderate/vigorous intensity summaries remain sums, with separate
coverage; they are not classified by this engine.

## Coverage and comparison

A direction is available only with at least 4 valid recent days of 7 and 10
valid baseline days of 21. Coverage for all 28 calendar days is:

- sufficient: at least 20 valid days and both comparison requirements;
- partial: 14--19 valid days, or 14+ without both comparison requirements;
- sparse: 1--13 valid days;
- none: zero valid days.

The UI always shows exact coverage (`N/28 valid days`). Coverage communicates
stored observations only, never feature capability or support.

`delta = recent median - baseline median`. A zero baseline has no percentage
change. Display rounding is deterministic and never exposes floating-point
artifacts. Product display thresholds (not medical or scientific diagnostic
thresholds) classify a delta smaller than the threshold as similar/stable:

| Metric | Threshold |
| --- | --- |
| Sleep duration | 0.25 hours |
| Sleep Score | 3 points |
| Overnight HRV | max(2 ms, 5% of positive baseline median) |
| Resting HR | 2 bpm |
| Stress | 3 points |
| Body Battery high, low, charged, drained | 5 points each |
| Recovery Time | 60 minutes |
| Steps | max(500 steps, 5% of positive baseline median) |

The approved language is factual: higher/lower than prior 21-day median,
similar to prior 21-day median, or not enough valid days to compare. It does
not say recovered, unrecovered, ready, fatigued, improved, worsened, ill,
dehydrated, overtrained, healthy, unhealthy, or injury risk.

## Sleep timing, Body Battery, and Garmin HRV facts

Sleep timing requires both stored endpoints and `end > start`. Its actual
midpoint is `start + (end-start)/2`, represented as minutes from midnight on
the stored `Sleep.day`; negative and greater-than-1440 offsets remain valid.
Each window reports median midpoint and median absolute deviation (MAD) from
that median. Displayed bedtime and wake-time medians are calculated from those
day-relative start/end offsets first, then formatted modulo 1440 as a local
clock time; raw `hour * 60 + minute` medians are not used. The card shows
recent median bedtime, wake time, timing
variability, valid-night coverage, and a higher/lower/similar comparison of
recent MAD to baseline MAD; 15 minutes is the stable threshold. It is not a
0--100 consistency score.

Body Battery remains four independent source trends: daily high, daily low,
charged, and drained. High and low are prominent, charged/drained secondary.
No high-minus-low, charged-minus-drained, or weighted composite is calculated.

The most recent non-empty Garmin `hrv_status` at or before the overnight
endpoint is displayed as title-cased source text with its source day; future
and incomplete-current-day source rows are excluded. Valid Garmin baseline
low/high are shown only as a finite positive ordered pair. GarminCoach never
derives, normalizes, or fabricates a status from local overnight HRV; that
separate local median comparison remains informational. Dashboard reference
lines are explicitly labeled Garmin baseline low/high or neutral sleep-hour
references; they are never GarminCoach “good range” judgments.

## Authority and acceptance rules

Every card carries: “Trends are informational and do not change your workout.”
Fresh same-day Garmin Training Readiness remains the only biometric with direct
V1 workout authority; program-required rest remains higher priority. These
trends never select, change, schedule, cancel, replace, complete, or modify a
workout or strength progression. They are not inputs to selected-workout
recovery evaluation, priority refresh, Telegram, notifications/outbox, Ask
Coach, morning briefing, or eligibility decisions.

Acceptance is testable: exact 7/21 calendar boundaries, validity rejection,
median and MAD math, duplicate-day handling, 4/7 and 10/21 gates, all
thresholds, coverage labels, partial-day endpoints, tenant-local bounded
queries, no writes/commits, no external calls, and unchanged decision outcomes
when historical trend data changes.
