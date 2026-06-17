import sqlite3
import config

def migrate():
    try:
        conn = sqlite3.connect(config.DB_PATH)
        cur = conn.cursor()
        
        # Check DailyHealth columns
        cur.execute("PRAGMA table_info(daily_health)")
        cols = [row[1] for row in cur.fetchall()]
        
        if 'training_readiness' not in cols:
            cur.execute("ALTER TABLE daily_health ADD COLUMN training_readiness INTEGER")
            print("Added training_readiness column to daily_health")
        if 'training_status' not in cols:
            cur.execute("ALTER TABLE daily_health ADD COLUMN training_status VARCHAR(32)")
            print("Added training_status column to daily_health")
            
        # Check Sleep columns
        cur.execute("PRAGMA table_info(sleep)")
        cols = [row[1] for row in cur.fetchall()]
        
        if 'respiration_avg' not in cols:
            cur.execute("ALTER TABLE sleep ADD COLUMN respiration_avg FLOAT")
            print("Added respiration_avg column to sleep")
        if 'sleep_stress_avg' not in cols:
            cur.execute("ALTER TABLE sleep ADD COLUMN sleep_stress_avg FLOAT")
            print("Added sleep_stress_avg column to sleep")
            
        conn.commit()
        conn.close()
        print("Database migration complete.")
    except Exception as e:
        print("Migration skipped or failed:", e)

if __name__ == "__main__":
    migrate()
