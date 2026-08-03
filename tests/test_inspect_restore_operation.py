from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

import config
from guarded_restore import (
    EXIT_FAILED_SAFE,
    EXIT_INVALID_ARGUMENTS,
    EXIT_INVALID_OPERATION,
    EXIT_MANUAL_RECOVERY_REQUIRED,
    EXIT_ROLLBACK_REQUIRED,
    EXIT_SUCCESS,
    FinalResult,
    RestoreStage,
    TargetRestoreState,
    canonical_json,
    create_restore_journal,
    create_restore_plan,
    update_restore_journal,
)
from inspect_restore_operation import inspect_operation, main


BACKUP_ID = "20260801T120000Z-a1b2c3d4"
SAFETY_ID = "20260801T120100Z-b1c2d3e4"
MANIFEST_SHA = "a" * 64
COMMIT_SHA = "b" * 40
TIMESTAMP = "2026-08-01T12:00:00Z"


def _setup_journal_env(tmp_path: Path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    restore_root = project_root / "restore_journals"
    restore_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config, "OPERATOR_RESTORE_ROOT", restore_root)
    return project_root, restore_root


def _make_plan(mode="single_user", targets=("control", "single-user")):
    return create_restore_plan(
        selected_backup_id=BACKUP_ID,
        selected_backup_manifest_sha256=MANIFEST_SHA,
        expected_application_commit=COMMIT_SHA,
        runtime_mode=mode,
        target_keys=targets,
        created_at=TIMESTAMP,
    )


def test_inspect_stage_precheck(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-11112222"
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)

    output, code = inspect_operation(op_id)
    assert code == EXIT_SUCCESS
    data = json.loads(output)
    assert data["operation_id"] == op_id
    assert data["assessment"] == "safe_to_proceed_to_apply"
    assert data["stage"] == "PRECHECK"
    assert data["final_result"] is None
    assert len(data["targets"]) == 2


def test_inspect_stage_completed(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-22223333"
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACED, now="2026-08-01T12:00:10Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.POSTCHECK_PASSED, now="2026-08-01T12:00:11Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.COMPLETED, now="2026-08-01T12:00:12Z")

    output, code = inspect_operation(op_id)
    assert code == EXIT_SUCCESS
    data = json.loads(output)
    assert data["assessment"] == "completed"
    assert data["stage"] == "COMPLETED"
    assert data["final_result"] == "COMPLETED"


def test_inspect_stage_failed_safe(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-33334444"
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.FAILED_SAFE, now="2026-08-01T12:00:01Z")

    output, code = inspect_operation(op_id)
    assert code == EXIT_FAILED_SAFE
    data = json.loads(output)
    assert data["assessment"] == "failed_safely"
    assert data["stage"] == "FAILED_SAFE"
    assert data["final_result"] == "FAILED_SAFE"


def test_inspect_stage_rollback_required(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-44445555"
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    journal = update_restore_journal(op_id, root=restore_root, target_key="control", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")

    output, code = inspect_operation(op_id)
    assert code == EXIT_ROLLBACK_REQUIRED
    data = json.loads(output)
    assert data["assessment"] == "rollback_required"
    assert data["stage"] == "ROLLBACK_REQUIRED"


def test_inspect_stage_manual_recovery_required_mixed_states(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-55556666"
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    journal = update_restore_journal(op_id, root=restore_root, target_key="control", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(op_id, root=restore_root, target_key="single-user", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:10Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:11Z")
    journal = update_restore_journal(op_id, root=restore_root, target_key="control", target_state=TargetRestoreState.ROLLED_BACK, now="2026-08-01T12:00:12Z")
    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, now="2026-08-01T12:00:13Z")

    output, code = inspect_operation(op_id)
    assert code == EXIT_MANUAL_RECOVERY_REQUIRED
    data = json.loads(output)
    assert data["assessment"] == "manual_recovery_required"
    assert data["stage"] == "FAILED_MANUAL_RECOVERY_REQUIRED"
    assert data["final_result"] == "FAILED_MANUAL_RECOVERY_REQUIRED"
    states = {t["target_key"]: t["state"] for t in data["targets"]}
    assert states["control"] == "ROLLED_BACK"
    assert states["single-user"] == "REPLACED"

    output_human, code_h = inspect_operation(op_id, human=True)
    assert code_h == EXIT_MANUAL_RECOVERY_REQUIRED
    assert "CRITICAL" in output_human
    assert "Manual recovery is required" in output_human


def test_inspect_human_output_and_show_local_paths(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-66667777"
    create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)

    output_human, code = inspect_operation(op_id, human=True, show_local_paths=False)
    assert code == EXIT_SUCCESS
    assert "GarminCoach Restore Operation Inspection" in output_human
    assert str(restore_root) not in output_human

    output_paths, code = inspect_operation(op_id, human=True, show_local_paths=True)
    assert code == EXIT_SUCCESS
    assert "Local Paths (Diagnostic):" in output_paths
    assert str(restore_root) in output_paths


def test_inspect_refuses_malformed_op_id(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    for invalid in ["../bad", "restore-bad", "123"]:
        code = main(["--operation-id", invalid])
        assert code == EXIT_INVALID_OPERATION


def test_inspect_refuses_missing_or_oversized_journal(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    op_id = "restore-20260801T120000Z-88889999"

    code = main(["--operation-id", op_id])
    assert code == EXIT_INVALID_OPERATION

    plan = _make_plan()
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    jfile = restore_root / f"operation-{op_id}" / "journal.json"
    
    # Oversized journal
    jfile.write_bytes(b"a" * (129 * 1024))
    code = main(["--operation-id", op_id])
    assert code == EXIT_INVALID_OPERATION


def test_inspect_refuses_duplicate_json_keys(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-99990000"
    create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)

    jfile = restore_root / f"operation-{op_id}" / "journal.json"
    raw = jfile.read_bytes().decode("utf-8")
    # Duplicate key
    assert '"runtime_mode":"single_user"' in raw
    bad_json = raw.replace('"runtime_mode":"single_user"', '"runtime_mode":"single_user","runtime_mode":"single_user"')
    jfile.write_bytes(bad_json.encode("utf-8"))

    code = main(["--operation-id", op_id])
    assert code == EXIT_INVALID_OPERATION


def test_inspect_refuses_invalid_args(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    code = main([])
    assert code == EXIT_INVALID_ARGUMENTS


def test_no_mutation_or_cleanup_during_inspection(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    plan = _make_plan()
    op_id = "restore-20260801T120000Z-77778888"
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    jfile = restore_root / f"operation-{op_id}" / "journal.json"
    mtime_before = jfile.stat().st_mtime_ns

    output, code = inspect_operation(op_id)
    assert code == EXIT_SUCCESS
    assert jfile.stat().st_mtime_ns == mtime_before
