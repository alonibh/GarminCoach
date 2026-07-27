import logging

from logging_filters import CalendarFeedAccessLogFilter


def _record(message, args=()):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, message, args, None
    )


def test_uvicorn_access_record_redacts_real_argument_structure():
    token = "A" * 43
    record = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", f"/calendar/feed/{token}.ics", "1.1", 200),
    )
    CalendarFeedAccessLogFilter().filter(record)
    rendered = record.getMessage()
    assert token not in rendered
    assert "/calendar/feed/***REDACTED***.ics" in rendered


def test_calendar_feed_access_filter_redacts_malformed_paths_without_touching_other_routes():
    secret = "malformed-private-path?unexpected=query"
    feed_record = _record(f'GET /calendar/feed/{secret} HTTP/1.1')
    normal_record = _record('GET /dashboard?view=week HTTP/1.1')
    filter_ = CalendarFeedAccessLogFilter()
    filter_.filter(feed_record)
    filter_.filter(normal_record)
    assert secret not in feed_record.getMessage()
    assert "/calendar/feed/***REDACTED***.ics" in feed_record.getMessage()
    assert normal_record.getMessage() == 'GET /dashboard?view=week HTTP/1.1'
