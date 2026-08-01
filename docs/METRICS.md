# GarminCoach — Metrics Reference

> **Approved product scope (2026-07-27):** [`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md)
> defines which metrics are fetched, their product use, history, sync cadence,
> and decision authority. This document remains the source of truth for formulas
> and implemented metric semantics.

## Authority summary

- No custom composite readiness score is allowed.
- Fresh Garmin Training Readiness is the only biometric with direct V1 workout authority.
- Prime/High/Moderate keep the selected workout; Low keeps it with a warning; Poor recommends Rest with an override.
- On devices without Training Readiness, sleep, HRV Status, Recovery Time, resting HR, stress, and Body Battery are warnings or informational facts only in V1.
- Fitness Age remains a UI metric.
- Training Status remains a capability-aware UI/weekly-summary metric.
- ACWR remains descriptive UI data only and must not drive coaching or injury-risk claims.
- The product is not a medical device and does not diagnose recovery, illness, or injury.
- The implemented 28-day recovery/health trend semantics are specified in
  [`RECOVERY_HEALTH_TRENDS_CONTRACT.md`](RECOVERY_HEALTH_TRENDS_CONTRACT.md).
  They compare recent 7-day and preceding-21-day medians, never impute missing
  days, use product display thresholds only, and are informational. Daily
  intensity minutes retain their separate 7-day/28-day sum semantics.
- Slow Fitness Age, target Fitness Age, VO₂ max, and Training Status history is
  governed by [`SLOW_METRIC_HISTORY_CONTRACT.md`](SLOW_METRIC_HISTORY_CONTRACT.md).
  It accumulates only forward from current/weekly/activity observations: account
  scope for Fitness Age values, explicit running/cycling activity scope for VO₂,
  and current-device scope for Garmin source-text Training Status. Legacy VO₂ is
  permanently activity-unverified. These facts have no decision authority.

---

## 1. Per-activity training load (TRIMP)

**Where:** `metrics/engine.py` — `compute_training_load`, `banister_trimp`,
`edwards_trimp`, `estimate_hr_max`.

No method applies means `None`; the engine must not invent load.

### Banister TRIMP — validated structure

```text
HRR   = (HRavg − HRrest) / (HRmax − HRrest), clamped to [0,1]
TRIMP = duration_min × HRR × A × e^(B × HRR)
```

- male constants: `A=0.64`, `B=1.92`;
- female constants: `A=0.86`, `B=1.67`;
- estimated HRmax fallback: `208 − 0.7 × age`;
- same-day resting HR is required.

### Edwards summated HR-zone TRIMP — validated structure, approximate application

```text
TRIMP = Σ(minutes_in_zone_i × weight_i), weights = [1,2,3,4,5]
```

Garmin zones may not equal the original percentage-of-HRmax zones, so the
application is approximate.

### Scale consistency

Banister and Edwards values are not interchangeable. One method is pinned for
an entire recomputation series. An activity missing the pinned method's inputs
returns `None` instead of switching scales.

**References:** Banister (1991); Morton, Fitz-Clarke & Banister (1990); Edwards
(1993); Tanaka, Monahan & Seals (2001); Akubat & Abt (2011).

---

## 2. ACWR — descriptive only

```text
λacute   = 2/(7+1)  = 0.25
λchronic = 2/(28+1) ≈ 0.069
EWMA     = load_today × λ + prior_EWMA × (1−λ)
ACWR     = acute_EWMA / chronic_EWMA
```

Current labels are heuristic:

| ACWR | UI label |
| --- | --- |
| <0.8 | underload |
| 0.8–1.3 | balanced |
| 1.3–1.5 | elevated |
| >1.5 | spike |

These labels are not validated individual injury thresholds. ACWR must not
appear in Telegram decisions, warnings, or injury-prevention advice.

**References:** Williams et al. (2016); Gabbett (2016); Hulin et al. (2016);
Esmaeili et al. (2018); Impellizzeri et al. (2020).

---

## 3. Garmin Training Readiness authority

| Score | Category | Product consequence |
| ---: | --- | --- |
| 95–100 | Prime | Keep selected workout |
| 75–94 | High | Keep selected workout |
| 50–74 | Moderate | Keep selected workout |
| 25–49 | Low | Keep selected workout; warning |
| 1–24 | Poor | Recommend Rest; explicit override |

Program-required rest takes precedence. The score never changes exercises,
sets, reps, or weights.

A usable score must be fresh, supported, for the current decision date, and
selected from the latest valid same-day Garmin snapshot. Missing data is not a
substitute score and does not imply support or non-support.

## Selected-workout recovery evaluation

A recovery decision exists only for exactly one local `PlannedSession` on the
local decision date: it must be neither completed nor cancelled and must be a
normal workout (not Rest or an optional/recovery replacement). An explicit
planned-session ID is accepted; when none is supplied, zero candidates produces
`NO_SELECTED_WORKOUT` and multiple candidates produces
`WORKOUT_SELECTION_REQUIRED`, including the candidate IDs and names. GarminCoach
never chooses a program-cursor session as a recovery target.

Fresh supported Garmin Training Readiness is the only biometric authority that
can recommend Rest. Informational metrics never automatically choose a walk or
Rest; when Training Readiness has no authority, any Telegram walk/Rest choice
is explicitly the athlete's selection.

Program-required rest precedes biometrics and leaves the selected session and
cursor unchanged. Fresh same-day supported Garmin Training Readiness is the
only biometric authority: Prime/High/Moderate keep, Low keeps with a warning,
and Poor recommends Rest while the original remains pending. Missing sleep does
not invalidate a valid Training Readiness score. Unsupported, unknown,
pending, missing, stale, error, and invalid readiness states have no substitute
score or outcome authority. Sleep, Sleep Score, HRV, Recovery Time, resting HR,
stress, and other valid fresh facts are informational only.

Evaluation and dashboard presentation read stored local data only and make zero
Garmin or private-calendar calls. This phase stages no recovery interaction and
does not schedule, cancel, unschedule, replace, complete, or modify a workout.

For unsupported devices, individual observations remain visible but have no
prescriptive workout authority in V1.

The durable selected-workout observation contract uses the freshness signal IDs:
`sleep` is a numeric duration in hours rounded to one decimal; `sleep_score` is
a numeric Garmin score; `hrv_status` is Garmin's status text; and
`recovery_time` is an integer number of minutes. Telegram presents factual
sleep (including stored timing when available), Sleep Score, HRV Status, and
human-readable Recovery Time as context. It does not show exact overnight HRV,
resting-HR, or stress values proactively. The dashboard may show those stored
fresh facts. None of these informational facts substitutes for Training
Readiness or changes the decision.

When fresh supported Training Readiness grants a selected-workout outcome,
Telegram shows its typed score and Garmin category separately. Unsupported,
unknown, pending, missing, stale, error, and invalid values are never shown as
an authoritative score.

---

## 4. Retired custom readiness

`DailyMetrics.readiness` remains only for schema compatibility. Runtime
recomputation must store `NULL`, and no UI, snapshot, notification, or coach
path may consume it.

---

## 5. Sleep debt — validated minimum, heuristic window

```text
sleep_debt = min(Σ max(0, target − valid_sleep_hours_i), cap)
target = personal goal when configured, otherwise 7.0 h
window = 7 valid nights
cap = 14 h
```

- Missing nights are excluded, not treated as zero sleep.
- No valid data produces unknown, not zero debt.
- The seven-night window and cap are product heuristics.

**References:** AASM/SRS adult sleep-duration consensus; Van Dongen et al.
(2003).

---

## 6. VO2 max category

Use the Cooper Institute age/sex normative table when age and sex are available.
Without them, show the raw Garmin value without fabricating a category.

Fitness Age is a separate Garmin-provided UI metric and is not derived by
GarminCoach from this table.

---

## 7. Strength metrics

### Volume load

```text
volume = Σ(reps_i × weight_i)
```

### Epley estimated 1RM

```text
e1RM = weight × (1 + reps/30), for reps > 1
e1RM = weight, for reps <= 1
```

- Only use sets with `reps <= 12`.
- Skip bodyweight/unknown-weight sets.
- Session e1RM is the maximum valid e1RM for the exercise.
- Progress compares against the best value in the previous five valid sessions;
  five sessions is a product heuristic.

**References:** Epley (1985); Schoenfeld et al. (2021); Wood et al. (2002).

---

## 8. Informational Garmin metrics

The following are retained for dashboard and weekly-summary use but have no V1
daily workout authority:

- sleep duration, score, timing, and stages;
- HRV overnight value, seven-day average, status, and baseline;
- resting HR, Recovery Time, Body Battery, stress, steps, and intensity minutes;
- Training Effect and HR zones;
- VO2 max, Fitness Age, conditional Training Status, weight, and body fat.

Their detailed request strategy and history are defined in
[`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md).

HRV Status is Garmin's explicit daily-payload status, not a GarminCoach
comparison of a nightly value with a baseline. The Garmin seven-day average is
stored as source data. The displayed `N/7 nights` value is only local stored
overnight-HRV completeness. Recovery Time is stored in minutes from the
selected current Training Readiness snapshot; `REACHED_ZERO` means effective
zero while retaining source minutes. No separate Recovery Time endpoint exists.

### Daily intensity minutes

Daily intensity minutes are read only from the existing `get_stats` daily
summary: `moderateIntensityMinutes` and `vigorousIntensityMinutes` in
`garminconnect==0.3.7`'s typed `DailyStats`. They are stored separately as
`DailyHealth.daily_moderate_intensity_minutes` and
`DailyHealth.daily_vigorous_intensity_minutes`; these are not the similarly
named per-activity fields.

Values must be finite, numeric, and non-negative. Zero is an observed value;
missing or malformed values remain `NULL`. Local 7-day and 28-day views sum
only stored values and disclose the number of days with either observed value,
so missing days are never treated as zero. They are informational only: they
do not enter readiness, workout, Telegram, injury-risk, or Ask Coach logic.

## Weekly summary

Phase 4G implements the deterministic local weekly report documented in
[`WEEKLY_SUMMARY_CONTRACT.md`](WEEKLY_SUMMARY_CONTRACT.md). It contains bounded
training/adherence, activity, movement, descriptive strength, approved Phase
4E recovery-trend, scoped Phase 4F slow-history, and next-session sections.
It reuses the existing Phase 4E formulas and Phase 4F history exactly; missing
days are never converted to zero. It has no custom score, workout authority,
ACWR, or Training Readiness content.

## Body composition gate

Body composition remains intentionally gated. See
[`BODY_COMPOSITION_CONTRACT.md`](BODY_COMPOSITION_CONTRACT.md): the pinned
client confirms a range-capable public method but not the response shape,
date/timestamp fields, read units, or empty-account behavior required for safe
storage and sync policy.
