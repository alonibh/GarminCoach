"""Unit tests for metrics.engine — pure functions, no DB or network.

These tests are the executable spec for the science-based formulas documented
in ``docs/METRICS.md``. Each asserted number is derived from the published
formula in that doc; if a formula changes, update both together.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

# Make the project root importable (tests/ is one level down).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metrics.engine import (
    SLEEP_TARGET_HOURS,
    SLEEP_DEBT_CAP,
    acwr_label,
    banister_trimp,
    choose_load_method,
    compute_daily_loads,
    compute_readiness,
    compute_sleep_debt,
    compute_training_load,
    edwards_trimp,
    estimate_hr_max,
)


# ---------------------------------------------------------------------------
# 1. Training load — Banister + Edwards TRIMP (A1)
# ---------------------------------------------------------------------------

class TestTrainingLoad:
    def test_estimate_hr_max_tanaka(self):
        # 208 - 0.7*30 = 187.0
        assert estimate_hr_max(30) == 187.0
        assert estimate_hr_max(None) is None
        assert estimate_hr_max(0) is None

    def test_banister_male_vs_female(self):
        # HRR = (150-50)/(190-50) = 0.714…; female constants give a higher load.
        male = banister_trimp(150, 3600, 50, 190, is_male=True)
        female = banister_trimp(150, 3600, 50, 190, is_male=False)
        assert male == 108.1
        assert female == 121.5
        assert female > male  # female constants are the conservative default

    def test_banister_needs_valid_hr_window(self):
        assert banister_trimp(150, 3600, 190, 190) is None  # max<=rest
        assert banister_trimp(150, 0, 50, 190) is None       # no duration

    def test_compute_load_prefers_banister(self):
        # When HRrest + HRmax are present, Banister is used.
        out = compute_training_load(
            150, 3600, hr_rest=50, hr_max=190, is_male=True
        )
        assert out == 108.1

    def test_compute_load_unknown_gender_uses_female(self):
        out = compute_training_load(150, 3600, hr_rest=50, hr_max=190)
        assert out == 121.5  # female (conservative) default

    def test_edwards_zone_fallback(self):
        # No HRrest/HRmax → falls back to Edwards zone TRIMP.
        # 600s in each of 5 zones: Σ (10 min × w) = 10*(1+2+3+4+5) = 150.
        zones = [600.0, 600.0, 600.0, 600.0, 600.0]
        assert compute_training_load(150, 3600, hr_zone_seconds=zones) == 150.0
        assert edwards_trimp(zones) == 150.0

    def test_no_invented_load(self):
        # Neither HR-reserve inputs nor zone data → None, never a guess.
        assert compute_training_load(150, 3600) is None
        assert compute_training_load(None, 3600) is None
        assert compute_training_load(150, None) is None

    def test_edwards_insufficient_zones(self):
        assert edwards_trimp([600.0, 600.0]) is None

    # --- Pinned-method behaviour (scale-consistency fix) -------------------

    def test_pinned_banister_does_not_cross_to_edwards(self):
        # method="banister" but no HRrest/HRmax → None, NOT an Edwards value,
        # so the ACWR series never silently switches scale.
        zones = [600.0, 600.0, 600.0, 600.0, 600.0]
        assert compute_training_load(
            150, 3600, hr_zone_seconds=zones, method="banister"
        ) is None

    def test_pinned_edwards_does_not_cross_to_banister(self):
        # method="edwards" with full HR-reserve inputs still uses Edwards only.
        assert compute_training_load(
            150, 3600, hr_rest=50, hr_max=190, method="edwards"
        ) is None  # no zone data → None, never a Banister value
        zones = [600.0, 600.0, 600.0, 600.0, 600.0]
        assert compute_training_load(
            150, 3600, hr_rest=50, hr_max=190, hr_zone_seconds=zones,
            method="edwards",
        ) == 150.0


class TestChooseLoadMethod:
    """choose_load_method pins one TRIMP scale for the whole activity set."""

    class _Act:
        def __init__(self, start_time, duration_s, avg_hr):
            self.start_time = start_time
            self.duration_s = duration_s
            self.avg_hr = avg_hr

    def _act(self, day, dur=3600, hr=150):
        return self._Act(datetime(day.year, day.month, day.day, 8), dur, hr)

    def test_edwards_when_hr_max_unknown(self):
        d = date(2026, 6, 1)
        acts = [self._act(d)]
        assert choose_load_method(acts, {d: 50.0}, hr_max=None) == "edwards"

    def test_banister_when_majority_scorable(self):
        d1, d2 = date(2026, 6, 1), date(2026, 6, 2)
        acts = [self._act(d1), self._act(d2)]
        rhr = {d1: 50.0, d2: 52.0}  # both have HRrest
        assert choose_load_method(acts, rhr, hr_max=190.0) == "banister"

    def test_edwards_when_most_activities_lack_hr_reserve(self):
        d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
        acts = [self._act(d1), self._act(d2), self._act(d3)]
        rhr = {d1: 50.0}  # only 1 of 3 scorable → fall back to Edwards
        assert choose_load_method(acts, rhr, hr_max=190.0) == "edwards"

    def test_no_scorable_activities_defaults_banister(self):
        assert choose_load_method([], {}, hr_max=190.0) == "banister"


# ---------------------------------------------------------------------------
# 2. EWMA ACWR (A2) — today's load is included (i=0)
# ---------------------------------------------------------------------------

class TestDailyLoads:
    def test_steady_load_acwr_near_one(self):
        today = date(2025, 6, 10)
        load_map = {today - timedelta(days=i): 10.0 for i in range(0, 40)}
        acute, chronic, acwr = compute_daily_loads(load_map, today)
        assert acute == 10.0
        # Chronic EWMA hasn't fully converged over 40 days; ~0.9–1.1 ratio.
        assert 0.9 <= acwr <= 1.1

    def test_today_is_counted(self):
        # A hard session TODAY must move acute load immediately (regression
        # test for the prior off-by-one that excluded i=0).
        today = date(2025, 6, 10)
        with_today = compute_daily_loads({today: 100.0}, today)[0]
        without = compute_daily_loads({today - timedelta(days=1): 100.0}, today)[0]
        assert with_today > without

    def test_all_rest_days(self):
        today = date(2025, 6, 10)
        acute, chronic, acwr = compute_daily_loads({}, today)
        assert acute == 0.0
        assert chronic == 0.0
        assert acwr is None  # division by zero → None


# ---------------------------------------------------------------------------
# 3. Readiness (A3) — z-score components, HRV-weighted composite
# ---------------------------------------------------------------------------

class TestReadiness:
    def test_all_at_baseline(self):
        # HRV & RHR exactly at their 7-day mean → z=0 → 50 each.
        # Sleep 8h → 100. Composite = 0.50*50 + 0.25*50 + 0.25*100 = 62.5 → 62.
        score = compute_readiness(
            hrv=50.0, hrv_mean=50.0, hrv_sd=5.0,
            rhr=60.0, rhr_mean=60.0, rhr_sd=3.0,
            sleep_hours=8.0, sleep_target=SLEEP_TARGET_HOURS,
        )
        assert score == 62.0

    def test_high_hrv_low_rhr_raises_score(self):
        score = compute_readiness(
            hrv=70.0, hrv_mean=50.0, hrv_sd=5.0,   # well above baseline
            rhr=52.0, rhr_mean=60.0, rhr_sd=3.0,   # well below baseline
            sleep_hours=9.0, sleep_target=SLEEP_TARGET_HOURS,
        )
        assert score >= 80

    def test_missing_hrv_renormalizes(self):
        score = compute_readiness(
            hrv=None, hrv_mean=None, hrv_sd=None,
            rhr=60.0, rhr_mean=60.0, rhr_sd=3.0,
            sleep_hours=8.0, sleep_target=SLEEP_TARGET_HOURS,
        )
        # Only RHR (50) and sleep (100) remain → (0.25*50+0.25*100)/0.5 = 75.
        assert score == 75.0

    def test_all_none(self):
        assert compute_readiness(
            None, None, None, None, None, None, None, SLEEP_TARGET_HOURS
        ) is None

    def test_clamped_range(self):
        hi = compute_readiness(
            hrv=200.0, hrv_mean=50.0, hrv_sd=5.0,
            rhr=30.0, rhr_mean=60.0, rhr_sd=3.0,
            sleep_hours=12.0, sleep_target=SLEEP_TARGET_HOURS,
        )
        lo = compute_readiness(
            hrv=10.0, hrv_mean=50.0, hrv_sd=5.0,
            rhr=120.0, rhr_mean=60.0, rhr_sd=3.0,
            sleep_hours=2.0, sleep_target=SLEEP_TARGET_HOURS,
        )
        assert 0 <= lo <= hi <= 100

    def test_sleep_efficiency_blend(self):
        # Duration alone (8h→100) vs blended with poor efficiency (60%).
        only_dur = compute_readiness(
            None, None, None, None, None, None,
            sleep_hours=8.0, sleep_target=SLEEP_TARGET_HOURS,
        )
        with_eff = compute_readiness(
            None, None, None, None, None, None,
            sleep_hours=8.0, sleep_target=SLEEP_TARGET_HOURS,
            sleep_efficiency_pct=60.0,
        )
        assert with_eff < only_dur


# ---------------------------------------------------------------------------
# 4. Sleep debt (A4) — linear cumulative deficit, no decay
# ---------------------------------------------------------------------------

class TestSleepDebt:
    def test_no_debt(self):
        assert compute_sleep_debt([8.0] * 7) == 0.0

    def test_linear_accumulation(self):
        # 6h/night for 7 nights, target 7.0 → 1h * 7 = 7.0 (no decay).
        assert compute_sleep_debt([6.0] * 7) == 7.0

    def test_none_excluded(self):
        # None nights are skipped, not counted as zero sleep.
        assert compute_sleep_debt([None, 8.0, None, 8.0]) == 0.0

    def test_capped(self):
        assert compute_sleep_debt([0.0] * 14) == SLEEP_DEBT_CAP

    def test_window_limited_to_7(self):
        # 20 entries of 5h (2h short each); only first 7 count → 14, capped 14.
        assert compute_sleep_debt([5.0] * 20) == 14.0

    def test_empty(self):
        assert compute_sleep_debt([]) == 0.0


# ---------------------------------------------------------------------------
# 5. ACWR label (A2) — relabeled, thresholds documented as heuristic
# ---------------------------------------------------------------------------

class TestAcwrLabel:
    def test_none(self):
        assert acwr_label(None) == ""

    def test_underload(self):
        assert acwr_label(0.5) == "underload"

    def test_balanced(self):
        assert acwr_label(1.0) == "balanced"

    def test_elevated(self):
        assert acwr_label(1.4) == "elevated"

    def test_spike(self):
        assert acwr_label(1.8) == "spike ⚠"

    def test_boundaries(self):
        assert acwr_label(0.8) == "balanced"   # lower boundary inclusive
        assert acwr_label(1.3) == "balanced"   # upper boundary inclusive
        assert acwr_label(1.5) == "elevated"   # upper boundary inclusive
