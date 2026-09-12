"""Daily Coach Insight scheduled job and pre-generation atomic reservation."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
from coach.advisory_snapshot import (
    build_advisory_snapshot,
    serialize_advisory_snapshot,
)
from coach.daily_insight_llm import (
    DailyInsightLLMError,
    generate_daily_coach_insight,
    render_daily_insight_telegram_text,
)
from control_db import (
    User,
    get_control_session,
    is_daily_insight_authorized,
)
from db import NotificationOutbox, get_session
from tenant_context import current_tenant

logger = logging.getLogger("garmincoach.daily_insight")


def reserve_daily_insight_claim(
    session: Session,
    local_date: date,
    now: datetime,
) -> NotificationOutbox | None:
    """Atomically reserve generation for local_date; returns claim row if acquired, else None."""
    idempotency_key = f"daily-insight:{local_date.isoformat()}"
    existing = (
        session.query(NotificationOutbox)
        .filter_by(idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        if existing.status == "generating":
            claim_age = now - existing.created_at
            if claim_age > timedelta(minutes=5):
                logger.warning(
                    "Recovering abandoned daily insight claim for %s (age %s); marking failed",
                    local_date,
                    claim_age,
                )
                existing.status = "failed"
                existing.last_error = "abandoned_generation"
                session.commit()
        return None

    claim_row = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=now,
        quiet_hour_policy="cancel",
        payload_json=json.dumps({"local_date": local_date.isoformat(), "text": ""}),
        status="generating",
        attempts=0,
        idempotency_key=idempotency_key,
        created_at=now,
    )
    session.add(claim_row)
    try:
        session.commit()
        return claim_row
    except IntegrityError:
        session.rollback()
        logger.info(
            "Another worker acquired daily insight reservation for %s",
            local_date,
        )
        return None


def run_daily_coach_insight_job(
    user_id: str | None = None,
    *,
    now_utc: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """Scheduled entry point for Daily Coach Insight.

    Runs Sunday through Friday at 21:00 local time.
    Skips Saturday entirely.
    Requires active user, linked Telegram, and valid AI consent/opt-in.
    Acquires an atomic pre-generation claim before calling Gemini.
    """
    effective_user_id = user_id
    if effective_user_id is None:
        tenant = current_tenant()
        if tenant is not None:
            effective_user_id = tenant.user_id

    # In multi-user mode, verify account status and telegram linking
    if config.MULTI_USER_ENABLED:
        if effective_user_id is None:
            logger.info("Daily insight skipped: no tenant identity available")
            return False
        with get_control_session() as control_session:
            user = control_session.get(User, effective_user_id)
            if user is None or user.status != "active" or not user.telegram_linked:
                logger.info(
                    "Daily insight skipped for user %s: inactive or unlinked",
                    effective_user_id,
                )
                return False

    user_key = effective_user_id or "default_user"
    if not is_daily_insight_authorized(user_key):
        logger.info("Daily insight skipped: missing or revoked consent/opt-in")
        return False

    if session is not None:
        return _generate_insight_with_session(session, effective_user_id, now_utc)

    with get_session() as db_session:
        return _generate_insight_with_session(db_session, effective_user_id, now_utc)


def generate_daily_coach_insight_for_date(
    session: Session,
    user_id: str | None = None,
    now_utc: datetime | None = None,
) -> bool:
    """Helper for manual/test generation for a specific date/session."""
    effective_user_id = user_id or (current_tenant().user_id if current_tenant() else "default_user")
    if not is_daily_insight_authorized(effective_user_id):
        return False
    return _generate_insight_with_session(session, effective_user_id, now_utc)


def _generate_insight_with_session(
    session: Session,
    effective_user_id: str | None,
    now_utc: datetime | None,
) -> bool:
    user_key = effective_user_id or "default_user"
    if not is_daily_insight_authorized(user_key):
        logger.info("Daily insight skipped: missing or revoked consent/opt-in")
        return False

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    # 1. Build bounded local snapshot using existing API and timezone-aware generation instant
    try:
        snapshot = build_advisory_snapshot(session, generated_at=now_utc)
        local_date_str = snapshot.get("date_context", {}).get("local_day")
        if not local_date_str:
            logger.error("Daily insight snapshot missing local_day")
            return False
        local_date = date.fromisoformat(local_date_str)
    except Exception as exc:
        logger.error("Failed to build advisory snapshot for daily insight: %s", type(exc).__name__)
        return False

    # 2. Saturday check: skip Gemini entirely on Saturday (weekly summary runs Saturday at 20:00)
    if local_date.weekday() == 5:
        logger.info("Daily insight skipped on Saturday for %s", local_date)
        return False

    # 3. Establish durable atomic reservation before calling Gemini
    naive_now = now_utc.replace(tzinfo=None)
    claim = reserve_daily_insight_claim(session, local_date, naive_now)
    if claim is None:
        logger.info(
            "Daily insight already claimed, generated, or completed for %s",
            local_date,
        )
        return False

    # 4. Serialize snapshot under the privacy ceiling (no raw series, calendar events, or GPS)
    try:
        snapshot_json = serialize_advisory_snapshot(snapshot)
    except Exception as exc:
        logger.error("Failed to serialize advisory snapshot: %s", type(exc).__name__)
        claim.status = "failed"
        claim.last_error = "snapshot_serialization_failed"
        session.commit()
        return False

    # 5. Invoke Gemini with strict Pydantic validation
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                generate_daily_coach_insight(
                    user_id=effective_user_id or "single_user",
                    snapshot_json=snapshot_json,
                ),
                loop,
            )
            response = future.result(timeout=config.DAILY_INSIGHT_TIMEOUT_SECONDS + 5)
        else:
            response = asyncio.run(
                generate_daily_coach_insight(
                    user_id=effective_user_id or "single_user",
                    snapshot_json=snapshot_json,
                )
            )
    except Exception as exc:
        category = getattr(exc, "category", type(exc).__name__)
        logger.warning("Daily insight Gemini generation failed: %s", category)
        claim.status = "failed"
        claim.last_error = category or "generation_failed"
        session.commit()
        return False

    # 6. Success: format rendered text and transition claim to pending for outbox delivery
    rendered_text = render_daily_insight_telegram_text(response)
    payload_data = {"local_date": local_date.isoformat(), "text": rendered_text}
    if effective_user_id:
        payload_data["user_id"] = effective_user_id
    claim.payload_json = json.dumps(
        payload_data,
        sort_keys=True,
    )
    claim.status = "pending"
    claim.due_at = naive_now
    session.commit()
    logger.info(
        "Daily insight successfully generated and queued for %s (mode=%s)",
        local_date,
        response.mode,
    )
    return True
