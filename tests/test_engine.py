"""Unit tests for metrics.engine — pure functions, no DB or network."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

# Make the project root importable (tests/ is one level down).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metrics.engine import (
    SLEEP_TARGET_HOURS,
    acwr_label,
    compute_daily_loads,
    compute_readiness,
    compute_sleep_debt,
    compute_training_load,
)


# ---------------------------------------------------------------------------
# 1. Training load
# ---------------------------------------------------------------------------

class TestTrainingLoad:
    def test_basic_trimp(self):
        # 140 avg_hr, 60 min (3600s) → 140 * 60 / 100 = 84.0
        assert compute_training_load(140.0, 3600.0) == 84.0

    def test_none_avg_hr(self):
        assert compute_training_load(None, 3600.0) is None

    def test_none_duration(self):
        assert compute_training_load(140.0, None) is None

    def test_zero_duration(self):
        assert compute_training_load(140.0, 0.0) is None

    def test_zone_weighted(self):
        # 5 zones, 600s (10 min) each.
        # Load = (600*1 + 600*1.5 + 600*2 + 600*3.5 + 600*5) / 60 / 100
        #       = (600 + 900 + 1200 + 2100 + 3000) / 60 / 100
        #       = 7800 / 6000 = 1.3
        zones = [600.0, 600.0, 600.0, 600.0, 600.0]
        result = compute_training_load(140.0, 3600.0, hr_zone_seconds=zones)
        assert result == 1.3

    def test_zone_weighted_overrides_trimp(self):
        """When zones are provided, avg_hr fallback is not used."""
        zones = [300.0, 300.0, 300.0, 300.0, 300.0]
        with_zones = compute_training_load(140.0, 3600.0, hr_zone_seconds=zones)
        without_zones = compute_training_load(140.0, 3600.0)
        assert with_zones != without_zones

    def test_insufficient_zones_falls_back(self):
        """If fewer than 5 zone values, fall back to simple TRIMP."""
        zones = [600.0, 600.0]  # only 2 zones
        result = compute_training_load(140.0, 3600.0, hr_zone_seconds=zones)
        # Should fall back to simple TRIMP: 140 * 60 / 100 = 84.0
        assert result == 84.0


# ---------------------------------------------------------------------------
# 2. Daily loads + ACWR
# ---------------------------------------------------------------------------

class TestDailyLoads:
    def test_basic_acwr(self):
        today = date(2025, 6, 10)
        # Create a map: 7 days of load 10, 21 preceding days of load 5.
        load_map: dict[date, float] = {}
        for i in range(1, 8):
            load_map[today - timedelta(days=i)] = 10.0
        for i in range(8, 29):
            load_map[today - timedelta(days=i)] = 5.0

        acute, chronic, acwr = compute_daily_loads(load_map, today)

        assert acute == 10.0  # mean of 7 days × 10.0
        # chronic = (7*10 + 21*5) / 28 = (70 + 105) / 28 = 6.25
        assert chronic == 6.2  # rounded to 1 decimal
        assert acwr == pytest.approx(10.0 / 6.2, abs=0.02)

    def test_all_rest_days(self):
        today = date(2025, 6, 10)
        acute, chronic, acwr = compute_daily_loads({}, today)
        assert acute == 0.0
        assert chronic == 0.0
        assert acwr is None  # division by zero → None

    def test_rest_days_count_as_zero(self):
        today = date(2025, 6, 10)
        # Only one day with load in the acute window.
        load_map = {today - timedelta(days=1): 70.0}
        acute, chronic, acwr = compute_daily_loads(load_map, today)
        assert acute == 10.0  # 70 / 7


# ---------------------------------------------------------------------------
# 3. Readiness
# ---------------------------------------------------------------------------

class TestReadiness:
    def test_all_at_baseline(self):
        """All metrics at baseline → score ≈ 50 for HRV/RHR, 100 for sleep,
        some BB. The exact value depends on the weights."""
        score = compute_readiness(
            hrv=50.0, hrv_baseline=50.0,
            rhr=60.0, rhr_baseline=60.0,
            sleep_hours=8.0, sleep_target=8.0,
            bb_low=50.0,
        )
        assert score is not None
        # HRV at baseline → 50, RHR at baseline → 50, sleep = 100, BB = 50
        # Weighted: 50*.4 + 100*.25 + 50*.2 + 50*.15 = 20+25+10+7.5 = 62.5
        assert score == 62.0  # rounded

    def test_all_excellent(self):
        """Above-baseline HRV, below-baseline RHR, full sleep, high BB."""
        score = compute_readiness(
            hrv=70.0, hrv_baseline=50.0,
            rhr=50.0, rhr_baseline=60.0,
            sleep_hours=9.0, sleep_target=8.0,
            bb_low=90.0,
        )
        assert score is not None
        assert score >= 75  # should be well above baseline

    def test_missing_hrv(self):
        """HRV missing → renormalize over remaining components."""
        score = compute_readiness(
            hrv=None, hrv_baseline=None,
            rhr=60.0, rhr_baseline=60.0,
            sleep_hours=8.0, sleep_target=8.0,
            bb_low=50.0,
        )
        assert score is not None
        # Only RHR (.20), sleep (.25), BB (.15) present; renormalized.

    def test_all_none(self):
        score = compute_readiness(
            hrv=None, hrv_baseline=None,
            rhr=None, rhr_baseline=None,
            sleep_hours=None, sleep_target=8.0,
            bb_low=None,
        )
        assert score is None

    def test_clamped_to_100(self):
        """Extreme inputs don't exceed 100."""
        score = compute_readiness(
            hrv=200.0, hrv_baseline=50.0,
            rhr=30.0, rhr_baseline=60.0,
            sleep_hours=12.0, sleep_target=8.0,
            bb_low=100.0,
        )
        assert score is not None
        assert score <= 100

    def test_clamped_to_0(self):
        """Extreme bad inputs don't go below 0."""
        score = compute_readiness(
            hrv=10.0, hrv_baseline=100.0,
            rhr=120.0, rhr_baseline=60.0,
            sleep_hours=2.0, sleep_target=8.0,
            bb_low=5.0,
        )
        assert score is not None
        assert score >= 0


# ---------------------------------------------------------------------------
# 4. Sleep debt
# ---------------------------------------------------------------------------

class TestSleepDebt:
    def test_no_debt(self):
        """Getting 8+ hours every night → 0 debt."""
        history = [8.5, 8.0, 9.0, 8.0, 8.2, 8.0, 8.0]
        assert compute_sleep_debt(history) == 0.0

    def test_accumulation(self):
        """6h/night for 7 nights → 7 * 2 = 14h debt."""
        history = [6.0] * 7
        assert compute_sleep_debt(history) == 14.0

    def test_none_skipped(self):
        """None entries are skipped, not counted as 0 sleep."""
        history = [None, 8.0, None, 8.0]
        assert compute_sleep_debt(history) == 0.0

    def test_capped(self):
        """Debt doesn't exceed the cap (30h)."""
        history = [0.0] * 14  # 0 sleep for 14 days = 14*8 = 112h, capped to 30.
        assert compute_sleep_debt(history) == 30.0

    def test_empty(self):
        assert compute_sleep_debt([]) == 0.0

    def test_window_limited(self):
        """Only the first 14 entries are used."""
        # 20 entries of 6h; only 14 counted → 14 * 2 = 28h.
        history = [6.0] * 20
        assert compute_sleep_debt(history) == 28.0


# ---------------------------------------------------------------------------
# 5. ACWR label
# ---------------------------------------------------------------------------

class TestAcwrLabel:
    def test_none(self):
        assert acwr_label(None) == ""

    def test_detraining(self):
        assert acwr_label(0.5) == "detraining"

    def test_balanced(self):
        assert acwr_label(1.0) == "balanced"

    def test_ramping(self):
        assert acwr_label(1.4) == "ramping"

    def test_spike(self):
        assert acwr_label(1.8) == "spike ⚠"

    def test_boundaries(self):
        assert acwr_label(0.8) == "balanced"  # lower boundary inclusive
        assert acwr_label(1.3) == "balanced"  # upper boundary inclusive
        assert acwr_label(1.5) == "ramping"   # upper boundary inclusive
