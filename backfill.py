import datetime
from sync.garmin_client import client
from sync.sync_service import _sync_daily_health
from db import get_session

client.login()
with get_session() as session:
    for i in range(14):
        day = datetime.date.today() - datetime.timedelta(days=i)
        _sync_daily_health(session, day)
    session.commit()
    print("Backfilled last 14 days.")
