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
    RestoreTransitionError,
    TargetRestoreState,
    create_restore_journal,
    create_restore_plan,
    update_restore_journal,
)
from inspect_restore_operation import (
    _derive_evidence_and_description,
    inspect_operation,
    main,
)


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


def test_replacing_stage_variants(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    # 1. No target intent yet
    plan = _make_plan()
    op_id_1 = "restore-20260801T120000Z-00000001"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_1, now=TIMESTAMP)
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_1, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_1, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")

    out, code = inspect_operation(op_id_1)
    d = json.loads(out)
    assert code == EXIT_OPERATION_IN_PROGRESS
    assert d["assessment"] == "operation_in_progress"
    assert d["terminal"] is False
    assert d["replacement_intent_recorded"] is False
    assert d["destination_replacement_completed"] is False
    assert d["all_completed_replacements_rolled_back"] is False

    # 2. First target replacement intent recorded but no replacement completed
    op_id_2 = "restore-20260801T120000Z-00000002"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_2, now=TIMESTAMP)
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_2, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_2, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_2, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:09Z")

    out, code = inspect_operation(op_id_2)
    d = json.loads(out)
    assert code == EXIT_OPERATION_IN_PROGRESS
    assert d["replacement_intent_recorded"] is True
    assert d["destination_replacement_completed"] is False
    assert d["all_completed_replacements_rolled_back"] is False

    # 3. One target durably replaced
    op_id_3 = "restore-20260801T120000Z-00000003"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_3, now=TIMESTAMP)
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_3, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_3, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_3, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")

    out, code = inspect_operation(op_id_3)
    d = json.loads(out)
    assert code == EXIT_OPERATION_IN_PROGRESS
    assert d["replacement_intent_recorded"] is True
    assert d["destination_replacement_completed"] is True
    assert d["all_completed_replacements_rolled_back"] is False

    # 4. All targets durably replaced but global stage remains REPLACING
    op_id_4 = "restore-20260801T120000Z-00000004"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_4, now=TIMESTAMP)
    update_restore_journal(op_id_4, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_4, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_4, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_4, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_4, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_4, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_4, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_4, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_4, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id_4, root=restore_root, target_key="single-user", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:10Z")

    out, code = inspect_operation(op_id_4)
    d = json.loads(out)
    assert code == EXIT_OPERATION_IN_PROGRESS
    assert d["replacement_intent_recorded"] is True
    assert d["destination_replacement_completed"] is True
    assert d["all_completed_replacements_rolled_back"] is False


def test_rollback_required_stage_variants(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    # 1. Rollback required before any destination replacement completed
    plan = _make_plan()
    op_id_1 = "restore-20260801T120000Z-00000010"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_1, now=TIMESTAMP)
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_1, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_1, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:09Z")

    out, code = inspect_operation(op_id_1)
    d = json.loads(out)
    assert code == EXIT_ROLLBACK_REQUIRED
    assert d["assessment"] == "rollback_required"
    assert d["destination_replacement_completed"] is False
    assert d["rollback_intent_recorded"] is False

    # 2. One replacement completed
    op_id_2 = "restore-20260801T120000Z-00000011"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_2, now=TIMESTAMP)
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_2, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_2, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_2, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")

    out, code = inspect_operation(op_id_2)
    d = json.loads(out)
    assert code == EXIT_ROLLBACK_REQUIRED
    assert d["destination_replacement_completed"] is True
    assert d["all_completed_replacements_rolled_back"] is False

    # 3. Rollback intent recorded but none completed
    update_restore_journal(op_id_2, root=restore_root, target_key="control", rollback_intent=True, target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:11Z")
    out, code = inspect_operation(op_id_2)
    d = json.loads(out)
    assert code == EXIT_ROLLBACK_REQUIRED
    assert d["rollback_intent_recorded"] is True
    assert d["destination_rollback_completed"] is False


def test_rolled_back_stage_properties(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    plan = _make_plan()
    op_id = "restore-20260801T120000Z-00000020"
    create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")
    update_restore_journal(op_id, root=restore_root, target_key="control", rollback_intent=True, target_state=TargetRestoreState.ROLLED_BACK, rollback_completed=True, now="2026-08-01T12:00:11Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLED_BACK, now="2026-08-01T12:00:12Z")

    out, code = inspect_operation(op_id)
    d = json.loads(out)

    assert code == EXIT_OPERATION_IN_PROGRESS
    assert d["assessment"] == "rollback_completed_pending_finalization"
    assert d["terminal"] is False
    assert d["automatic_reentry_required"] is True
    assert d["manual_recovery_required"] is False
    assert d["postcheck_required"] is False
    assert d["final_result"] is None
    assert d["destination_replacement_completed"] is True
    assert d["destination_rollback_completed"] is True
    assert d["all_completed_replacements_rolled_back"] is True

    out_h, _ = inspect_operation(op_id, human=True)
    assert "Rollback is durably complete, but FAILED_SAFE has not yet been persisted and reread" in out_h


def test_state_machine_rejects_contradictory_rolled_back_stage(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    plan = _make_plan()
    op_id = "restore-20260801T120000Z-00000021"
    create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")

    # Attempting to set global stage ROLLED_BACK while target 'control' is still REPLACED must raise RestoreJournalError
    with pytest.raises(RestoreJournalError):
        update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLED_BACK, now="2026-08-01T12:00:11Z")


def test_failed_safe_descriptions(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    # 1. Failure before replacement began
    plan = _make_plan()
    op_id_1 = "restore-20260801T120000Z-00000030"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_1, now=TIMESTAMP)
    update_restore_journal(op_id_1, root=restore_root, stage=RestoreStage.FAILED_SAFE, now="2026-08-01T12:00:01Z")

    out_1, code_1 = inspect_operation(op_id_1)
    d_1 = json.loads(out_1)
    assert code_1 == EXIT_FAILED_SAFE
    assert d_1["assessment"] == "failed_safely"
    assert d_1["terminal"] is True
    assert d_1["final_result"] == FinalResult.FAILED_SAFE.value
    assert d_1["replacement_intent_recorded"] is False
    assert d_1["destination_replacement_completed"] is False

    out_h1, _ = inspect_operation(op_id_1, human=True)
    assert "Operation failed safely before replacement began." in out_h1

    # 2. Failure after replacement intent recorded but no replacement completed
    op_id_2 = "restore-20260801T120000Z-00000031"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_2, now=TIMESTAMP)
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_2, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_2, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_2, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.ROLLED_BACK, now="2026-08-01T12:00:11Z")
    update_restore_journal(op_id_2, root=restore_root, stage=RestoreStage.FAILED_SAFE, now="2026-08-01T12:00:12Z")

    out_2, code_2 = inspect_operation(op_id_2)
    d_2 = json.loads(out_2)
    assert code_2 == EXIT_FAILED_SAFE
    assert d_2["replacement_intent_recorded"] is True
    assert d_2["destination_replacement_completed"] is False

    out_h2, _ = inspect_operation(op_id_2, human=True)
    assert "Operation failed safely with no durably completed destination replacement." in out_h2

    # 3. Failure after a complete verified rollback of replaced targets
    op_id_3 = "restore-20260801T120000Z-00000032"
    create_restore_journal(plan, root=restore_root, operation_id=op_id_3, now=TIMESTAMP)
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_3, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id_3, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id_3, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")
    update_restore_journal(op_id_3, root=restore_root, target_key="control", rollback_intent=True, target_state=TargetRestoreState.ROLLED_BACK, rollback_completed=True, now="2026-08-01T12:00:11Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.ROLLED_BACK, now="2026-08-01T12:00:12Z")
    update_restore_journal(op_id_3, root=restore_root, stage=RestoreStage.FAILED_SAFE, now="2026-08-01T12:00:13Z")

    out_3, code_3 = inspect_operation(op_id_3)
    d_3 = json.loads(out_3)
    assert code_3 == EXIT_FAILED_SAFE
    assert d_3["destination_replacement_completed"] is True
    assert d_3["all_completed_replacements_rolled_back"] is True

    out_h3, _ = inspect_operation(op_id_3, human=True)
    assert "Operation failed safely after verified rollback of all replaced targets." in out_h3


def test_failed_manual_recovery_required_variants(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    plan = _make_plan()
    op_id = "restore-20260801T120000Z-00000040"
    create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY_ID, now="2026-08-01T12:00:02Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for k in ("control", "single-user"):
        update_restore_journal(op_id, root=restore_root, target_key=k, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    update_restore_journal(op_id, root=restore_root, target_key="control", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:09Z")
    update_restore_journal(op_id, root=restore_root, target_key="single-user", replacement_intent=True, target_state=TargetRestoreState.REPLACED, replacement_completed=True, now="2026-08-01T12:00:10Z")
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:11Z")
    update_restore_journal(op_id, root=restore_root, target_key="control", rollback_intent=True, target_state=TargetRestoreState.ROLLED_BACK, rollback_completed=True, now="2026-08-01T12:00:12Z")
    # control is ROLLED_BACK, single-user is REPLACED (mixed states!)
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, now="2026-08-01T12:00:13Z")

    out, code = inspect_operation(op_id)
    d = json.loads(out)
    assert code == EXIT_MANUAL_RECOVERY_REQUIRED
    assert d["assessment"] == "manual_recovery_required"
    assert d["terminal"] is True
    assert d["manual_recovery_required"] is True
    assert d["destination_replacement_completed"] is True
    assert d["all_completed_replacements_rolled_back"] is False


def test_inspection_strict_read_only_guarantees(tmp_path, monkeypatch):
    project_root, restore_root = _setup_journal_env(tmp_path, monkeypatch)

    plan = _make_plan()
    op_id = "restore-20260801T120000Z-00000050"
    create_restore_journal(plan, root=restore_root, operation_id=op_id, now=TIMESTAMP)
    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")

    op_dir = restore_root / f"operation-{op_id}"
    jfile = op_dir / "journal.json"

    entries_before = sorted(os.listdir(op_dir))
    mtime_before = jfile.stat().st_mtime_ns
    bytes_before = jfile.read_bytes()
    mode_before = jfile.stat().st_mode

    out, code = inspect_operation(op_id, human=True, show_local_paths=True)
    assert code == EXIT_PREPARATION_INCOMPLETE

    assert sorted(os.listdir(op_dir)) == entries_before
    assert jfile.stat().st_mtime_ns == mtime_before
    assert jfile.read_bytes() == bytes_before
    assert jfile.stat().st_mode == mode_before


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
