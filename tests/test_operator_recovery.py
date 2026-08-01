from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

import config
from operator_storage import DatabaseIntegrityError, discover_database_targets, inspect_sqlite
from verified_backup import BackupError, create_verified_backup, restore_plan, verify_verified_backup


def _configured(tmp_path: Path, monkeypatch):
    control = tmp_path / "control.db"; single = tmp_path / "single.db"; root = tmp_path / "users"
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control)
    monkeypatch.setattr(config, "DB_PATH", single)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", root)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)
    for path, ledger, key in ((control, "migration_versions", "version"), (single, "app_migrations", "migration_key")):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute(f"CREATE TABLE {ledger}({key} TEXT PRIMARY KEY)")
        connection.execute(f"INSERT INTO {ledger} VALUES ('base')")
        connection.commit(); connection.close()
    return control, single, root


def test_target_discovery_is_canonical_and_deduplicated(tmp_path, monkeypatch):
    control, single, root = _configured(tmp_path, monkeypatch)
    tenant = str(uuid4()); (root / tenant).mkdir(parents=True)
    sqlite3.connect(root / tenant / "athlete.db").close()
    (root / "not-a-uuid").mkdir()
    targets = discover_database_targets()
    assert [target.target_key for target in targets] == ["control", "single-user", f"tenant:{tenant}"]
    monkeypatch.setattr(config, "DB_PATH", control)
    assert [target.target_key for target in discover_database_targets()] == ["control", f"tenant:{tenant}"]


def test_inspection_is_read_only_and_malformed_fails_closed(tmp_path):
    path = tmp_path / "sample.db"; sqlite3.connect(path).execute("CREATE TABLE x(id INTEGER)").connection.close()
    before = path.read_bytes()
    assert inspect_sqlite(path).quick_check_ok is True
    assert path.read_bytes() == before
    path.write_bytes(b"not sqlite")
    with pytest.raises(DatabaseIntegrityError):
        from operator_storage import require_healthy_existing_database
        require_healthy_existing_database(path)
    assert path.read_bytes() == b"not sqlite"


def test_verified_backup_and_dry_run_are_mutation_free(tmp_path, monkeypatch):
    control, single, _root = _configured(tmp_path, monkeypatch)
    backup = create_verified_backup(tmp_path / "backups")
    verified = verify_verified_backup(backup, against_current_config=True)
    assert verified["verified"] is True
    plan = restore_plan(backup, against_current_config=True)
    assert plan["restorable"] is False
    assert {item["backup_file"] for item in plan["operations"]} == {"000-control.sqlite", "001-single-user.sqlite"}
    assert control.exists() and single.exists()
    (backup / "unexpected.sqlite").write_bytes(b"x")
    with pytest.raises(BackupError):
        verify_verified_backup(backup)
