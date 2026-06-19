"""Deterministic metrics engine (Phase 2).

Pure-math functions over SQLite data — no LLM, instant, same inputs → same
outputs. Called after every sync via ``recompute_all()``.

Every formula here is grounded in published sports-science literature; see
``docs/METRICS.md`` for the full derivation, constants, and citations. Where no
single validated formula exists (the composite readiness score, the sleep-debt
window) the choice is flagged as a documented heuristic both here and in that
doc.

Key design choices:
  - Every scoring function is pure (takes primitives, returns primitives) so it
    can be unit-tested without a DB or network.
  - Missing inputs → graceful degradation (renormalize, skip), never crash.
  - Only the orchestrator (``recompute_daily_metrics``) touches the ORM.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Optional

from db import Activity, DailyHealth, DailyMetrics, Sleep, SyncState, get_session

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Edwards (1993) summated-HR-zone TRIMP multipliers for zones 1–5
# (50–60 / 60–70 / 70–80 / 80–90 / 90–100 % HRmax). Edwards S, *The Heart Rate
# Monitor Book*, 1993.  NOTE: Garmin's native zones are threshold-based, not
# fixed %HRmax, so applying these to Garmin zone times is approximate.
ZONE_WEIGHTS: list[float] = [1.0, 2.0, 3.0, 4.0, 5.0]

# Banister TRIMP gender constants (Banister 1991; Morton et al. 1990;
# confirmed Akubat & Abt 2011). Female constants yield the higher (more
# conservative) load, so they are the safe default when gender is unknown.
TRIMP_A_MALE, TRIMP_B_MALE = 0.64, 1.92
TRIMP_A_FEMALE, TRIMP_B_FEMALE = 0.86, 1.67

# Readiness component weights. HEURISTIC (expert-informed, not RCT-validated):
# weight concentrated on HRV, the metric with the strongest evidence base
# (Buchheit 2014). Body Battery is intentionally excluded from the weighted
# score — it is a proprietary Garmin composite that already embeds HRV, so
# including it double-counts. Weights sum to 1.0 across the three components.
W_HRV = 0.50
W_RHR = 0.25
W_SLEEP = 0.25

# Sleep target for debt calculation: AASM/SRS adult minimum (Watson et al. 2015).
SLEEP_TARGET_HOURS = 7.0

# Trailing windows.
ACUTE_DAYS = 7
CHRONIC_DAYS = 28
READINESS_BASELINE_DAYS = 7   # acute HRV/RHR baseline (Plews et al. 2012)
SLEEP_DEBT_WINDOW = 7         # HEURISTIC: one week (no authoritative window)
SLEEP_DEBT_CAP = 14.0         # = 7 nights × 2 h/night max plausible shortfall

# How many days to recompute on each sync (older data is stable).
RECOMPUTE_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# 1. Training load
# ---------------------------------------------------------------------------

def estimate_hr_max(age: float | None) -> float | None:
    """Age-predicted maximal HR, Tanaka et al. 2001: HRmax = 208 − 0.7·age.
    (More accurate than the older Fox 220−age.)  None if age unknown."""
    if age is None or age <= 0:
        return None
    return 208.0 - 0.7 * age


def banister_trimp(
    avg_hr: float,
    duration_s: float,
    hr_rest: float,
    hr_max: float,
    is_male: bool | None = None,
) -> float | None:
    """Banister TRIMP — the preferred per-activity load when HRrest/HRmax known.

        HRR   = (HRavg − HRrest) / (HRmax − HRrest)
        TRIMP = minutes × HRR × A × e^(B·HRR)

    Constants A,B are gender-specific (Banister 1991; Morton et al. 1990).
    Female constants give the higher (conservative) estimate, so they are the
    default when gender is unknown.  Output is in arbitrary units (AU).
    """
    if not duration_s or hr_max is None or hr_rest is None or hr_max <= hr_rest:
        return None
    hrr = (avg_hr - hr_rest) / (hr_max - hr_rest)
    hrr = max(0.0, min(1.0, hrr))  # clamp to physiological [0,1]
    a, b = (TRIMP_A_MALE, TRIMP_B_MALE) if is_male else (TRIMP_A_FEMALE, TRIMP_B_FEMALE)
    minutes = duration_s / 60.0
    trimp = minutes * hrr * a * math.exp(b * hrr)
    return round(trimp, 1) if trimp > 0 else None


def edwards_trimp(hr_zone_seconds: list[float]) -> float | None:
    """Edwards (1993) summated-HR-zone TRIMP: Σ(minutes_in_zone_i × weight_i),
    weights 1–5 for zones 1–5.  Approximate on Garmin's threshold-based zones."""
    if not hr_zone_seconds or len(hr_zone_seconds) < len(ZONE_WEIGHTS):
        return None
    total = sum(
        (secs / 60.0) * w for secs, w in zip(hr_zone_seconds, ZONE_WEIGHTS)
    )
    return round(total, 1) if total > 0 else None


def compute_training_load(
    avg_hr: float | None,
    duration_s: float | None,
    hr_zone_seconds: list[float] | None = None,
    hr_rest: float | None = None,
    hr_max: float | None = None,
    is_male: bool | None = None,
    method: str | None = None,
) -> float | None:
    """Per-activity training load (TRIMP, arbitrary units).

    Banister and Edwards TRIMP differ ~1.5–2.2× in magnitude, so they must NOT
    be mixed within a single ACWR series — switching formulas mid-window makes
    the acute/chronic ratio spike or drop purely from the scale change, not from
    any real change in load. Callers therefore pick ONE *method* for the whole
    activity set (see ``choose_load_method``) and pass it here; this function
    never silently falls back across scales.

      - ``method="banister"``: HR-reserve TRIMP; requires avg_hr + HRrest + HRmax.
      - ``method="edwards"``:  summated zone TRIMP; requires seconds-in-zone.
      - ``method=None``:       legacy auto (Banister if possible, else Edwards).
                               Kept for callers scoring a single isolated
                               activity where scale-consistency is irrelevant.

    Returns None when the chosen method's inputs are missing — we do NOT invent
    a load, and (when method is pinned) we do NOT cross to the other scale.
    """
    can_banister = (
        avg_hr is not None
        and duration_s
        and hr_rest is not None
        and hr_max is not None
    )

    if method == "banister":
        return banister_trimp(avg_hr, duration_s, hr_rest, hr_max, is_male) if can_banister else None
    if method == "edwards":
        return edwards_trimp(hr_zone_seconds) if hr_zone_seconds else None

    # method is None: legacy most-accurate-first auto-selection.
    if can_banister:
        banister = banister_trimp(avg_hr, duration_s, hr_rest, hr_max, is_male)
        if banister is not None:
            return banister
    return edwards_trimp(hr_zone_seconds) if hr_zone_seconds else None


def choose_load_method(
    activities: list,
    rhr_by_day: dict[date, float],
    hr_max: float | None,
) -> str:
    """Pick ONE TRIMP method for the whole activity set so the ACWR series stays
    on a single scale (per the reviewer's "enforce one method consistently"
    note). Prefer Banister — the more accurate HR-reserve model — when the
    majority of activities can be scored with it; otherwise fall back to Edwards
    zone TRIMP for the entire set.

    Choosing once here, rather than per-activity, is what prevents the artificial
    ACWR spikes/drops from formula-switching between inconsistently-instrumented
    activities.
    """
    if hr_max is None:
        return "edwards"

    scorable = [a for a in activities if a.start_time and a.duration_s]
    if not scorable:
        return "banister"  # nothing to score; harmless default

    banister_ready = sum(
        1
        for a in scorable
        if a.avg_hr is not None and rhr_by_day.get(a.start_time.date()) is not None
    )
    # Use Banister when it covers most activities; else keep the whole set on
    # Edwards rather than mixing scales.
    return "banister" if banister_ready >= len(scorable) / 2 else "edwards"


# ---------------------------------------------------------------------------
# 2. Acute / chronic load + ACWR
# ---------------------------------------------------------------------------

def compute_daily_loads(
    daily_load_map: dict[date, float],
    target_day: date,
) -> tuple[float | None, float | None, float | None]:
    """Compute (acute_load, chronic_load, acwr) for *target_day* using EWMA.

    EWMA ACWR per Williams et al. 2016: each day's EWMA is
    ``Load_today × λ + (1 − λ) × EWMA_yesterday`` with ``λ = 2/(N+1)``
    (λ_acute = 0.25 for N=7, λ_chronic ≈ 0.069 for N=28). Implemented as the
    equivalent weighted sum starting at i=0 so that *today's* load is included
    — the previous version started at i=1, leaving every ratio a day stale.

    The exponential weighting also prevents the artificial drop a rolling
    window shows when a big session ages out.
    """
    def _ewma(days: int) -> float:
        alpha = 2.0 / (days + 1)
        total = 0.0
        weight_sum = 0.0
        # Look back 3x the window to capture ~95% of the exponential curve.
        lookback = days * 3

        # i=0 is today (weight 1.0), i=1 yesterday, …
        for i in range(0, lookback + 1):
            d = target_day - timedelta(days=i)
            load = daily_load_map.get(d, 0.0)
            w = (1.0 - alpha) ** i
            total += load * w
            weight_sum += w

        return round(total / weight_sum, 1) if weight_sum > 0 else 0.0

    acute = _ewma(ACUTE_DAYS)
    chronic = _ewma(CHRONIC_DAYS)

    if chronic == 0.0:
        return acute, chronic, None

    acwr = round(acute / chronic, 2)
    return acute, chronic, acwr


# ---------------------------------------------------------------------------
# 3. Readiness score (0–100)
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _z_to_score(z: float, *, invert: bool = False) -> float:
    """Map a z-score (deviation from personal baseline, in SDs) to 0–100 via a
    smooth bounded transform: ``50 + 50·tanh(z/2)`` (z=+2 → ≈88, z=−2 → ≈12,
    z=0 → 50). ``invert=True`` flips it (used for RHR, where higher = worse)."""
    if invert:
        z = -z
    return _clamp(50.0 + 50.0 * math.tanh(z / 2.0))


def _score_hrv(hrv: float, baseline_mean: float, baseline_sd: float) -> float:
    """HRV sub-score from today's value vs a personal rolling baseline.

    Compares to the 7-day rolling mean using a z-score (Plews et al. 2012/2013;
    z-score normalization per Dial et al. 2025). Garmin's overnight HRV is an
    RMSSD-derived ms value; we treat the supplied series consistently (linear),
    which is adequate over a short 7-day window where lnRMSSD ≈ linear.
    """
    if baseline_sd <= 0:
        # No within-person variance yet — fall back to a neutral-ish ratio.
        return _clamp(50.0 + (hrv / baseline_mean - 1.0) * 100.0)
    z = (hrv - baseline_mean) / baseline_sd
    return _z_to_score(z)


def _score_rhr(rhr: float, baseline_mean: float, baseline_sd: float) -> float:
    """RHR sub-score (inverted: elevated RHR vs baseline lowers readiness)."""
    if baseline_sd <= 0:
        return _clamp(50.0 - (rhr / baseline_mean - 1.0) * 200.0)
    z = (rhr - baseline_mean) / baseline_sd
    return _z_to_score(z, invert=True)


def _score_sleep(actual_hours: float, target_hours: float,
                 efficiency_pct: float | None = None) -> float:
    """Sleep sub-score from duration (and efficiency when available).

    dur_score anchors 8 h → 100; if sleep efficiency is known, blend
    0.6·dur + 0.4·eff (the 0.6/0.4 split is a documented HEURISTIC — no source
    fixes it). Thresholds align with Watson et al. 2015 / Costa et al. 2021.
    """
    dur_score = _clamp(actual_hours / 8.0 * 100.0)
    if efficiency_pct is None:
        return dur_score
    eff_score = _clamp((efficiency_pct - 50.0) / 40.0 * 100.0)
    return _clamp(0.6 * dur_score + 0.4 * eff_score)


def compute_readiness(
    hrv: float | None, hrv_mean: float | None, hrv_sd: float | None,
    rhr: float | None, rhr_mean: float | None, rhr_sd: float | None,
    sleep_hours: float | None, sleep_target: float,
    sleep_efficiency_pct: float | None = None,
) -> float | None:
    """Composite readiness (0–100) — HEURISTIC composite of science-based parts.

    Weighted blend of HRV (0.50), RHR (0.25) and sleep (0.25) sub-scores. Each
    sub-score is normalized against the user's own recent baseline. No
    peer-reviewed study validates a specific weight vector for a consumer-
    wearable composite, so the weights are expert-informed (HRV dominant per
    Buchheit 2014). Body Battery is deliberately NOT a component (it already
    embeds HRV → double-counting); show it separately instead.

    Missing components are skipped and remaining weights renormalized.
    """
    components: list[tuple[float, float]] = []  # (score, weight)

    if hrv is not None and hrv_mean is not None and hrv_mean > 0:
        components.append((_score_hrv(hrv, hrv_mean, hrv_sd or 0.0), W_HRV))

    if rhr is not None and rhr_mean is not None and rhr_mean > 0:
        components.append((_score_rhr(rhr, rhr_mean, rhr_sd or 0.0), W_RHR))

    if sleep_hours is not None:
        components.append(
            (_score_sleep(sleep_hours, sleep_target, sleep_efficiency_pct), W_SLEEP)
        )

    if not components:
        return None

    total_weight = sum(w for _, w in components)
    score = sum(s * w for s, w in components) / total_weight
    return round(_clamp(score), 0)


# ---------------------------------------------------------------------------
# 4. Sleep debt
# ---------------------------------------------------------------------------

def compute_sleep_debt(
    sleep_hours_history: list[float | None],
    target_hours: float = SLEEP_TARGET_HOURS,
) -> float:
    """Accumulated sleep deficit over the trailing window (linear, no decay).

    debt = min( Σ max(0, target − hours_i) over the last N nights, CAP ).

    Each night's shortfall counts equally — Van Dongen et al. 2003 found
    near-linear accumulation of deficit, with no published inter-day decay.
    Nights with no data (``None``) are excluded, not imputed as zero sleep
    (which would add a spurious full-target deficit). N=7 and the 14 h cap are
    documented heuristics. *sleep_hours_history* is newest-first.
    """
    debt = 0.0
    for hours in sleep_hours_history[:SLEEP_DEBT_WINDOW]:
        if hours is not None:
            debt += max(0.0, target_hours - hours)
    return round(min(debt, SLEEP_DEBT_CAP), 1)


# ---------------------------------------------------------------------------
# 5. ACWR word label
# ---------------------------------------------------------------------------

def acwr_label(acwr: float | None) -> str:
    """Human-readable ACWR interpretation. Thresholds are HEURISTIC: the
    0.8/1.3/1.5 cut-points come from rolling-average team-sport studies
    (Gabbett 2016) and are not independently validated for EWMA ACWR on an
    individual; treat as a guide, not a diagnosis."""
    if acwr is None:
        return ""
    if acwr < 0.8:
        return "underload"
    if acwr <= 1.3:
        return "balanced"
    if acwr <= 1.5:
        return "elevated"
    return "spike ⚠"


# ---------------------------------------------------------------------------
# 6. Orchestrator (the only part that touches the DB)
# ---------------------------------------------------------------------------

def _mean_sd(vals: list[float]) -> tuple[float | None, float | None]:
    """Sample mean and (population) standard deviation; (None, None) if empty.
    SD is None when <2 points (no within-person variance to normalize against)."""
    if not vals:
        return None, None
    mean = sum(vals) / len(vals)
    if len(vals) < 2:
        return round(mean, 1), None
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return round(mean, 1), round(var ** 0.5, 2)


def _baselines(
    health_rows: list[DailyHealth],
    target_day: date,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Personal HRV/RHR baseline over the READINESS_BASELINE_DAYS window before
    *target_day* (Plews et al. 2012: a 7-day rolling window tracks acute
    readiness, not long-term fitness drift). Returns
    (hrv_mean, hrv_sd, rhr_mean, rhr_sd)."""
    cutoff = target_day - timedelta(days=READINESS_BASELINE_DAYS)
    hrv_vals: list[float] = []
    rhr_vals: list[float] = []
    for h in health_rows:
        if cutoff <= h.day < target_day:
            if h.hrv_overnight is not None:
                hrv_vals.append(h.hrv_overnight)
            if h.resting_hr is not None:
                rhr_vals.append(h.resting_hr)
    hrv_mean, hrv_sd = _mean_sd(hrv_vals)
    rhr_mean, rhr_sd = _mean_sd(rhr_vals)
    return hrv_mean, hrv_sd, rhr_mean, rhr_sd


def recompute_daily_metrics(session) -> None:
    """Recompute DailyMetrics for the recent window.

    Reads Activity, DailyHealth, and Sleep tables; writes to DailyMetrics.
    Called from ``recompute_all()``.
    """
    today = date.today()
    window_start = today - timedelta(days=RECOMPUTE_WINDOW_DAYS)
    # We need data going back further for baselines and chronic load.
    data_start = today - timedelta(days=RECOMPUTE_WINDOW_DAYS + CHRONIC_DAYS * 3)

    # --- Load raw data in bulk (one query each) ---------------------------
    activities = (
        session.query(Activity)
        .filter(Activity.start_time.isnot(None))
        .filter(Activity.start_time >= data_start)
        .all()
    )
    health_rows = (
        session.query(DailyHealth)
        .filter(DailyHealth.day >= data_start)
        .order_by(DailyHealth.day.asc())
        .all()
    )
    sleep_rows = (
        session.query(Sleep)
        .filter(Sleep.day >= data_start)
        .order_by(Sleep.day.asc())
        .all()
    )

    # --- Pre-compute lookups -----------------------------------------------
    # Daily load map: sum of training_load for all activities on each day.
    daily_load: dict[date, float] = {}
    for act in activities:
        if act.start_time and act.training_load:
            d = act.start_time.date()
            daily_load[d] = daily_load.get(d, 0.0) + act.training_load

    # Health / sleep by day.
    health_by_day: dict[date, DailyHealth] = {h.day: h for h in health_rows}
    sleep_by_day: dict[date, Sleep] = {s.day: s for s in sleep_rows}

    # --- Compute each day in the recompute window --------------------------
    day = window_start
    while day <= today:
        # ACWR
        acute, chronic, acwr = compute_daily_loads(daily_load, day)

        # Personal HRV/RHR baselines for readiness (7-day rolling mean + SD).
        hrv_mean, hrv_sd, rhr_mean, rhr_sd = _baselines(health_rows, day)

        # Today's health + sleep values.
        h = health_by_day.get(day)
        s = sleep_by_day.get(day)
        hrv = h.hrv_overnight if h else None
        rhr = h.resting_hr if h else None
        sleep_hours = (s.total_s / 3600.0) if (s and s.total_s) else None
        # Sleep efficiency = time asleep / time in bed (asleep + awake).
        sleep_eff = None
        if s and s.total_s and s.awake_s is not None:
            in_bed = s.total_s + s.awake_s
            sleep_eff = (s.total_s / in_bed * 100.0) if in_bed > 0 else None

        readiness = compute_readiness(
            hrv, hrv_mean, hrv_sd,
            rhr, rhr_mean, rhr_sd,
            sleep_hours, SLEEP_TARGET_HOURS, sleep_eff,
        )

        # Sleep debt: trailing 14 days, newest first.
        sleep_hist: list[float | None] = []
        for i in range(SLEEP_DEBT_WINDOW):
            sd = sleep_by_day.get(day - timedelta(days=i))
            sleep_hist.append((sd.total_s / 3600.0) if (sd and sd.total_s) else None)
        sleep_debt = compute_sleep_debt(sleep_hist)

        # Upsert DailyMetrics row.
        row = session.get(DailyMetrics, day) or DailyMetrics(day=day)
        row.readiness = readiness
        row.acute_load = acute
        row.chronic_load = chronic
        row.acwr = acwr
        row.sleep_debt_h = sleep_debt
        session.add(row)

        day += timedelta(days=1)


def _user_age(session) -> float | None:
    """Age in years from the synced profile birth date (SyncState), or None."""
    bd = session.get(SyncState, "user_birth_date")
    if not (bd and bd.value):
        return None
    try:
        b = date.fromisoformat(bd.value[:10])
        today = date.today()
        return today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    except Exception:
        return None


def _user_is_male(session) -> bool | None:
    """True/False from the synced profile gender, or None when unknown
    (compute_training_load then uses the conservative female constants)."""
    g = session.get(SyncState, "user_gender")
    if not (g and g.value):
        return None
    return g.value.strip().upper() == "MALE"


def recompute_all() -> None:
    """Recompute derived metrics. Called after every sync."""
    with get_session() as session:
        age = _user_age(session)
        hr_max = estimate_hr_max(age)
        is_male = _user_is_male(session)

        # Resting HR by day, to feed Banister TRIMP (HR reserve).
        rhr_by_day: dict[date, float] = {
            h.day: h.resting_hr
            for h in session.query(DailyHealth).all()
            if h.resting_hr is not None
        }

        # Pick ONE TRIMP method for the whole set so the ACWR series stays on a
        # single scale (Banister and Edwards differ ~1.5–2.2×; mixing them makes
        # ACWR spike/drop from the formula switch, not from real load changes).
        activities = session.query(Activity).all()
        method = choose_load_method(activities, rhr_by_day, hr_max)
        log.info("training-load method for this recompute: %s", method)

        # Per-activity training load using the pinned method (None when that
        # method's inputs are missing — never invented, never cross-scale).
        for act in activities:
            hr_rest = rhr_by_day.get(act.start_time.date()) if act.start_time else None
            act.training_load = compute_training_load(
                act.avg_hr,
                act.duration_s,
                hr_rest=hr_rest,
                hr_max=hr_max,
                is_male=is_male,
                method=method,
            )

        # Daily aggregate metrics.
        recompute_daily_metrics(session)
