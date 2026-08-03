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
    EXIT_OPERATION_IN_PROGRESS,
    EXIT_PREPARATION_INCOMPLETE,
    EXIT_ROLLBACK_REQUIRED,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED_FAILURE,
    FinalResult,
    RestoreJournalError,
    RestoreStage,
    TargetRestoreState,
    create_restore_journal,
    create_restore_plan,
    update_restore_journal,
)
from inspect_restore_operation import _classify_stage, inspect_operation, main


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


def _create_journal_at_stage(restore_root: Path, stage: RestoreStage, op_id: str):
    plan = _make_plan()
    journal = create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    if stage is RestoreStage.PRECHECK:
        return journal

    if stage is RestoreStage.FAILED_SAFE:
        return update_restore_journal(op_id, root=restore_root, stage=RestoreStage.FAILED_SAFE, now="2026-08-01T12:00:01Z")

    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    if stage is RestoreStage.VERIFIED:
        return journal

    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    if stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
        return journal

    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    if stage is RestoreStage.RESTORE_STAGED:
        return journal

    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")

    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    if stage is RestoreStage.STAGED_VERIFIED:
        return journal

    for key in journal.target_keys:
        journal = update_restore_journal(op_id, root=restore_root, target_key=key, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")

    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    if stage is RestoreStage.REPLACEMENT_READY:
        return journal

    journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    if stage is RestoreStage.REPLACING:
        return journal

    journal = update_restore_journal(op_id, root=restore_root, target_key="control", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(op_id, root=restore_root, target_key="single-user", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:10Z")

    if stage is RestoreStage.REPLACED:
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACED, now="2026-08-01T12:00:11Z")
        return journal

    if stage is RestoreStage.POSTCHECK_PASSED:
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACED, now="2026-08-01T12:00:11Z")
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.POSTCHECK_PASSED, now="2026-08-01T12:00:12Z")
        return journal

    if stage is RestoreStage.COMPLETED:
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACED, now="2026-08-01T12:00:11Z")
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.POSTCHECK_PASSED, now="2026-08-01T12:00:12Z")
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.COMPLETED, now="2026-08-01T12:00:13Z")
        return journal

    if stage is RestoreStage.ROLLBACK_REQUIRED:
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:11Z")
        return journal

    if stage is RestoreStage.ROLLED_BACK:
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:11Z")
        journal = update_restore_journal(op_id, root=restore_root, target_key="single-user", target_state=TargetRestoreState.ROLLED_BACK, now="2026-08-01T12:00:12Z")
        journal = update_restore_journal(op_id, root=restore_root, target_key="control", target_state=TargetRestoreState.ROLLED_BACK, now="2026-08-01T12:00:13Z")
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLED_BACK, now="2026-08-01T12:00:14Z")
        return journal

    if stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:11Z")
        journal = update_restore_journal(op_id, root=restore_root, target_key="control", target_state=TargetRestoreState.ROLLED_BACK, now="2026-08-01T12:00:12Z")
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, now="2026-08-01T12:00:13Z")
        return journal

    raise ValueError(f"Unhandled stage setup: {stage}")


EXPECTED_STAGE_PROPERTIES = {
    RestoreStage.PRECHECK: {
        "assessment": "preparation_incomplete",
        "exit_code": EXIT_PREPARATION_INCOMPLETE,
        "terminal": False,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.VERIFIED: {
        "assessment": "preparation_incomplete",
        "exit_code": EXIT_PREPARATION_INCOMPLETE,
        "terminal": False,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.CURRENT_SNAPSHOT_CREATED: {
        "assessment": "preparation_incomplete",
        "exit_code": EXIT_PREPARATION_INCOMPLETE,
        "terminal": False,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.RESTORE_STAGED: {
        "assessment": "preparation_incomplete",
        "exit_code": EXIT_PREPARATION_INCOMPLETE,
        "terminal": False,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.STAGED_VERIFIED: {
        "assessment": "preparation_incomplete",
        "exit_code": EXIT_PREPARATION_INCOMPLETE,
        "terminal": False,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.REPLACEMENT_READY: {
        "assessment": "ready_for_replacement",
        "exit_code": EXIT_SUCCESS,
        "terminal": False,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.REPLACING: {
        "assessment": "operation_in_progress",
        "exit_code": EXIT_OPERATION_IN_PROGRESS,
        "terminal": False,
        "replacement_started": True,
        "postcheck_required": True,
        "automatic_reentry_required": True,
        "manual_recovery_required": False,
    },
    RestoreStage.REPLACED: {
        "assessment": "operation_in_progress",
        "exit_code": EXIT_OPERATION_IN_PROGRESS,
        "terminal": False,
        "replacement_started": True,
        "postcheck_required": True,
        "automatic_reentry_required": True,
        "manual_recovery_required": False,
    },
    RestoreStage.POSTCHECK_PASSED: {
        "assessment": "operation_in_progress",
        "exit_code": EXIT_OPERATION_IN_PROGRESS,
        "terminal": False,
        "replacement_started": True,
        "postcheck_required": False,
        "automatic_reentry_required": True,
        "manual_recovery_required": False,
    },
    RestoreStage.COMPLETED: {
        "assessment": "completed",
        "exit_code": EXIT_SUCCESS,
        "terminal": True,
        "replacement_started": True,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.ROLLBACK_REQUIRED: {
        "assessment": "rollback_required",
        "exit_code": EXIT_ROLLBACK_REQUIRED,
        "terminal": False,
        "replacement_started": True,
        "postcheck_required": False,
        "automatic_reentry_required": True,
        "manual_recovery_required": False,
    },
    RestoreStage.ROLLED_BACK: {
        "assessment": "failed_safely",
        "exit_code": EXIT_FAILED_SAFE,
        "terminal": True,
        "replacement_started": True,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.FAILED_SAFE: {
        "assessment": "failed_safely",
        "exit_code": EXIT_FAILED_SAFE,
        "terminal": True,
        "replacement_started": False,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": False,
    },
    RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED: {
        "assessment": "manual_recovery_required",
        "exit_code": EXIT_MANUAL_RECOVERY_REQUIRED,
        "terminal": True,
        "replacement_started": True,
        "postcheck_required": False,
        "automatic_reentry_required": False,
        "manual_recovery_required": True,
    },
}


@pytest.mark.parametrize("stage", list(RestoreStage))
def test_exhaustive_stage_inspection_properties(tmp_path, monkeypatch, stage):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    idx = list(RestoreStage).index(stage)
    op_id = f"restore-20260801T120000Z-000000{idx:02x}"

    journal = _create_journal_at_stage(restore_root, stage, op_id)
    jfile = restore_root / f"operation-{op_id}" / "journal.json"
    mtime_before = jfile.stat().st_mtime_ns
    bytes_before = jfile.read_bytes()

    output_json, code = inspect_operation(op_id)
    exp = EXPECTED_STAGE_PROPERTIES[stage]

    assert code == exp["exit_code"]
    data = json.loads(output_json)

    assert data["operation_id"] == op_id
    assert data["stage"] == stage.value
    assert data["assessment"] == exp["assessment"]
    assert data["terminal"] == exp["terminal"]
    assert data["replacement_started"] == exp["replacement_started"]
    assert data["postcheck_required"] == exp["postcheck_required"]
    assert data["automatic_reentry_required"] == exp["automatic_reentry_required"]
    assert data["manual_recovery_required"] == exp["manual_recovery_required"]

    # Test human-readable output
    output_human, code_human = inspect_operation(op_id, human=True)
    assert code_human == exp["exit_code"]
    assert exp["assessment"] in output_human

    # Verify read-only guarantees (no mutation or cleanup)
    assert jfile.stat().st_mtime_ns == mtime_before
    assert jfile.read_bytes() == bytes_before


def test_specific_stage_classification_proofs(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    # 1. REPLACING is never reported as failed_safely
    _create_journal_at_stage(restore_root, RestoreStage.REPLACING, "restore-20260801T120000Z-00000001")
    output, code = inspect_operation("restore-20260801T120000Z-00000001")
    data = json.loads(output)
    assert data["assessment"] != "failed_safely"
    assert code != EXIT_FAILED_SAFE
    assert data["assessment"] == "operation_in_progress"
    assert code == EXIT_OPERATION_IN_PROGRESS

    # 2. REPLACED is never reported as failed_safely or completed
    _create_journal_at_stage(restore_root, RestoreStage.REPLACED, "restore-20260801T120000Z-00000002")
    output, code = inspect_operation("restore-20260801T120000Z-00000002")
    data = json.loads(output)
    assert data["assessment"] not in {"failed_safely", "completed"}
    assert code not in {EXIT_FAILED_SAFE, EXIT_SUCCESS}
    assert data["assessment"] == "operation_in_progress"
    assert code == EXIT_OPERATION_IN_PROGRESS

    # 3. POSTCHECK_PASSED is never reported as completed
    _create_journal_at_stage(restore_root, RestoreStage.POSTCHECK_PASSED, "restore-20260801T120000Z-00000003")
    output, code = inspect_operation("restore-20260801T120000Z-00000003")
    data = json.loads(output)
    assert data["assessment"] != "completed"
    assert code != EXIT_SUCCESS
    assert data["assessment"] == "operation_in_progress"
    assert code == EXIT_OPERATION_IN_PROGRESS

    # 4. PRECHECK through STAGED_VERIFIED are not reported as ready_for_replacement
    for idx, stage in enumerate([RestoreStage.PRECHECK, RestoreStage.VERIFIED, RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.RESTORE_STAGED, RestoreStage.STAGED_VERIFIED]):
        op_id = f"restore-20260801T120000Z-000000{idx+4:02x}"
        _create_journal_at_stage(restore_root, stage, op_id)
        output, code = inspect_operation(op_id)
        data = json.loads(output)
        assert data["assessment"] != "ready_for_replacement"
        assert data["assessment"] == "preparation_incomplete"
        assert code == EXIT_PREPARATION_INCOMPLETE

    # 5. Only REPLACEMENT_READY receives ready_for_replacement
    _create_journal_at_stage(restore_root, RestoreStage.REPLACEMENT_READY, "restore-20260801T120000Z-00000009")
    output, code = inspect_operation("restore-20260801T120000Z-00000009")
    data = json.loads(output)
    assert data["assessment"] == "ready_for_replacement"
    assert code == EXIT_SUCCESS

    # 6. COMPLETED, FAILED_SAFE, and FAILED_MANUAL_RECOVERY_REQUIRED are terminal
    terminal_stages = {RestoreStage.COMPLETED, RestoreStage.FAILED_SAFE, RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, RestoreStage.ROLLED_BACK}
    for idx, stage in enumerate(RestoreStage):
        op_id = f"restore-20260801T120000Z-000000{idx+10:02x}"
        _create_journal_at_stage(restore_root, stage, op_id)
        output, _ = inspect_operation(op_id)
        data = json.loads(output)
        assert data["terminal"] == (stage in terminal_stages)


def test_simulated_unknown_stage_raises_unexpected_failure():
    # Pass invalid stage object to _classify_stage directly
    with pytest.raises(RestoreJournalError, match="Unrecognized or unsupported global restore stage"):
        _classify_stage("NON_EXISTENT_STAGE")


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
    assert '"runtime_mode":"single_user"' in raw
    bad_json = raw.replace('"runtime_mode":"single_user"', '"runtime_mode":"single_user","runtime_mode":"single_user"')
    jfile.write_bytes(bad_json.encode("utf-8"))

    code = main(["--operation-id", op_id])
    assert code == EXIT_INVALID_OPERATION


def test_inspect_refuses_invalid_args(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)
    code = main([])
    assert code == EXIT_INVALID_ARGUMENTS
