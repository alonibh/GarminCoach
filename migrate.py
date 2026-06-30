import sqlite3

try:
    conn = sqlite3.connect('garmincoach.db')
    conn.execute('ALTER TABLE activities ADD COLUMN hr_zone_seconds TEXT')
    conn.commit()
    print("Migrated db to add hr_zone_seconds.")
except Exception as e:
    print(f"Migration error or already applied: {e}")

