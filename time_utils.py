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
