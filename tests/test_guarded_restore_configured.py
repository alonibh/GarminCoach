from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pytest
import sqlite3
import subprocess

import config
from guarded_restore import (
    EXIT_OPERATION_IN_PROGRESS,
    EXIT_PREPARATION_INCOMPLETE,
    EXIT_SUCCESS,
    RestoreLockError,
    RestoreStage,
    TargetRestoreState,
    confirmation_value,
    load_restore_journal,
    target_set_hash,
)
from guarded_restore_configured import (
    ConfiguredRestoreError,
    ConfiguredRestorePreconditionError,
    ConfiguredRestorePreparationResult,
    prepare_configured_restore,
)
from guarded_restore_configured_staging import (
    ConfiguredPreflightError,
    ConfiguredStagingError,
    ConfiguredStagingPersistenceError,
    preflight_backup_disk_space,
    preflight_staging_disk_space,
)
from guarded_restore_replacement import SyntheticReplacementError as ReplacementSyntheticError
from guarded_restore_staging import SyntheticDestinationError as StagingSyntheticError, _validate_fixture_root
from operator_storage import (
    DatabaseTarget,
    TargetProfile,
    discover_database_targets,
    inspect_sqlite,
)
from process_lock import ProcessLock, acquire_process_lock, release_process_lock
from verified_backup import BackupLock, create_verified_backup, load_validated_backup_snapshot


def _setup_test_env(tmp_path: Path, monkeypatch, multi_user: bool = False):
    """Set up isolated test repository structure and monkeypatch config paths."""
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    backup_root = project_root / "operator_backups"
    backup_root.mkdir(parents=True, exist_ok=True)

    restore_root = project_root / "operator_restore_operations"
    restore_root.mkdir(parents=True, exist_ok=True)

    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    control_db = data_dir / "garmincoach.db"
    single_user_db = data_dir / "garminconnect.db"

    # Populate control DB
    conn = sqlite3.connect(control_db)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO sample (val) VALUES ('control_data');")
    conn.commit()
    conn.close()

    # Populate single-user DB
    conn = sqlite3.connect(single_user_db)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO sample (val) VALUES ('single_user_data');")
    conn.commit()
    conn.close()

    tenant_root = data_dir / "tenants"
    tenant_root.mkdir(parents=True, exist_ok=True)

    if multi_user:
        tenant_id = "11111111-1111-4111-8111-111111111111"
        t_dir = tenant_root / tenant_id
        t_dir.mkdir(parents=True, exist_ok=True)
        t_db = t_dir / "athlete.db"
        conn = sqlite3.connect(t_db)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO sample (val) VALUES ('tenant_data');")
        conn.commit()
        conn.close()

    monkeypatch.chdir(project_root)
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config, "OPERATOR_BACKUP_ROOT", backup_root)
    monkeypatch.setattr(config, "OPERATOR_RESTORE_ROOT", restore_root)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control_db)
    monkeypatch.setattr(config, "DB_PATH", single_user_db)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tenant_root)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", multi_user)

    # Mock git subprocess outputs for project root & HEAD
    commit_hex = "a" * 40

    def mock_check_output(cmd, **kwargs):
        if "rev-parse" in cmd and "--show-toplevel" in cmd:
            return str(project_root)
        if "rev-parse" in cmd and "HEAD" in cmd:
            return commit_hex
        return "ok"

    monkeypatch.setattr(subprocess, "check_output", mock_check_output)

    return project_root, backup_root, restore_root, control_db, single_user_db, commit_hex


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# -----------------------------------------------------------------------------
# 1. Confirmation Boundary & Project Root Tests
# -----------------------------------------------------------------------------

def test_single_user_configured_preparation_success(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch, multi_user=False)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )
    c_val = confirmation_value(
        target_hash=t_hash,
        expected_application_commit=commit_hex,
    )

    control_sha_before = _sha256(control_db)
    single_user_sha_before = _sha256(single_user_db)

    result = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )

    assert isinstance(result, ConfiguredRestorePreparationResult)
    assert result.stage is RestoreStage.REPLACEMENT_READY
    assert result.selected_backup_id == source_backup_id
    assert result.safety_backup_id != source_backup_id
    assert result.ready_for_future_apply is True
    assert result.configured_database_mutated is False
    assert result.locks_released is True

    # Assert configured database files were NOT mutated
    assert _sha256(control_db) == control_sha_before
    assert _sha256(single_user_db) == single_user_sha_before

    # Assert locks can immediately be reacquired by a separate caller!
    p_lock = acquire_process_lock(proj_root / "garmincoach.lock")
    release_process_lock(p_lock)


def test_confirmation_boundary_wrong_target_hash_refused(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")

    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash="0" * 64,
            confirmed_restore_value="1" * 64,
        )


def test_confirmation_boundary_wrong_restore_value_refused(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )

    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value="0" * 64,
        )


def test_exact_project_root_cwd_mismatch_refused(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")

    other_dir = tmp_path / "other"
    other_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(other_dir)

    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash="0" * 64,
            confirmed_restore_value="1" * 64,
        )


# -----------------------------------------------------------------------------
# 2. Multi-User Preparation Success Tests
# -----------------------------------------------------------------------------

def test_multi_user_configured_preparation_success(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch, multi_user=True)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="multi_user",
        target_keys=snapshot.target_keys,
    )
    c_val = confirmation_value(
        target_hash=t_hash,
        expected_application_commit=commit_hex,
    )

    result = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )

    assert result.stage is RestoreStage.REPLACEMENT_READY
    assert result.runtime_mode == "multi_user"
    assert result.locks_released is True


# -----------------------------------------------------------------------------
# 3. Lock Lifecycle & Non-Reentrancy Tests
# -----------------------------------------------------------------------------

def test_competing_process_lock_refusal(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )
    c_val = confirmation_value(
        target_hash=t_hash,
        expected_application_commit=commit_hex,
    )

    # Acquire process lock before calling prepare_configured_restore
    held_lock = acquire_process_lock(proj_root / "garmincoach.lock")
    try:
        with pytest.raises(RestoreLockError):
            prepare_configured_restore(
                selected_backup_id=source_backup_id,
                expected_application_commit=commit_hex,
                confirmed_target_set_hash=t_hash,
                confirmed_restore_value=c_val,
            )
    finally:
        release_process_lock(held_lock)


def test_non_recursive_backup_lock_behavior(tmp_path, monkeypatch):
    """Verify BackupLock is NOT held when create_verified_backup is called."""
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )
    c_val = confirmation_value(
        target_hash=t_hash,
        expected_application_commit=commit_hex,
    )

    # Holding BackupLock externally before prepare_configured_restore causes
    # public safety backup creation to fail because BackupLock is non-reentrant!
    with BackupLock(backup_root):
        with pytest.raises(ConfiguredRestoreError):
            prepare_configured_restore(
                selected_backup_id=source_backup_id,
                expected_application_commit=commit_hex,
                confirmed_target_set_hash=t_hash,
                confirmed_restore_value=c_val,
            )


# -----------------------------------------------------------------------------
# 4. Same-Filesystem Staging & Ownership Binding Tests
# -----------------------------------------------------------------------------

def test_same_filesystem_staging_and_ownership_binding(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )
    c_val = confirmation_value(
        target_hash=t_hash,
        expected_application_commit=commit_hex,
    )

    result = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )

    # Staging directory must be beside control_db: data/.garmincoach-restore-stage-<op_id>
    parent_dir = control_db.parent
    staged_dir = parent_dir / f".garmincoach-restore-stage-{result.operation_id}"

    assert staged_dir.exists() and staged_dir.is_dir()

    # Verify ownership binding metadata file exists inside staged_dir
    binding_file = staged_dir / ".staging-binding.json"
    assert binding_file.exists() and binding_file.is_file()

    binding_data = json.loads(binding_file.read_bytes().decode("utf-8"))
    assert binding_data["format_version"] == "garmincoach-restore-staging-binding-v1"
    assert binding_data["operation_id"] == result.operation_id
    assert binding_data["selected_backup_id"] == source_backup_id


# -----------------------------------------------------------------------------
# 5. Immutability & Tripwires Tests
# -----------------------------------------------------------------------------

def test_configured_database_and_sidecar_bytes_remain_100_percent_unchanged(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    source_backup_dir = create_verified_backup(output_root=backup_root)
    source_backup_id = source_backup_dir.name.removeprefix("backup-")
    snapshot = load_validated_backup_snapshot(source_backup_dir, against_current_config=True)

    t_hash = target_set_hash(
        backup_id=source_backup_id,
        manifest_sha256=snapshot.manifest_sha256,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )
    c_val = confirmation_value(
        target_hash=t_hash,
        expected_application_commit=commit_hex,
    )

    control_bytes_before = control_db.read_bytes()
    single_bytes_before = single_user_db.read_bytes()

    result = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )

    assert control_db.read_bytes() == control_bytes_before
    assert single_user_db.read_bytes() == single_bytes_before


def test_synthetic_apis_continue_to_reject_configured_paths(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    # Synthetic staging root validator must refuse configured paths
    with pytest.raises(StagingSyntheticError):
        _validate_fixture_root(control_db.parent, backup_root)
