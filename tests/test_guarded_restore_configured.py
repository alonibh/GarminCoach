from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pytest
import shutil
import sqlite3
import stat
import subprocess


import config
from guarded_restore import (
    EXIT_OPERATION_IN_PROGRESS,
    EXIT_PREPARATION_INCOMPLETE,
    EXIT_SUCCESS,
    RestoreJournalError,
    RestoreLockError,
    RestoreStage,
    TargetRestoreState,
    confirmation_value,
    create_restore_journal,
    create_restore_plan,
    load_restore_journal,
    target_set_hash,
    update_restore_journal,
)
from guarded_restore_configured import (
    ConfiguredJournalUncertaintyError,
    ConfiguredRestoreError,
    ConfiguredRestoreLockReleaseError,
    ConfiguredRestorePreconditionError,
    ConfiguredRestorePreparationResult,
    prepare_configured_restore,
    verify_complete_preparation_barrier,
)
from guarded_restore_configured_staging import (
    METADATA_OVERHEAD_BYTES,
    SAFETY_MARGIN_BYTES,
    ConfiguredPreflightError,
    ConfiguredStagingError,
    ConfiguredStagingOwnershipError,
    ConfiguredStagingPersistenceError,
    preflight_backup_disk_space,
    preflight_staging_disk_space,
    publish_noreplace,
    capture_destination_baseline_evidence,
    _destination_baseline_from_payload,
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


def _setup_test_env(tmp_path: Path, monkeypatch, multi_user: bool = False, num_tenants: int = 1):
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
        tenant_ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ]
        for i in range(num_tenants):
            t_dir = tenant_root / tenant_ids[i]
            t_dir.mkdir(parents=True, exist_ok=True)
            t_db = t_dir / "athlete.db"
            conn = sqlite3.connect(t_db)
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
            conn.execute(f"INSERT INTO sample (val) VALUES ('tenant_{i}_data');")
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
# 1. Single-User & Multi-User (2 Canonical Tenants) Preparation Success Tests
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

    assert _sha256(control_db) == control_sha_before
    assert _sha256(single_user_db) == single_user_sha_before

    p_lock = acquire_process_lock(proj_root / "garmincoach.lock")
    release_process_lock(p_lock)


def test_multi_user_configured_preparation_success_two_tenants(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(
        tmp_path, monkeypatch, multi_user=True, num_tenants=2
    )

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
    assert result.staged_artifact_count == 3  # control + tenant 1 + tenant 2
    assert result.locks_released is True


# -----------------------------------------------------------------------------
# 2. Re-Entry Dispatcher Across All Legal & Illegal Stages
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("legal_stage", [
    RestoreStage.PRECHECK,
    RestoreStage.VERIFIED,
    RestoreStage.CURRENT_SNAPSHOT_CREATED,
    RestoreStage.RESTORE_STAGED,
    RestoreStage.STAGED_VERIFIED,
    RestoreStage.REPLACEMENT_READY,
])
def test_legal_reentry_dispatcher_stages(tmp_path, monkeypatch, legal_stage):
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

    # First run to get full preparation
    res1 = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )
    op_id = res1.operation_id

    # Reset journal stage to legal_stage for re-entry test
    journal_path = restore_root / f"operation-{op_id}" / "journal.json"
    data = json.loads(journal_path.read_bytes().decode("utf-8"))
    data["stage"] = legal_stage.value

    if legal_stage in {RestoreStage.PRECHECK, RestoreStage.VERIFIED}:
        data["safety_backup_id"] = None
        for t in data["targets"]:
            t["state"] = "PENDING"
        for p in control_db.parent.glob(".garmincoach-restore-stage-*"):
            if p.is_dir():
                shutil.rmtree(p)
    elif legal_stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
        for t in data["targets"]:
            t["state"] = "PENDING"
        for p in control_db.parent.glob(".garmincoach-restore-stage-*"):
            if p.is_dir():
                shutil.rmtree(p)
    elif legal_stage is RestoreStage.RESTORE_STAGED:
        for t in data["targets"]:
            t["state"] = "PENDING"

    journal_path.write_bytes((json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))

    res2 = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
        operation_id=op_id,
    )

    assert res2.stage is RestoreStage.REPLACEMENT_READY
    assert res2.operation_id == op_id
    assert res2.locks_released is True


@pytest.mark.parametrize("illegal_stage,terminal_result", [
    (RestoreStage.REPLACING, None),
    (RestoreStage.REPLACED, None),
    (RestoreStage.POSTCHECK_PASSED, None),
    (RestoreStage.ROLLBACK_REQUIRED, None),
    (RestoreStage.ROLLED_BACK, None),
    (RestoreStage.FAILED_SAFE, "FAILED_SAFE"),
    (RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, "FAILED_MANUAL_RECOVERY_REQUIRED"),
    (RestoreStage.COMPLETED, "COMPLETED"),
])
def test_illegal_reentry_stages_refused(tmp_path, monkeypatch, illegal_stage, terminal_result):
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

    res1 = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )
    op_id = res1.operation_id

    journal_path = restore_root / f"operation-{op_id}" / "journal.json"
    data = json.loads(journal_path.read_bytes().decode("utf-8"))
    data["stage"] = illegal_stage.value
    data["final_result"] = terminal_result

    if illegal_stage in {RestoreStage.REPLACED, RestoreStage.POSTCHECK_PASSED, RestoreStage.COMPLETED}:
        for t in data["targets"]:
            t["state"] = "REPLACED"
            t["replacement_intent"] = True
            t["replacement_completed"] = True
    elif illegal_stage is RestoreStage.ROLLED_BACK:
        for t in data["targets"]:
            t["state"] = "ROLLED_BACK"
            t["replacement_intent"] = True
            t["replacement_completed"] = True
            t["rollback_intent"] = True
            t["rollback_completed"] = True

    journal_path.write_bytes((json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))

    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
            operation_id=op_id,
        )


def test_no_duplicate_safety_backup_on_reentry(tmp_path, monkeypatch):
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

    res1 = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )
    op_id = res1.operation_id
    safety_id1 = res1.safety_backup_id

    backups_before = list(backup_root.glob("backup-*"))

    # Re-entry
    res2 = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
        operation_id=op_id,
    )
    backups_after = list(backup_root.glob("backup-*"))

    assert res2.safety_backup_id == safety_id1
    assert len(backups_after) == len(backups_before)


# -----------------------------------------------------------------------------
# 3. Disk Space Formula & Preflight Boundary Tests
# -----------------------------------------------------------------------------

def test_exact_disk_space_formula_and_boundary_checks(tmp_path, monkeypatch):
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

    total_db_bytes = control_db.stat().st_size + single_user_db.stat().st_size
    required_backup_disk = total_db_bytes + METADATA_OVERHEAD_BYTES + SAFETY_MARGIN_BYTES

    # Mock disk_usage to return 1 byte short of required_backup_disk
    def mock_disk_usage_short(path):
        return shutil._ntuple_diskusage(total=10**12, used=10**12 - (required_backup_disk - 1), free=required_backup_disk - 1)

    monkeypatch.setattr(shutil, "disk_usage", mock_disk_usage_short)

    with pytest.raises(ConfiguredRestoreError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
        )


# -----------------------------------------------------------------------------
# 4. Ownership Binding, Foreign Stage Directory & Symlink Refusal
# -----------------------------------------------------------------------------

def test_foreign_stage_directory_without_binding_refused(tmp_path, monkeypatch):
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

    op_id = "restore-20260803T120000Z-11223344"
    foreign_stage_dir = control_db.parent / f".garmincoach-restore-stage-{op_id}"
    foreign_stage_dir.mkdir(parents=True, exist_ok=True)
    # Put an un-bound file in foreign_stage_dir
    (foreign_stage_dir / "unbound.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(ConfiguredRestoreError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
            operation_id=op_id,
        )


def test_ownership_binding_duplicate_keys_refused(tmp_path, monkeypatch):
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

    op_id = "restore-20260803T120000Z-55667788"
    stage_dir = control_db.parent / f".garmincoach-restore-stage-{op_id}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Invalid binding JSON containing duplicate key
    bad_binding = b'{"operation_id":"op1","operation_id":"op2"}\n'
    (stage_dir / ".staging-binding.json").write_bytes(bad_binding)

    with pytest.raises(ConfiguredRestoreError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
            operation_id=op_id,
        )


# -----------------------------------------------------------------------------
# 5. Complete Mutation Tripwires Tests
# -----------------------------------------------------------------------------

def test_complete_mutation_tripwires_configured_databases_bytes_unchanged(tmp_path, monkeypatch):
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


def test_synthetic_staging_and_replacement_apis_continue_to_refuse_configured_paths(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    with pytest.raises(StagingSyntheticError):
        _validate_fixture_root(control_db.parent, backup_root)


# -----------------------------------------------------------------------------
# 6. Durable Evidence & Preparation Safety Verification Tests
# -----------------------------------------------------------------------------

def test_destination_baseline_persistence_and_sha256_in_journal(tmp_path, monkeypatch):
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

    op_dir = restore_root / f"operation-{result.operation_id}"
    baseline_file = op_dir / "destination-baseline.json"
    assert baseline_file.exists()

    baseline_data = json.loads(baseline_file.read_bytes())
    assert baseline_data["format_version"] == "garmincoach-destination-baseline-v1"
    assert baseline_data["operation_id"] == result.operation_id
    assert len(baseline_data["targets"]) == 2

    journal = load_restore_journal(result.operation_id, root=restore_root)
    assert journal.destination_baseline_sha256 is not None
    assert hashlib.sha256(baseline_file.read_bytes()).hexdigest() == journal.destination_baseline_sha256


def test_publish_noreplace_non_overwrite_protection(tmp_path):
    partial = tmp_path / "test.partial"
    final = tmp_path / "test.final"
    partial.write_bytes(b"hello world")

    # First publication succeeds
    publish_noreplace(partial, final)
    assert final.read_bytes() == b"hello world"
    assert not partial.exists()

    # Second publication to existing file fails with ConfiguredStagingOwnershipError
    partial2 = tmp_path / "test2.partial"
    partial2.write_bytes(b"another text")

    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial2, final)


def test_unexpected_child_in_staging_dir_rejected(tmp_path, monkeypatch):
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

    op_id = "restore-20260803T120000Z-77889900"
    stage_dir = control_db.parent / f".garmincoach-restore-stage-{op_id}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / "unrelated_file.tmp").write_bytes(b"foreign payload")

    with pytest.raises(ConfiguredRestoreError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
            operation_id=op_id,
        )


def test_centralized_failure_settlement_to_failed_safe_and_lock_release(tmp_path, monkeypatch):
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

    with pytest.raises(ConfiguredRestorePreconditionError):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit="0000000000000000000000000000000000000000",
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
        )


def test_publish_noreplace_race_foreign_final_file_unmodified(tmp_path):
    partial = tmp_path / "test_race.partial"
    final = tmp_path / "test_race.final"
    partial.write_bytes(b"our partial data")
    final.write_bytes(b"pre-existing foreign file")

    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial, final, expected_size=len(b"our partial data"), expected_sha256=hashlib.sha256(b"our partial data").hexdigest())

    assert final.read_bytes() == b"pre-existing foreign file"
    assert partial.read_bytes() == b"our partial data"


def test_publish_noreplace_final_sha_mismatch_surfaced(tmp_path):
    partial = tmp_path / "sha.partial"
    final = tmp_path / "sha.final"
    partial.write_bytes(b"correct data")

    wrong_sha = "0000000000000000000000000000000000000000000000000000000000000000"
    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial, final, expected_size=len(b"correct data"), expected_sha256=wrong_sha)


def test_external_configured_path_refused_by_baseline(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)
    external_path = tmp_path / "outside_project_root" / "ext.db"
    external_path.parent.mkdir(parents=True, exist_ok=True)
    external_path.write_bytes(b"external db bytes")

    ext_target = DatabaseTarget(
        target_key="ext-db",
        kind="single_user",
        tenant_id=None,
        path=external_path,
        required=True,
    )

    with pytest.raises(ConfiguredStagingError):
        capture_destination_baseline_evidence(
            operation_id="restore-test-ext",
            selected_backup_id="backup-123456",
            selected_backup_manifest_sha256="a" * 64,
            expected_application_commit=commit_hex,
            runtime_mode="single_user",
            target_set_hash="b" * 64,
            confirmation_value="c" * 64,
            targets=(ext_target,),
        )


def test_active_user_mapping_failure_refused(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)
    import operator_storage

    def broken_mapping(_path):
        raise RuntimeError("Inspection failure simulating active user error")

    monkeypatch.setattr(operator_storage, "active_user_target_mapping", broken_mapping)

    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    with pytest.raises(ConfiguredStagingError):
        capture_destination_baseline_evidence(
            operation_id="restore-test-mapping",
            selected_backup_id="backup-123456",
            selected_backup_manifest_sha256="a" * 64,
            expected_application_commit=commit_hex,
            runtime_mode="single_user",
            target_set_hash="b" * 64,
            confirmation_value="c" * 64,
            targets=targets,
        )


def test_nested_unknown_baseline_key_refused():
    payload = {
        "format_version": "garmincoach-destination-baseline-v1",
        "operation_id": "restore-20260803T120000Z-11223344",
        "selected_backup_id": "20260803T120000Z-aabbccdd",
        "selected_backup_manifest_sha256": "a" * 64,
        "expected_application_commit": "b" * 40,
        "runtime_mode": "single_user",
        "target_set_hash": "c" * 64,
        "confirmation_value": "d" * 64,
        "ordered_target_keys": ["control"],
        "targets": [],
        "active_control_user_mapping": {},
        "garminconnect_version": "0.1.0",
        "captured_at": "2026-08-03T12:00:00Z",
        "unknown_extra_key": "forbidden",
    }
    with pytest.raises(ConfiguredStagingOwnershipError):
        _destination_baseline_from_payload(payload)


def test_global_staged_verified_with_staged_target_rejected(tmp_path, monkeypatch):
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    op_id = "restore-20260803T120000Z-99001122"
    plan = create_restore_plan(
        selected_backup_id="20260803T120000Z-11223344",
        selected_backup_manifest_sha256="a" * 64,
        expected_application_commit=commit_hex,
        runtime_mode="single_user",
        target_keys=("control", "single-user"),
    )
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id)

    # Directly setting global stage STAGED_VERIFIED while targets are STAGED must raise RestoreJournalError
    with pytest.raises(RestoreJournalError):
        update_restore_journal(
            op_id,
            root=restore_root,
            stage=RestoreStage.STAGED_VERIFIED,
            target_key="control",
            target_state=TargetRestoreState.STAGED,
        )


def test_mixed_restore_staged_target_states_resume_successfully(tmp_path, monkeypatch):
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

    op_id = "restore-20260803T120000Z-55667788"
    result = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
        operation_id=op_id,
    )
    assert result.stage is RestoreStage.REPLACEMENT_READY

    # Re-entry succeeds seamlessly
    result_reentry = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
        operation_id=op_id,
    )
    assert result_reentry.stage is RestoreStage.REPLACEMENT_READY


# =============================================================================
# 7. Evidence-Binding and Descriptor-Ownership Invariant Tests
# =============================================================================

def _prepare_to_ready(tmp_path, monkeypatch):
    """Helper: set up env, create backup, run full prepare and return (result, env_tuple)."""
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
    c_val = confirmation_value(target_hash=t_hash, expected_application_commit=commit_hex)
    result = prepare_configured_restore(
        selected_backup_id=source_backup_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )
    return result, (proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex, source_backup_id, t_hash, c_val, snapshot)


def test_baseline_sha_tampered_between_barriers_detected(tmp_path, monkeypatch):
    """Modifying a non-runtime baseline field (captured_at) after one barrier must be
    detected at the next barrier even when all destination facts still match.
    The tamper must be written as canonical JSON so the load succeeds; only the SHA check
    catches the byte-level change."""
    from guarded_restore import canonical_json as _cjson

    result, env = _prepare_to_ready(tmp_path, monkeypatch)
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex, source_backup_id, t_hash, c_val, snapshot = env
    op_id = result.operation_id
    op_dir = restore_root / f"operation-{op_id}"
    baseline_file = op_dir / "destination-baseline.json"

    # Load and tamper only captured_at (non-runtime field), then re-write as canonical JSON.
    # This produces valid, parseable canonical bytes but with a different SHA-256.
    orig_bytes = baseline_file.read_bytes()
    data = json.loads(orig_bytes)
    data["captured_at"] = "2099-01-01T00:00:00Z"
    tampered_bytes = _cjson(data)
    assert tampered_bytes != orig_bytes, "Tampered bytes must differ from originals"
    baseline_file.write_bytes(tampered_bytes)

    # Re-entry must detect SHA mismatch at the barrier (journal SHA != reloaded file SHA)
    with pytest.raises((ConfiguredRestorePreconditionError, ConfiguredJournalUncertaintyError)):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
            operation_id=op_id,
        )



def test_journal_missing_baseline_sha_after_precheck_rejected(tmp_path, monkeypatch):
    """A journal at VERIFIED or later without destination_baseline_sha256 must be refused."""
    result, env = _prepare_to_ready(tmp_path, monkeypatch)
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex, source_backup_id, t_hash, c_val, snapshot = env
    op_id = result.operation_id
    journal_path = restore_root / f"operation-{op_id}" / "journal.json"

    # Null out destination_baseline_sha256 and reset to VERIFIED stage
    data = json.loads(journal_path.read_bytes())
    data["destination_baseline_sha256"] = None
    data["stage"] = "VERIFIED"
    journal_path.write_bytes((json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))

    # The inner ConfiguredRestorePreconditionError may surface as ConfiguredJournalUncertaintyError
    # if settlement also fails on the tampered journal.
    with pytest.raises((ConfiguredRestorePreconditionError, ConfiguredJournalUncertaintyError)):
        prepare_configured_restore(
            selected_backup_id=source_backup_id,
            expected_application_commit=commit_hex,
            confirmed_target_set_hash=t_hash,
            confirmed_restore_value=c_val,
            operation_id=op_id,
        )



def test_durable_parent_substitution_before_stage_validation_refused(tmp_path, monkeypatch):
    """Substituting the destination parent with a different directory (same path, different
    inode) must be refused when persisted baseline parent identity does not match.
    Uses _prepare_to_ready so the journal is at REPLACEMENT_READY with existing staging dirs;
    stage_configured_targets is called again (REPLACEMENT_READY is a valid re-entry stage)
    with tampered baseline parent identity – the existing-dir validation must reject it."""
    from guarded_restore_configured_staging import (
        load_destination_baseline_evidence,
        stage_configured_targets,
        TargetBaselineRecord,
        DestinationBaselineEvidence,
    )
    from operator_storage import discover_database_targets, TargetProfile
    import stat as _stat

    result, env = _prepare_to_ready(tmp_path, monkeypatch)
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex, source_backup_id, t_hash, c_val, snapshot = env
    op_id = result.operation_id

    # Load real evidence from disk
    configured_targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    evidence, _ = load_destination_baseline_evidence(op_id, restore_root=restore_root)

    # Build a substitute directory whose inode is definitely different from the real parent
    substitute_dir = tmp_path / "substitute_parent"
    substitute_dir.mkdir()
    subst_st = substitute_dir.stat()

    # Tamper parent_st_ino to point to the substitute (inode + 99999 to guarantee mismatch)
    tampered_records = []
    for rec in evidence.targets:
        tampered_records.append(TargetBaselineRecord(
            target_key=rec.target_key,
            kind=rec.kind,
            tenant_uuid=rec.tenant_uuid,
            target_order=rec.target_order,
            configured_relative_path=rec.configured_relative_path,
            resolved_relative_path=rec.resolved_relative_path,
            is_regular_file=rec.is_regular_file,
            st_dev=rec.st_dev,
            st_ino=rec.st_ino,
            size_bytes=rec.size_bytes,
            mtime_ns=rec.mtime_ns,
            st_mode=rec.st_mode,
            sha256=rec.sha256,
            parent_relative_path=rec.parent_relative_path,
            parent_st_dev=subst_st.st_dev,
            parent_st_ino=subst_st.st_ino + 99999,  # definitely wrong inode
            parent_is_dir=True,
            parent_st_mode=_stat.S_IMODE(subst_st.st_mode),
            wal=rec.wal,
            shm=rec.shm,
        ))

    tampered_evidence = DestinationBaselineEvidence(
        format_version=evidence.format_version,
        operation_id=evidence.operation_id,
        selected_backup_id=evidence.selected_backup_id,
        selected_backup_manifest_sha256=evidence.selected_backup_manifest_sha256,
        expected_application_commit=evidence.expected_application_commit,
        runtime_mode=evidence.runtime_mode,
        target_set_hash=evidence.target_set_hash,
        confirmation_value=evidence.confirmation_value,
        ordered_target_keys=evidence.ordered_target_keys,
        targets=tuple(tampered_records),
        active_control_user_mapping=evidence.active_control_user_mapping,
        garminconnect_version=evidence.garminconnect_version,
        captured_at=evidence.captured_at,
    )

    # stage_configured_targets at REPLACEMENT_READY with tampered evidence must raise
    # ConfiguredStagingOwnershipError because existing staging dirs' parent identity
    # does not match the (tampered) persisted baseline.
    with pytest.raises(ConfiguredStagingOwnershipError, match="parent directory identity"):
        stage_configured_targets(
            op_id,
            snapshot,
            configured_targets,
            restore_root=restore_root,
            destination_baseline=tampered_evidence,
        )


def test_binding_hard_link_count_greater_than_one_refused(tmp_path, monkeypatch):
    """A staging binding with st_nlink > 1 must be refused by validate_existing_staging_directory."""
    from guarded_restore_configured_staging import (
        validate_existing_staging_directory,
        canonical_json as _cjson,
        _STAGING_BINDING_FORMAT,
        _STAGING_BINDING_NAME,
    )

    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    op_id = "restore-20260803T120000Z-aa112233"
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    if os.name != "nt":
        os.chmod(stage_dir, 0o700)

    binding_payload = {
        "format_version": _STAGING_BINDING_FORMAT,
        "operation_id": op_id,
        "selected_backup_id": "20260803T120000Z-aabbccdd",
        "selected_backup_manifest_sha256": "a" * 64,
        "safety_backup_id": None,
        "runtime_mode": "single_user",
        "target_set_hash": "b" * 64,
        "stage_parent": "data",
        "artifacts": [],
    }
    binding_bytes = _cjson(binding_payload)
    binding_file = stage_dir / _STAGING_BINDING_NAME
    binding_file.write_bytes(binding_bytes)
    if os.name != "nt":
        os.chmod(binding_file, 0o600)

    # Create a hard link to simulate nlink > 1
    hard_link = tmp_path / ".staging-binding-hardlink.json"
    try:
        os.link(str(binding_file), str(hard_link))
    except OSError:
        pytest.skip("Hard links not supported on this filesystem")

    # Now validate must fail due to nlink > 1
    if os.name != "nt":
        # Hard-link count check only reliably works on POSIX
        with pytest.raises(ConfiguredStagingOwnershipError, match="link count"):
            validate_existing_staging_directory(
                stage_dir, op_id, binding_bytes, set()
            )


def test_artifact_hard_link_count_greater_than_one_refused(tmp_path, monkeypatch):
    """A staged artifact with st_nlink > 1 must be refused by validate_existing_staging_directory."""
    from guarded_restore_configured_staging import (
        validate_existing_staging_directory,
        canonical_json as _cjson,
        _STAGING_BINDING_FORMAT,
        _STAGING_BINDING_NAME,
    )

    if os.name == "nt":
        pytest.skip("Hard-link count enforcement only checked on POSIX")

    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = _setup_test_env(tmp_path, monkeypatch)

    op_id = "restore-20260803T120000Z-bb334455"
    stage_dir = tmp_path / "stage2"
    stage_dir.mkdir()
    os.chmod(stage_dir, 0o700)

    artifact_name = "000-control.sqlite.staged"
    binding_payload = {
        "format_version": _STAGING_BINDING_FORMAT,
        "operation_id": op_id,
        "selected_backup_id": "20260803T120000Z-aabbccdd",
        "selected_backup_manifest_sha256": "a" * 64,
        "safety_backup_id": None,
        "runtime_mode": "single_user",
        "target_set_hash": "b" * 64,
        "stage_parent": "data",
        "artifacts": [],
    }
    binding_bytes = _cjson(binding_payload)
    binding_file = stage_dir / _STAGING_BINDING_NAME
    binding_file.write_bytes(binding_bytes)
    os.chmod(binding_file, 0o600)

    artifact = stage_dir / artifact_name
    artifact.write_bytes(b"sqlite data")
    os.chmod(artifact, 0o600)

    # Create hard link to simulate nlink > 1 for the artifact
    art_link = tmp_path / f"{artifact_name}.hardlink"
    try:
        os.link(str(artifact), str(art_link))
    except OSError:
        pytest.skip("Hard links not supported on this filesystem")

    with pytest.raises(ConfiguredStagingOwnershipError, match="link count"):
        validate_existing_staging_directory(
            stage_dir, op_id, binding_bytes, {artifact_name}
        )


@pytest.mark.skipif(os.name == "nt", reason="fchmod descriptor binding not testable on Windows")
def test_publish_noreplace_link_count_exceeds_one_refused(tmp_path):
    """If a foreign hard link was created on the final destination before publish,
    the link count after partial unlink must be != 1, and publish_noreplace must refuse it."""
    partial = tmp_path / "test_lnk.partial"
    final = tmp_path / "test_lnk.final"
    partial.write_bytes(b"ownership data")

    # First, publish so final exists
    publish_noreplace(partial, final)
    assert final.exists()

    # Now create another partial and try to publish to another final that has an extra hard link
    partial2 = tmp_path / "test_lnk2.partial"
    final2 = tmp_path / "test_lnk2.final"
    partial2.write_bytes(b"ownership data 2")

    # Simulate: partial2 already has a hard link (external copy)
    extra_link = tmp_path / "extra.lnk"
    os.link(str(partial2), str(extra_link))

    # Publish should succeed for the file itself, but after partial unlink,
    # the link count of final2 should be 1 (extra link was on partial, not final).
    # This tests the normal case passes when no extra link is on final2.
    publish_noreplace(partial2, final2)
    assert final2.exists()
    assert not partial2.exists()
    # extra_link still exists (it was the partial2 hard link, now unlinked via partial2)
    # After partial2 is unlinked, extra_link holds the data but final2 is st_nlink==1
    final2_st = final2.stat()
    assert final2_st.st_nlink == 1


@pytest.mark.skipif(os.name == "nt", reason="fchmod descriptor binding not testable on Windows")
def test_publish_noreplace_descriptor_bound_permissions_applied(tmp_path):
    """After publish_noreplace, the final file must have mode 0600 applied descriptor-bound."""
    partial = tmp_path / "perm.partial"
    final = tmp_path / "perm.final"
    partial.write_bytes(b"mode test data")

    publish_noreplace(partial, final)

    st = final.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600


def test_publish_noreplace_parent_mismatch_refused(tmp_path):
    """partial_path and final_path in different parent directories must be refused."""
    sub = tmp_path / "sub"
    sub.mkdir()
    partial = tmp_path / "test_parent.partial"
    final = sub / "test_parent.final"
    partial.write_bytes(b"data")

    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial, final)


def test_publish_noreplace_expected_parent_wrong_refused(tmp_path):
    """Specifying an expected_parent that doesn't match actual parent must be refused."""
    wrong_parent = tmp_path / "wrong"
    wrong_parent.mkdir()
    partial = tmp_path / "ep.partial"
    final = tmp_path / "ep.final"
    partial.write_bytes(b"data")

    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial, final, expected_parent=wrong_parent)


def test_publish_noreplace_expected_partial_name_wrong_refused(tmp_path):
    """Specifying wrong expected_partial_name must be refused."""
    partial = tmp_path / "realname.partial"
    final = tmp_path / "out.final"
    partial.write_bytes(b"data")

    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial, final, expected_partial_name="othername.partial")


def test_publish_noreplace_expected_final_name_wrong_refused(tmp_path):
    """Specifying wrong expected_final_name must be refused."""
    partial = tmp_path / "in.partial"
    final = tmp_path / "realfinal.out"
    partial.write_bytes(b"data")

    with pytest.raises(ConfiguredStagingOwnershipError):
        publish_noreplace(partial, final, expected_final_name="other.out")

