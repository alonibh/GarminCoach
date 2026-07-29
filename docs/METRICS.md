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

For unsupported devices, individual observations remain visible but have no
prescriptive workout authority in V1.

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

## Body composition gate

Body composition remains intentionally gated. See
[`BODY_COMPOSITION_CONTRACT.md`](BODY_COMPOSITION_CONTRACT.md): the pinned
client confirms a range-capable public method but not the response shape,
date/timestamp fields, read units, or empty-account behavior required for safe
storage and sync policy.
