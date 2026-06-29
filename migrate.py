import sqlite3

try:
    conn = sqlite3.connect('garmincoach.db')
    conn.execute('ALTER TABLE activities ADD COLUMN rpe INTEGER')
    conn.execute('ALTER TABLE activities ADD COLUMN feel INTEGER')
    conn.commit()
    print("Migrated db to add rpe and feel.")
except Exception as e:
    print(f"Migration error or already applied: {e}")
