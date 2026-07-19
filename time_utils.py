from datetime import datetime, date
import os
import pytz

def get_local_tz():
    return pytz.timezone(os.getenv("USER_TIMEZONE", "Asia/Jerusalem"))

def get_local_now() -> datetime:
    """Returns the current datetime in the user's timezone."""
    return datetime.now(get_local_tz())

def get_local_date() -> date:
    """Returns the current date in the user's timezone."""
    return get_local_now().date()


def format_chat_datetime(value: datetime | str | None) -> str | None:
    """Format a timestamp for Telegram in the athlete's local timezone."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is not None:
        value = value.astimezone(get_local_tz())
    return value.strftime("%d/%m/%Y %H:%M")


def format_chat_date(value: date | datetime | str | None) -> str | None:
    """Format a calendar date for Telegram without exposing ISO dates."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d/%m/%Y")
