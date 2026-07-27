"""Logging safeguards for opaque URLs that must never reach access logs."""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping


_CALENDAR_FEED_PATH = re.compile(r"/calendar/feed/[^\s\"']*")
_REDACTED_CALENDAR_FEED_PATH = "/calendar/feed/***REDACTED***.ics"


def _redact_calendar_feed_path(value: object) -> object:
    if isinstance(value, str):
        return _CALENDAR_FEED_PATH.sub(_REDACTED_CALENDAR_FEED_PATH, value)
    if isinstance(value, tuple):
        return tuple(_redact_calendar_feed_path(item) for item in value)
    if isinstance(value, list):
        return [_redact_calendar_feed_path(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _redact_calendar_feed_path(item) for key, item in value.items()}
    return value


class CalendarFeedAccessLogFilter(logging.Filter):
    """Redact opaque calendar tokens from Uvicorn's access-log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's AccessFormatter stores the request path in ``args`` (for
        # example ``('%s - "%s %s HTTP/%s" %d', (..., path, ...))``), while
        # tests and alternate configurations can put it directly in ``msg``.
        record.msg = _redact_calendar_feed_path(record.msg)
        record.args = _redact_calendar_feed_path(record.args)
        return True


def install_calendar_feed_access_log_filter() -> None:
    """Attach this idempotent filter before the application serves requests."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, CalendarFeedAccessLogFilter) for item in logger.filters):
        logger.addFilter(CalendarFeedAccessLogFilter())
