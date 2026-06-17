import db
from sqlalchemy.orm import Session

def fix():
    s = Session(db.engine)
    d = s.query(db.DailyHealth).filter(db.DailyHealth.resting_hr == None).delete()
    sl = s.query(db.Sleep).filter(db.Sleep.score == None).delete()
    k = s.query(db.SyncState).filter_by(key='last_sync_through').delete()
    s.commit()
    print(f"Deleted {d} empty health rows, {sl} empty sleep rows, and reset sync state.")

if __name__ == '__main__':
    fix()
