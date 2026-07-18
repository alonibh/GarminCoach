"""Reviewed proactive metric-event rules.

Version 1 intentionally contains no ACWR, composite-readiness, sleep-debt, HRV,
or Body Battery alert. Those observations have no independently approved rule
that authorizes an unsolicited workout change or severe-anomaly message.
"""
from __future__ import annotations

from db import DailyMetrics


def check_and_notify_rules(metrics: DailyMetrics | None) -> list[str]:
    """Return emitted rule ids. Empty until a reviewed event rule is registered."""
    return []
