from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

import config
from operator_storage import DatabaseIntegrityError, TargetProfile, discover_database_targets, inspect_sqlite
from verified_backup import BackupError, create_verified_backup, restore_plan, verify_verified_backup
from verified_backup import canonical_json


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
    assert [target.target_key for target in targets] == ["control", "single-user"]
    maintenance = discover_database_targets(profile=TargetProfile.ALL_CONFIGURED_MAINTENANCE)
    assert [target.target_key for target in maintenance] == ["control", "single-user", f"tenant:{tenant}"]
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
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


def _rewrite_manifest(backup: Path, mutate) -> None:
    import hashlib, json
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    body = canonical_json(manifest)
    manifest_path.write_bytes(body)
    (backup / "manifest.sha256").write_text(
        hashlib.sha256(body).hexdigest() + "  manifest.json\n", encoding="ascii"
    )


@pytest.mark.parametrize("mutate", [
    lambda manifest: manifest.update(databases=[]),
    lambda manifest: manifest["databases"].pop(0),
    lambda manifest: manifest["databases"][1].update(target_key="legacy-control-copy"),
    lambda manifest: manifest.update(completed_at="not-a-timestamp"),
    lambda manifest: manifest.update(control_user_tenant_mapping_after=[{"user_id": str(uuid4()), "target_key": "tenant:00000000-0000-0000-0000-000000000000"}]),
])
def test_strict_manifest_semantics_reject_invalid_backup_sets(tmp_path, monkeypatch, mutate):
    _configured(tmp_path, monkeypatch)
    backup = create_verified_backup(tmp_path / "backups")
    _rewrite_manifest(backup, mutate)
    with pytest.raises(BackupError):
        verify_verified_backup(backup)


def test_multi_user_runtime_excludes_stale_single_user_database(tmp_path, monkeypatch):
    control, _single, root = _configured(tmp_path, monkeypatch)
    tenant = str(uuid4()); tenant_db = root / tenant / "athlete.db"; tenant_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(tenant_db)
    connection.execute("CREATE TABLE app_migrations(migration_key TEXT PRIMARY KEY)")
    connection.commit(); connection.close()
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    backup = create_verified_backup(tmp_path / "backups")
    verified = verify_verified_backup(backup, against_current_config=True)
    assert verified["verified"] is True
    assert {entry["target_key"] for entry in __import__("json").loads((backup / "manifest.json").read_text())["databases"]} == {"control", f"tenant:{tenant}"}


def test_restore_plan_cross_profile_is_safe_without_compatibility_mode(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)
    backup = create_verified_backup(tmp_path / "backups")
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    plan = restore_plan(backup)
    assert plan["restorable"] is False
    assert any(item["configured_destination"] is None for item in plan["operations"])
    with pytest.raises(BackupError):
        restore_plan(backup, against_current_config=True)


def test_runtime_discovery_ignores_empty_tenant_but_backup_rejects_active_missing(tmp_path, monkeypatch):
    control, _single, root = _configured(tmp_path, monkeypatch)
    tenant = str(uuid4()); (root / tenant).mkdir(parents=True)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    assert [t.target_key for t in discover_database_targets()] == ["control"]
    connection = sqlite3.connect(control)
    connection.execute("CREATE TABLE users(id TEXT, status TEXT)")
    connection.execute("INSERT INTO users VALUES (?, 'active')", (tenant,))
    connection.commit(); connection.close()
    with pytest.raises(BackupError):
        create_verified_backup(tmp_path / "backups")
