# GarminCoach Metric and Sync Policy

**Status:** Approved policy; Phase 2 sync foundation complete
**Approved:** 2026-07-27  
**Scope:** Product behavior and synchronization target. Runtime code may still differ until later phases are implemented.

## 1. Core rule

GarminCoach keeps three concerns separate:

1. **Daily decision authority** — may change today's selected workout.
2. **Warnings and explanations** — provide useful context without changing it.
3. **Progress and health views** — dashboard and weekly-summary information.

Fetching a metric does not automatically give it authority over a workout.

The app must not create a custom readiness score or let an LLM interpret raw metrics into a decision.

## 2. When a recovery decision exists

A daily recovery decision is evaluated only when the athlete has selected or scheduled a specific normal workout in the website.

Without a selected workout:

- the app may display recovery and health information;
- it does not choose the next program workout;
- it does not create a Workout / Active Recovery / Rest decision.

Selection is strict: a candidate is a `PlannedSession` for the requested local
date that is not completed, cancelled, Rest, or a recovery replacement. One
candidate may be resolved implicitly; multiple candidates require an explicit
planned-session ID and no candidate produces `NO_SELECTED_WORKOUT`. Automatic
morning priority refresh runs only for exactly one candidate. No-candidate and
multiple-candidate mornings settle as informational messages and do not wait
for a program or stage scheduling.

Workout selection uses fresh local data. It does not call Garmin automatically. If required data is stale or missing, Telegram may offer an explicit recovery refresh.

## 3. Outcomes and confirmation

### Planned workout

Keep the selected workout unchanged.

### Active Recovery

A fixed Garmin walking workout:

- name: `Active Recovery — 30 Minute Walk`;
- walking for 30 minutes;
- duration only;
- no HR, pace, distance, calorie, interval, or progression target.

The app must not promise that it restores performance or prevents injury.

### Rest

No structured workout that day.

Telegram is the only action-confirmation surface. No response leaves the selected workout unchanged. Active Recovery or Rest keeps the original program session pending and does not advance the program cursor.

## 4. V1 decision authority

Program recovery rules are evaluated before biometrics. A required program rest day remains Rest.

### Garmin Training Readiness

Fresh Garmin Training Readiness is the only biometric with direct V1 authority.

Missing sleep does not block a valid fresh same-day Training Readiness score.
Support state, fetch freshness, and decision eligibility remain separate:
unsupported means no usable device/account value, unknown means unverified,
and supported pending/missing/stale/error/invalid values are reported exactly.
None permits a fallback score or biometric recommendation. Recovery evaluation
and dashboard display use stored local facts only; they make zero Garmin or
private-calendar calls and create no recovery mutation in this phase.

| Garmin score | Category | GarminCoach action |
| ---: | --- | --- |
| 95–100 | Prime | Planned workout |
| 75–94 | High | Planned workout |
| 50–74 | Moderate | Planned workout |
| 25–49 | Low | Planned workout with warning |
| 1–24 | Poor | Recommend Rest; athlete may override |

Garmin Training Readiness already considers sleep score, Recovery Time, HRV Status, acute load, sleep history, and stress history. GarminCoach must not rescore those components when a fresh score exists.

### Devices without Training Readiness

V1 has no evidence-supported fallback formula that automatically selects Active Recovery or Rest.

Sleep duration, HRV Status, Recovery Time, resting HR, stress, and Body Battery may produce clear warnings or context, but the official outcome remains the selected workout unless a future versioned evidence rule is separately approved.

When warnings exist, Telegram may let the athlete choose the original workout, the fixed walk, or Rest. That is an athlete choice, not a hidden biometric recommendation.

### Missing data

Capability, fetch state, and analytical eligibility are separate:

- capability: `supported`, `unsupported`, `unknown`;
- fetch state: `fresh`, `expected_pending`, `missing`, `stale`, `error`;
- eligibility: `eligible`, `insufficient_history`, `invalid`, `not_applicable`.

Missing never means unsupported. Stale or insufficient observations have no decision authority. No missing value is imputed.

## 5. Metric map

### Recovery and health

| Metric | Keep? | Product use | Decision authority | Target history |
| --- | --- | --- | --- | ---: |
| Training Readiness | Yes, capability-aware | Current recovery decision and UI | Direct when fresh | Current day; accumulate changes locally |
| Sleep duration | Yes | Warning, sleep debt, trends | No direct V1 authority | 28 nights |
| Sleep Score | Yes | Nightly quality context | None | 28 nights |
| Sleep start/end | Yes | Sleep consistency | None | 28 nights |
| Sleep stages | Yes when free in sleep payload | Detail view only | None | 28 nights or summary-only |
| HRV overnight value | Yes | Personal recovery trend | None | 28 days |
| HRV seven-day average/status/baseline | Yes | Garmin-defined trend and warnings | None in V1 | 28 days plus Garmin baseline |
| Resting HR | Yes | Personal recovery/health trend | None | 28 days |
| Recovery Time | Yes | Time until Garmin considers another hard workout optimal | Warning only | Current; accumulate changes locally |
| Body Battery current/high/low/charged/drained | Yes | Current energy and trend | None | 28 days |
| Stress average | Yes | Lifestyle/recovery trend | None | 28 days |
| Steps | Yes | Daily movement and weekly trend | None | 28 days |
| Moderate/vigorous intensity minutes | Yes | Weekly activity volume | None | 28 days |

### Training and progress

| Metric | Keep? | Product use | Target history |
| --- | --- | --- | ---: |
| Activity summaries, duration, distance, HR | Yes | Workout history and analysis | 90 days |
| Strength sets, reps, weights, exercise identity | Yes | Volume, e1RM, progression | Latest 20 strength activities or 90 days |
| RPE and Feel when Garmin recorded them | Yes | Workout-quality context | Latest 20 activities |
| Workout provenance | Yes | Program completion reconciliation | 90 days |
| Numeric Training Effect | Yes when available in an already-needed activity detail | Explain stimulus | Accumulate forward |
| HR-zone time | Conditional | Activity detail and load calculation | New relevant activities; no forced mass backfill |
| Training load, acute/chronic load, ACWR | Yes, locally derived | Descriptive trend only | Derived from activity history |
| Program completion and unmatched activity | Yes, locally derived | Adherence and auditability | Active program plus 90 days |
| VO2 max | Yes | Cardio-fitness progress | Current; accumulate changes locally |
| Fitness Age | Yes | Understandable long-term fitness progress | Current weekly; accumulate changes locally |
| Training Status | Conditional keep | Overall training direction when account/device supports it | Latest weekly/after meaningful activity; accumulate changes locally |
| Weight and body-fat percentage | Conditional keep | Body-composition progress when account has useful data | Up to 180 days via range-efficient fetch |

ACWR remains descriptive and must not be used for injury-risk claims or Telegram decisions.

### Implemented intensity-minute detail

The existing per-day `get_stats` request is the sole source. The exact pinned
`DailyStats` fields are `moderateIntensityMinutes` and
`vigorousIntensityMinutes`; no extra endpoint is requested. GarminCoach stores
them separately from activity-level intensity minutes. Stage 1, Stage 2, and
incremental daily-health windows therefore receive them as part of their
existing daily-summary work. Morning-priority sync and dashboard reads do not
add Garmin requests. The local 7-day and 28-day totals report raw moderate and
vigorous minutes separately plus valid-day coverage; there is no weighted total
and no missing-day imputation.

### Body-composition gate

Weight/body-fat remains conditional and is not implemented yet. It is
account/scale scoped rather than watch-model scoped: a watch registry cannot
establish whether an account has a scale measurement. Although the pinned
public wrapper supports `get_body_composition(startdate, enddate=None)`, its
response contract is untyped and lacks a reliable GarminCoach fixture. See
[`BODY_COMPOSITION_CONTRACT.md`](BODY_COMPOSITION_CONTRACT.md) for the exact
missing evidence. Once verified, it must use bounded range reads (latest for
Stage 1, at most 180 days for Stage 2), durable cursors, bounded unknown-account
probing, and no morning-priority or UI-time request.

## 6. Metrics not planned for V1

Do not add separate calls or product surfaces for:

- Health Snapshot;
- raw intraday HRV, stress, or Body Battery samples after summaries are derived;
- Pulse Ox and respiration trends;
- hydration or calories consumed;
- floors, badges, and challenges;
- GPS routes, per-second heart rate, or full sensor streams;
- detailed laps/splits unless a later product use is approved;
- historical Training Readiness backfill.

Fields returned free in an already-required payload may be stored defensively, but they do not justify a new call or UI feature.

## 7. Sync plan

### Initial Stage 1 — usable bootstrap

Fetch in this order:

1. authentication and device capability;
2. today's sleep and supported current recovery facts;
3. seven recent wellness days;
4. 30 days of activity summaries;
5. latest ten strength activities with sets;
6. latest body composition when available;
7. current VO2 max and Fitness Age;
8. current Training Status only when supported.

The dashboard and workout flow become usable after Stage 1.

### Initial Stage 2 — bounded background completion

- wellness metrics: 28 days;
- activities: 90 days;
- strength detail: latest 20 strength activities or 90 days;
- body composition: up to 180 days when range-efficient;
- no historical scans for Training Readiness, Training Status, Fitness Age, or VO2 max.

Newest and most product-useful gaps are filled first.

### Morning recovery refresh

Only when a selected workout or current recovery view needs it:

1. device upload/capability;
2. current sleep;
3. current Training Readiness when supported, or one due bounded probe when unknown;
4. current HRV Status and Recovery Time when available.

Do not fetch activities, steps, stress, Body Battery history, body composition, Fitness Age, VO2 max, or Training Status in this path.

### Normal incremental sync

- use a lightweight preflight;
- prefer one daily-summary payload for resting HR, stress, steps, intensity minutes, and Body Battery summaries when those fields are present;
- settle today plus a small recent overlap;
- fetch only new/recent activity summaries;
- enrich only new or incomplete activities;
- fetch strength sets only for strength activities;
- fetch HR zones only when relevant and not already complete;
- recompute all derived metrics locally.

### Capability registry and unknown probes

Device capability is resolved by a versioned, offline official-source registry.
Known unsupported optional endpoints are skipped. Unknown does not mean an
unconditional daily request: it may be probed only on Stage 1, scheduled
low-priority sync, or explicit full sync, and no more often than the configured
seven-day probe interval (priority recovery sync may probe only Training
Readiness when due). Empty, authentication, rate-limit, and ordinary-error
responses retain unknown. Recovery Time on the watch and Recovery Time expected
through Garmin Connect are distinct capabilities.

### Weekly low-priority sync

Fetch current:

- Fitness Age;
- VO2 max;
- Training Status when supported;
- body composition when available;
- bounded remaining gap chunks.

Store only changed slow-moving values to build local history.

### Manual sync

`Sync now` means current recovery refresh when needed, otherwise normal incremental sync. It must not restart a complete history backfill.

## 8. Rate-limit and reliability rules

Garmin consumer endpoints are unofficial and have no dependable published quota.

- Use one per-account request guard.
- Persist per-resource cursors and completion states.
- Stop low-priority work on the first 429 and persist cooldown.
- Let the library retry transient network/5xx failures, but never repeatedly retry 429.
- No ordinary dashboard or activity-page load may call Garmin.
- Local recomputation makes zero Garmin calls.

## 9. Garmin package target

Phase 2 tested and pinned `garminconnect[typed]==0.3.7` with Python 3.12.

Useful capabilities include typed responses for core read endpoints, stronger authentication/token refresh, transient-error retries, strength-workout helpers, an exercise catalog, workout updates, and explicit device push support.

The upgrade must be a compatibility task first because 0.3 uses a new token format, requires Python 3.12, and exposes response shapes that differ from current GarminCoach assumptions.

## 10. Remaining Phase 1 questions

No product decision remains open.

Later implementation must still verify factual account/API details with sanitized fixtures:

- exact Training Readiness response shapes;
- whether daily stats contain every intended combined summary field;
- whether the account has useful weight/body-fat data;
- Training Status capability and response behavior.

## 11. Verified Phase 3B recovery storage

GarminCoach reads HRV Status from the existing daily HRV payload. Its weekly
average and explicit status are Garmin source facts. `hrv_7d_coverage_days` is
different: it is a GarminCoach-local count of valid stored overnight HRV values
in the inclusive seven-day window, never Garmin eligibility or sample coverage.

Recovery Time is read only from the selected current Training Readiness
snapshot, in minutes; there is no dedicated Recovery Time request. When Garmin
reports `REACHED_ZERO`, GarminCoach stores the source minutes when supplied and
uses zero as the effective remaining time. Device and Connect Recovery Time are
separate capabilities: a vívoactive 5 can show it on-watch without exposing a
Connect value to GarminCoach.

HRV Status, Recovery Time, and Body Battery (including charged and drained
summary values) are informational in V1 and have no direct workout authority.

These checks may change adapters or request strategy, but not the approved product authority above.

## References

- [Garmin Training Readiness categories and inputs](https://www8.garmin.com/manuals/webhelp/GUID-A315BE5B-E191-4238-9712-D9C368997ADB/EN-GB/GUID-C21BE0C8-A08E-4DA1-B6C6-2E0E2DDDB372.html)
- [Garmin HRV Status](https://www8.garmin.com/manuals/webhelp/GUID-D3C2D1F9-D2C0-404D-9372-7B2D57459BF8/EN-US/GUID-9282196F-D969-404D-B678-F48A13D8D0CB.html)
- [vívoactive 5 Recovery Time](https://www8.garmin.com/manuals/webhelp/GUID-5D183A14-BB43-4A9B-B441-5F824214CE40/EN-US/GUID-DAC27D10-886A-4EA8-8339-674479E9574A.html)
- [Garmin Fitness Age](https://support.garmin.com/en-US/?faq=CM1YJmMrrNAbEpM9PapJ07)
- [Garmin Unified Training Status](https://support.garmin.com/en-US/?faq=EjPECQK58qA0xzJ5X74vm7)
- [Garmin Training Status definition](https://www8.garmin.com/manuals/webhelp/GUID-A315BE5B-E191-4238-9712-D9C368997ADB/EN-US/GUID-44C7BB4B-EFF7-4A42-AC03-8A6AABB94807.html)
- [python-garminconnect releases](https://github.com/cyberjunky/python-garminconnect/releases)
