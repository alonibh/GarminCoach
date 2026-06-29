from sync.garmin_client import client
from sync.sync_service import _sync_activities
from db import SessionLocal
from datetime import date, timedelta
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    client.login()
    session = SessionLocal()
    
    start_date = date.today() - timedelta(days=60)
    end_date = date.today()
    
    print(f"Backfilling from {start_date} to {end_date}...")
    count = _sync_activities(session, start_date, end_date)
    print(f"Synced {count} activities.")
