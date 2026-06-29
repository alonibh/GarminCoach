from __future__ import annotations

import logging
from db import DailyMetrics
from notify import telegram

logger = logging.getLogger(__name__)

def check_and_notify_rules(metrics: DailyMetrics | None) -> None:
    """Run deterministic rules against the latest metrics and send Telegram alerts if thresholds are breached."""
    if not metrics:
        return
        
    messages = []
    
    # 1. Sleep Debt Warning
    if metrics.sleep_debt_hours is not None and metrics.sleep_debt_hours > 3.0:
        messages.append(
            f"⚠️ *Recovery Alert*\nYour sleep debt has accumulated to {round(metrics.sleep_debt_hours, 1)} hours. "
            "Consider going to bed 30-60 mins earlier tonight to protect your muscle recovery."
        )
        
    # 2. Over-training Alert
    acwr = metrics.acwr_ratio
    readiness = metrics.readiness_score_0_to_100
    if acwr is not None and readiness is not None:
        if acwr > 1.5 or readiness < 40:
            messages.append(
                f"🛑 *High Load Alert*\nYour ACWR is {round(acwr, 2)} and Readiness is {round(readiness, 1)}. "
                "You are entering the over-training zone. Take it easy over the next 48 hours."
            )
            
    # Send all generated alerts
    for msg in messages:
        try:
            telegram.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send rule-based Telegram alert: {e}")
