from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import pytest

import config
from guarded_restore import (
    EXIT_INVALID_ARGUMENTS,
    EXIT_PRECONDITION_FAILED,
    EXIT_SUCCESS,
    create_restore_plan,
)
from operator_storage import TargetProfile, discover_database_targets, schema_fingerprint
from plan_verified_restore import main, plan_restore
from verified_backup import create_verified_backup


def _setup_fixture_env(tmp_path: Path, monkeypatch, *, multi_user: bool = False):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    
    control_db = project_root / "data" / "control.db"
    control_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(control_db)
    conn.execute("CREATE TABLE migration_versions (version TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO migration_versions VALUES ('v1')")
    if multi_user:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO users VALUES ('00000000-0000-0000-0000-000000000001', 'active')")
        conn.execute("INSERT INTO users VALUES ('00000000-0000-0000-0000-000000000002', 'active')")
    conn.commit()
    conn.close()

    single_db = project_root / "garmincoach.db"
    conn = sqlite3.connect(single_db)
    conn.execute("CREATE TABLE app_migrations (migration_key TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO app_migrations VALUES ('_BODY_COMPOSITION_CONTRACT_GATE_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_CAPABILITY_SCOPE_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_PROGRAM_DURATION_REVIEW_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_SLOW_METRIC_HISTORY_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_SOURCE_PROGRESSION_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_STRENGTH_PROGRESSION_FOUNDATION_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_STRENGTH_PROGRESSION_REVIEW_ACTIONS_MIGRATION_KEY')")
    conn.execute("INSERT INTO app_migrations VALUES ('_STRENGTH_PROGRESSION_TELEGRAM_NOTIFICATIONS_MIGRATION_KEY')")
    conn.commit()
    conn.close()

    tenant_root = project_root / "data" / "users"
    if multi_user:
        for tenant_id in ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]:
            tdir = tenant_root / tenant_id
            tdir.mkdir(parents=True, exist_ok=True)
            tdb = tdir / "athlete.db"
            conn = sqlite3.connect(tdb)
            conn.execute("CREATE TABLE app_migrations (migration_key TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO app_migrations VALUES ('_BODY_COMPOSITION_CONTRACT_GATE_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_CAPABILITY_SCOPE_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_PROGRAM_DURATION_REVIEW_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_SLOW_METRIC_HISTORY_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_SOURCE_PROGRESSION_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_STRENGTH_PROGRESSION_FOUNDATION_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_STRENGTH_PROGRESSION_REVIEW_ACTIONS_MIGRATION_KEY')")
            conn.execute("INSERT INTO app_migrations VALUES ('_STRENGTH_PROGRESSION_TELEGRAM_NOTIFICATIONS_MIGRATION_KEY')")
            conn.commit()
            conn.close()

    backup_root = project_root / "operator_backups"
    restore_root = project_root / "restore_journals"

    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control_db)
    monkeypatch.setattr(config, "DB_PATH", single_db)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tenant_root)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", multi_user)
    monkeypatch.setattr(config, "OPERATOR_BACKUP_ROOT", backup_root)
    monkeypatch.setattr(config, "OPERATOR_RESTORE_ROOT", restore_root)
    monkeypatch.setattr(os, "getcwd", lambda: str(project_root))
    monkeypatch.setattr(Path, "cwd", lambda: project_root)

    backup_dir = create_verified_backup(backup_root)
    backup_id = backup_dir.name.removeprefix("backup-")
    return project_root, backup_id, backup_dir


def test_plan_single_user_success(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)
    
    # Assert database timestamps/mtimes before planning
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    mtimes_before = {t.path: t.path.stat().st_mtime_ns for t in targets if t.path.exists()}

    output, code = plan_restore(backup_id)
    assert code == EXIT_SUCCESS

    data = json.loads(output)
    assert data["format_version"] == "garmincoach-guarded-restore-plan-v1"
    assert data["selected_backup_id"] == backup_id
    assert data["runtime_mode"] == "single_user"
    assert data["target_keys"] == ["control", "single-user"]
    assert "target_set_hash" in data
    assert "confirmation_value" in data
    assert "created_at" in data

    # Verify no absolute paths in default JSON output
    raw_str = json.dumps(data)
    assert str(root) not in raw_str
    assert "C:\\" not in raw_str and "c:\\" not in raw_str

    # Verify no database mutation
    for t in targets:
        if t.path.exists():
            assert t.path.stat().st_mtime_ns == mtimes_before[t.path]


def test_plan_multi_user_success(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=True)

    output, code = plan_restore(backup_id)
    assert code == EXIT_SUCCESS

    data = json.loads(output)
    assert data["format_version"] == "garmincoach-guarded-restore-plan-v1"
    assert data["selected_backup_id"] == backup_id
    assert data["runtime_mode"] == "multi_user"
    assert data["target_keys"] == [
        "control",
        "tenant:00000000-0000-0000-0000-000000000001",
        "tenant:00000000-0000-0000-0000-000000000002",
    ]


def test_plan_human_readable_output_and_show_local_paths(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    output_human, code = plan_restore(backup_id, human=True, show_local_paths=False)
    assert code == EXIT_SUCCESS
    assert "GarminCoach Guarded Restore Plan" in output_human
    assert f"Selected Backup ID:              {backup_id}" in output_human
    assert str(root) not in output_human

    output_paths, code = plan_restore(backup_id, human=True, show_local_paths=True)
    assert code == EXIT_SUCCESS
    assert "Local Paths (Diagnostic):" in output_paths
    assert str(backup_dir) in output_paths


def test_plan_cli_main_success(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    code = main(["--backup-id", backup_id])
    assert code == EXIT_SUCCESS

    code_pos = main([backup_id])
    assert code_pos == EXIT_SUCCESS


def test_plan_refuses_wrong_project_root(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)
    wrong_dir = tmp_path / "other_dir"
    wrong_dir.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: wrong_dir)

    code = main(["--backup-id", backup_id])
    assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_malformed_backup_id(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    for invalid in ["../bad_id", "bad/id", "123", "backup-invalid-uuid"]:
        code = main(["--backup-id", invalid])
        assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_missing_or_unverified_backup(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    code = main(["--backup-id", "20260803T090000Z-99999999"])
    assert code == EXIT_PRECONDITION_FAILED

    # Corrupt the backup manifest
    manifest_file = backup_dir / "manifest.json"
    manifest_file.write_text("{}")
    code = main(["--backup-id", backup_id])
    assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_modified_backup_database(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    db_file = backup_dir / "000-control.sqlite"
    with db_file.open("a+b") as f:
        f.write(b"corrupt")

    code = main(["--backup-id", backup_id])
    assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_runtime_mode_mismatch(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    # Switch config to multi-user after backup creation
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    code = main(["--backup-id", backup_id])
    assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_schema_or_migration_mismatch(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    # Mutate current database schema
    single_db = config.DB_PATH
    conn = sqlite3.connect(single_db)
    conn.execute("CREATE TABLE new_table (id INT)")
    conn.commit()
    conn.close()

    code = main(["--backup-id", backup_id])
    assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_invalid_expected_commit(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    code = main(["--backup-id", backup_id, "--expected-commit", "invalid commit!"])
    assert code == EXIT_PRECONDITION_FAILED


def test_plan_refuses_invalid_args(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    code = main([])
    assert code == EXIT_INVALID_ARGUMENTS


@pytest.mark.skipif(os.name == "nt", reason="Symlinks are not available unprivileged on Windows")
def test_plan_refuses_symlinked_backup_directory(tmp_path, monkeypatch):
    root, backup_id, backup_dir = _setup_fixture_env(tmp_path, monkeypatch, multi_user=False)

    symlink_dir = config.OPERATOR_BACKUP_ROOT / f"backup-20260803T090000Z-88888888"
    symlink_dir.symlink_to(backup_dir, target_is_directory=True)

    code = main(["--backup-id", "20260803T090000Z-88888888"])
    assert code == EXIT_PRECONDITION_FAILED
