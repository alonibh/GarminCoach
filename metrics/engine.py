"""Deterministic metrics engine (Phase 2).

Currently a working stub that computes per-activity training load so Phase 1
has something real; readiness/ACWR/sleep-debt land in Phase 2. Pure math over
SQLite — no LLM, instant, same inputs -> same outputs.
"""
from __future__ import annotations

from db import Activity, get_session


def compute_training_load(avg_hr: float | None, duration_s: float | None) -> float | None:
    """Simple TRIMP-style load: avg_hr * minutes / 100. Refined in Phase 2."""
    if avg_hr is None or not duration_s:
        return None
    return round(avg_hr * (duration_s / 60.0) / 100.0, 1)


def recompute_all() -> None:
    """Recompute derived metrics. Called after every sync."""
    with get_session() as session:
        for act in session.query(Activity).all():
            act.training_load = compute_training_load(act.avg_hr, act.duration_s)
        # Phase 2: aggregate acute/chronic load, ACWR, readiness, sleep debt
        # into DailyMetrics here.
