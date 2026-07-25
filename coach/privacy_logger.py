"""Metadata-only logging for Ask Coach."""
from __future__ import annotations

import logging

logger = logging.getLogger("garmincoach.ask_coach")


def log_generation_metadata(
    *,
    user_id: str,
    model: str,
    response_type: str | None,
    latency_ms: int,
    http_status: int | None,
    input_chars: int,
    output_chars: int,
    retry_count: int,
    validation_result: str,
) -> None:
    logger.info(
        "ask_coach_generation user_id=%s model=%s response_type=%s "
        "latency_ms=%d http_status=%s input_chars=%d output_chars=%d "
        "retry_count=%d validation_result=%s",
        user_id,
        model,
        response_type or "none",
        latency_ms,
        http_status if http_status is not None else "none",
        input_chars,
        output_chars,
        retry_count,
        validation_result,
    )


def log_sanitized_error(
    category: str,
    *,
    user_id: str | None = None,
    http_status: int | None = None,
) -> None:
    logger.warning(
        "ask_coach_error category=%s user_id=%s http_status=%s",
        category,
        user_id or "none",
        http_status if http_status is not None else "none",
    )


def log_sanitized_cleanup_error(name: str, exc: BaseException) -> None:
    logger.error(
        "ask_coach_cleanup_failed step=%s exception_type=%s",
        name,
        type(exc).__name__,
    )
