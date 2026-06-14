import datetime
from sync.garmin_client import client
client.login()
from sync.sync_service import _sync_daily_health
from db import get_session

with get_session() as session:
    _sync_daily_health(session, datetime.date(2026, 6, 13))
    _sync_daily_health(session, datetime.date(2026, 6, 14))
    session.commit()
    from sqlalchemy import text
    res = session.execute(text("SELECT day, total_kcal, active_kcal, bmr_kcal FROM daily_health ORDER BY day DESC LIMIT 2")).fetchall()
    print("DB Result after sync:", res)
