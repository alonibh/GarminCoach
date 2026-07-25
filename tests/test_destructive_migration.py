import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import config
import db_migration

USER_ID = "00000000-0000-0000-0000-000000000001"


def _database(path: Path, *, obsolete: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE durable_data(value TEXT)")
        connection.execute("INSERT INTO durable_data VALUES ('preserve me')")
        if obsolete:
            for table in db_migration.OBSOLETE_TABLES:
                connection.execute(f'CREATE TABLE "{table}"(value TEXT)')


def test_migration_backs_up_verifies_drops_and_is_idempotent(
    tmp_path, monkeypatch
):
    control = tmp_path / "control.db"
    legacy = tmp_path / "legacy.db"
    root = tmp_path / "users"
    athlete = root / USER_ID / "athlete.db"
    _database(control)
    _database(legacy, obsolete=True)
    _database(athlete, obsolete=True)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control)
    monkeypatch.setattr(config, "DB_PATH", legacy)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", root)
    backup_root = tmp_path / "backups"

    manifest_path = db_migration.run_destructive_migrations(backup_root)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["databases"]) == 3
    for entry in manifest["databases"]:
        assert entry["status"] == "migrated"
        backup = Path(entry["backup_path"])
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == entry["sha256"]
        with sqlite3.connect(entry["source_path"]) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if Path(entry["source_path"]) != control:
            assert not set(db_migration.OBSOLETE_TABLES) & tables
    with sqlite3.connect(control) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ask_coach_consents)"
            )
        }
    assert "data_categories_version" in columns
    assert db_migration.run_destructive_migrations(tmp_path / "other") is None


def test_migration_failure_restores_all_databases_and_removes_sidecars(
    tmp_path, monkeypatch
):
    control = tmp_path / "control.db"
    legacy = tmp_path / "legacy.db"
    root = tmp_path / "users"
    athlete = root / USER_ID / "athlete.db"
    _database(control)
    _database(legacy, obsolete=True)
    _database(athlete, obsolete=True)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control)
    monkeypatch.setattr(config, "DB_PATH", legacy)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", root)

    def fail(_connection):
        Path(f"{athlete}-wal").write_bytes(b"stale")
        Path(f"{athlete}-shm").write_bytes(b"stale")
        raise RuntimeError("forced migration failure")

    monkeypatch.setattr(db_migration, "_create_consent_table", fail)
    with pytest.raises(RuntimeError):
        db_migration.run_destructive_migrations(tmp_path / "backups")

    assert not Path(f"{athlete}-wal").exists()
    assert not Path(f"{athlete}-shm").exists()
    for path in (legacy, athlete):
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            value = connection.execute(
                "SELECT value FROM durable_data"
            ).fetchone()[0]
        assert set(db_migration.OBSOLETE_TABLES) <= tables
        assert value == "preserve me"
