"""Deterministic metrics engine (Phase 2).

Pure-math functions over SQLite data — no LLM, instant, same inputs → same
outputs. Called after every sync via ``recompute_all()``.

Key design choices:
  - Every scoring function is pure (takes primitives, returns primitives) so it
    can be unit-tested without a DB or network.
  - Missing inputs → graceful degradation (renormalize, skip), never crash.
  - Only the orchestrator (``recompute_daily_metrics``) touches the ORM.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from db import Activity, DailyHealth, DailyMetrics, Sleep, get_session

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Zone-weighted TRIMP multipliers (zones 1–5).
ZONE_WEIGHTS: list[float] = [1.0, 1.5, 2.0, 3.5, 5.0]

# Readiness component weights (must sum to 1.0).
W_HRV = 0.40
W_SLEEP = 0.25
W_RHR = 0.20
W_BB = 0.15

# Sleep target for debt calculation.
SLEEP_TARGET_HOURS = 8.0

# Trailing windows.
ACUTE_DAYS = 7
CHRONIC_DAYS = 28
BASELINE_DAYS = 60
SLEEP_DEBT_WINDOW = 14
SLEEP_DEBT_CAP = 30.0

# How many days to recompute on each sync (older data is stable).
RECOMPUTE_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# 1. Training load
# ---------------------------------------------------------------------------

def compute_training_load(
    avg_hr: float | None,
    duration_s: float | None,
    hr_zone_seconds: list[float] | None = None,
) -> float | None:
    """Per-activity training load.

    When *hr_zone_seconds* (seconds spent in zones 1–5) is available, uses
    zone-weighted TRIMP for better accuracy with stop-start sports.  Falls
    back to simple ``avg_hr × minutes / 100`` when zone data is absent.
    """
    if hr_zone_seconds and len(hr_zone_seconds) >= len(ZONE_WEIGHTS):
        total = sum(
            secs * weight
            for secs, weight in zip(hr_zone_seconds, ZONE_WEIGHTS)
        )
        load = total / 60.0 / 100.0
        return round(load, 1) if load > 0 else None

    # Fallback: simple TRIMP.
    if avg_hr is None or not duration_s:
        return None
    return round(avg_hr * (duration_s / 60.0) / 100.0, 1)


# ---------------------------------------------------------------------------
# 2. Acute / chronic load + ACWR
# ---------------------------------------------------------------------------

def compute_daily_loads(
    daily_load_map: dict[date, float],
    target_day: date,
) -> tuple[float | None, float | None, float | None]:
    """Compute (acute_load, chronic_load, acwr) for *target_day*.

    Rest days within each window count as 0 load (not skipped).
    """
    def _window_mean(days: int) -> float | None:
        total = 0.0
        for i in range(1, days + 1):
            d = target_day - timedelta(days=i)
            total += daily_load_map.get(d, 0.0)
        return round(total / days, 1)

    acute = _window_mean(ACUTE_DAYS)
    chronic = _window_mean(CHRONIC_DAYS)

    if acute is None or chronic is None:
        return acute, chronic, None
    acwr = round(acute / chronic, 2) if chronic > 0 else None
    return acute, chronic, acwr


# ---------------------------------------------------------------------------
# 3. Readiness score (0–100)
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _score_hrv(hrv: float, baseline: float) -> float:
    """Above baseline → high score, below → low."""
    return _clamp(50.0 + 50.0 * (hrv - baseline) / baseline)


def _score_rhr(rhr: float, baseline: float) -> float:
    """Below baseline → high score (lower resting HR = better recovery)."""
    return _clamp(50.0 + 50.0 * (baseline - rhr) / baseline)


def _score_sleep(actual_hours: float, target_hours: float) -> float:
    """Fraction of target, mapped to 0–100."""
    return _clamp(100.0 * actual_hours / target_hours)


def _score_bb(bb_low: float) -> float:
    """Body Battery low is already 0–100 from Garmin."""
    return _clamp(bb_low)


def compute_readiness(
    hrv: float | None, hrv_baseline: float | None,
    rhr: float | None, rhr_baseline: float | None,
    sleep_hours: float | None, sleep_target: float,
    bb_low: float | None,
) -> float | None:
    """Weighted readiness score (0–100).

    Missing components are skipped and remaining weights renormalized, so
    the score degrades gracefully when some data is absent.
    """
    components: list[tuple[float, float]] = []  # (score, weight)

    if hrv is not None and hrv_baseline is not None and hrv_baseline > 0:
        components.append((_score_hrv(hrv, hrv_baseline), W_HRV))

    if rhr is not None and rhr_baseline is not None and rhr_baseline > 0:
        components.append((_score_rhr(rhr, rhr_baseline), W_RHR))

    if sleep_hours is not None:
        components.append((_score_sleep(sleep_hours, sleep_target), W_SLEEP))

    if bb_low is not None:
        components.append((_score_bb(bb_low), W_BB))

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
    """Accumulated sleep deficit over trailing days.

    *sleep_hours_history* is newest-first.  None entries (missing days) are
    skipped.  Result is capped at SLEEP_DEBT_CAP to avoid runaway values
    from long data gaps.
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
    """Human-readable ACWR interpretation."""
    if acwr is None:
        return ""
    if acwr < 0.8:
        return "detraining"
    if acwr <= 1.3:
        return "balanced"
    if acwr <= 1.5:
        return "ramping"
    return "spike ⚠"


# ---------------------------------------------------------------------------
# 6. Orchestrator (the only part that touches the DB)
# ---------------------------------------------------------------------------

def _baselines(
    health_rows: list[DailyHealth],
    target_day: date,
) -> tuple[float | None, float | None]:
    """Rolling mean HRV and RHR over the BASELINE_DAYS window before
    *target_day*.  Returns (hrv_baseline, rhr_baseline)."""
    cutoff = target_day - timedelta(days=BASELINE_DAYS)
    hrv_vals: list[float] = []
    rhr_vals: list[float] = []
    for h in health_rows:
        if cutoff <= h.day < target_day:
            if h.hrv_overnight is not None:
                hrv_vals.append(h.hrv_overnight)
            if h.resting_hr is not None:
                rhr_vals.append(h.resting_hr)
    hrv_base = round(sum(hrv_vals) / len(hrv_vals), 1) if hrv_vals else None
    rhr_base = round(sum(rhr_vals) / len(rhr_vals), 1) if rhr_vals else None
    return hrv_base, rhr_base


def recompute_daily_metrics(session) -> None:
    """Recompute DailyMetrics for the recent window.

    Reads Activity, DailyHealth, and Sleep tables; writes to DailyMetrics.
    Called from ``recompute_all()``.
    """
    today = date.today()
    window_start = today - timedelta(days=RECOMPUTE_WINDOW_DAYS)
    # We need data going back further for baselines and chronic load.
    data_start = today - timedelta(days=RECOMPUTE_WINDOW_DAYS + BASELINE_DAYS)

    # --- Load raw data in bulk (one query each) ---------------------------
    activities = (
        session.query(Activity)
        .filter(Activity.start_time is not None)
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

        # Baselines for readiness (60-day trailing mean).
        hrv_base, rhr_base = _baselines(health_rows, day)

        # Today's health + sleep values.
        h = health_by_day.get(day)
        s = sleep_by_day.get(day)
        hrv = h.hrv_overnight if h else None
        rhr = h.resting_hr if h else None
        bb_low = h.body_battery_low if h else None
        sleep_hours = (s.total_s / 3600.0) if (s and s.total_s) else None

        readiness = compute_readiness(
            hrv, hrv_base,
            rhr, rhr_base,
            sleep_hours, SLEEP_TARGET_HOURS,
            bb_low,
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


def recompute_all() -> None:
    """Recompute derived metrics. Called after every sync."""
    with get_session() as session:
        # Per-activity training load.
        for act in session.query(Activity).all():
            act.training_load = compute_training_load(act.avg_hr, act.duration_s)

        # Daily aggregate metrics.
        recompute_daily_metrics(session)
