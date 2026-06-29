from __future__ import annotations

import logging
from datetime import date, timedelta
from sqlalchemy import func

from db import SessionLocal, Activity, DailyMetrics
from coach.llm import LLMClient
from notify.telegram import send_message

logger = logging.getLogger(__name__)

def send_weekly_summary() -> None:
    """Run on Sunday evening to generate a weekly summary."""
    try:
        with SessionLocal() as db:
            today = date.today()
            seven_days_ago = today - timedelta(days=7)
            
            # Get workouts from the last 7 days
            activities = db.query(Activity).filter(
                Activity.start_time >= seven_days_ago.isoformat()
            ).all()
            
            workout_count = len(activities)
            total_volume = sum(
                float(a.summary_data.get("total_volume", 0)) if a.summary_data else 0
                for a in activities
            )
            
            # Get latest metrics
            latest_metrics = db.query(DailyMetrics).order_by(DailyMetrics.date.desc()).first()
            if latest_metrics:
                acwr = latest_metrics.acwr
                sleep_debt = latest_metrics.sleep_debt_h
            else:
                acwr = None
                sleep_debt = None
                
            # Construct AI prompt for summary
            prompt = f"""
Generate a brief, encouraging Weekly Summary for the user's training.
Data for the past 7 days:
- Workouts completed: {workout_count}
- Total volume: {total_volume} kg
- Current ACWR: {acwr}
- Current Sleep Debt: {sleep_debt} hours

Format as a single paragraph. No greetings, just the summary.
Use emojis sparingly but effectively.
CRITICAL: Do NOT output the numeric value for ACWR in the summary. Instead, translate it to a verbal description (e.g., 'optimal load', 'undertraining', 'high load').
"""
            llm = LLMClient()
            summary_text = llm.generate("You are an expert fitness coach.", prompt)
            
            msg = f"📊 *Weekly Summary*\n\n{summary_text}"
            send_message(msg)
            logger.info("Sent weekly summary")
            
    except Exception as e:
        logger.error(f"Failed to generate weekly summary: {e}")

if __name__ == "__main__":
    send_weekly_summary()
