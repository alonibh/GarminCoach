from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pytest
import sqlite3

import config
from guarded_restore import (
    EXIT_OPERATION_IN_PROGRESS,
    EXIT_PREPARATION_INCOMPLETE,
    EXIT_SUCCESS,
    RestoreLockError,
    RestoreStage,
    TargetRestoreState,
    load_restore_journal,
)
from guarded_restore_configured import (
    ConfiguredRestoreContext,
    ConfiguredRestoreError,
    ConfiguredRestorePreconditionError,
    prepare_configured_restore,
)
from guarded_restore_configured_staging import (
    ConfiguredPreflightError,
    preflight_disk_space,
    stage_configured_targets,
    verify_configured_readiness,
)
from operator_storage import (
    DatabaseTarget,
    TargetProfile,
    discover_database_targets,
    inspect_sqlite,
)
from process_lock import ProcessLock, acquire_process_lock, release_process_lock
from verified_backup import BackupLock, create_verified_backup


def _setup_test_env(tmp_path: Path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    backup_root = project_root / "operator_backups"
    backup_root.mkdir(parents=True, exist_ok=True)

    restore_root = project_root / "operator_restore_operations"
    restore_root.mkdir(parents=True, exist_ok=True)

    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Setup dummy configured database targets
    control_db = data_dir / "garmincoach.db"
    single_user_db = data_dir / "garminconnect.db"

    for db_path in (control_db, single_user_db):
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO sample (val) VALUES ('initial_data');")
        conn.commit()
        conn.close()

    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config, "OPERATOR_BACKUP_ROOT", backup_root)
    monkeypatch.setattr(config, "OPERATOR_RESTORE_ROOT", restore_root)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control_db)
    monkeypatch.setattr(config, "DB_PATH", single_user_db)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)

    return project_root, backup_root, restore_root, control_db, single_user_db


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def test_prepare_configured_restore_success_through_replacement_ready(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db = _setup_test_env(tmp_path, monkeypatch)

    # 1. Create a Phase 6A source verified backup
    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")

    # Record baseline state of configured database files
    control_bytes_before = control_db.read_bytes()
    single_user_bytes_before = single_user_db.read_bytes()
    control_sha_before = _sha256(control_db)
    single_user_sha_before = _sha256(single_user_db)
    control_mtime_before = control_db.stat().st_mtime_ns
    single_user_mtime_before = single_user_db.stat().st_mtime_ns

    # 2. Execute configured restore preparation through REPLACEMENT_READY
    ctx = prepare_configured_restore(
        source_backup_id,
        backup_root=backup_root,
        restore_root=restore_root,
        project_root=proj_root,
    )

    try:
        assert isinstance(ctx, ConfiguredRestoreContext)
        assert ctx.operation_id.startswith("restore-")
        assert ctx.journal.stage is RestoreStage.REPLACEMENT_READY
        assert ctx.selected_backup_id == source_backup_id
        assert ctx.safety_backup_id is not None
        assert ctx.safety_backup_id != source_backup_id

        # Assert all target states in journal are STAGED_VERIFIED
        for target_fact in ctx.journal.targets:
            assert target_fact.state is TargetRestoreState.STAGED_VERIFIED

        # Assert staged artifacts exist in operation directory and pass SQLite inspection
        op_dir = restore_root / f"operation-{ctx.operation_id}"
        assert op_dir.exists() and op_dir.is_dir()
        staged_files = [p for p in op_dir.iterdir() if p.name.endswith(".staged")]
        assert len(staged_files) == 2

        for staged_file in staged_files:
            check = inspect_sqlite(staged_file, deep=True)
            assert check.integrity_check_ok is True
            assert check.quick_check_ok is True

        # Assert configured database files were NOT modified or replaced
        assert control_db.read_bytes() == control_bytes_before
        assert single_user_db.read_bytes() == single_user_bytes_before
        assert _sha256(control_db) == control_sha_before
        assert _sha256(single_user_db) == single_user_sha_before
        assert control_db.stat().st_mtime_ns == control_mtime_before
        assert single_user_db.stat().st_mtime_ns == single_user_mtime_before

        # Assert process lock, restore lock, and long-held backup lock are held
        with pytest.raises(RuntimeError):
            acquire_process_lock(proj_root / "garmincoach.lock")

        with pytest.raises(RestoreLockError):
            with prepare_configured_restore(source_backup_id, backup_root=backup_root, restore_root=restore_root, project_root=proj_root):
                pass
    finally:
        ctx.close()

    # After close(), locks should be released cleanly
    proc_lock_after = acquire_process_lock(proj_root / "garmincoach.lock")
    release_process_lock(proc_lock_after)


def test_prepare_configured_restore_refuses_invalid_backup_id(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db = _setup_test_env(tmp_path, monkeypatch)

    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            "invalid-backup-id",
            backup_root=backup_root,
            restore_root=restore_root,
            project_root=proj_root,
        )


def test_prepare_configured_restore_refuses_missing_backup_dir(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db = _setup_test_env(tmp_path, monkeypatch)

    fake_id = "20260801T120000Z-99999999"
    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            fake_id,
            backup_root=backup_root,
            restore_root=restore_root,
            project_root=proj_root,
        )


def test_non_recursive_backup_lock_behavior(tmp_path, monkeypatch):
    """Verify BackupLock is acquired nonblockingly only AFTER safety backup creation."""
    proj_root, backup_root, restore_root, control_db, single_user_db = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")

    # If BackupLock is held BEFORE prepare_configured_restore begins,
    # public create_verified_backup() inside prepare_configured_restore must fail
    # because BackupLock is non-reentrant!
    with BackupLock(backup_root):
        with pytest.raises(ConfiguredRestoreError):
            prepare_configured_restore(
                source_backup_id,
                backup_root=backup_root,
                restore_root=restore_root,
                project_root=proj_root,
            )


def test_preflight_disk_space_failure(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    with pytest.raises(ConfiguredPreflightError):
        preflight_disk_space(targets, restore_root, multiplier=1000000000.0)
