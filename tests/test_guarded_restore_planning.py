from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import replace

import pytest

import config
from guarded_restore import (
    FinalResult,
    RestoreJournalError,
    RestoreJournalPersistenceError,
    RestoreLock,
    RestoreLockError,
    RestorePlanError,
    RestoreStage,
    RestoreTransitionError,
    TargetRestoreState,
    confirmation_value,
    create_restore_journal,
    create_restore_plan,
    load_restore_journal,
    target_set_hash,
    update_restore_journal,
    validate_restore_root,
)


BACKUP = "20260801T120000Z-a1b2c3d4"
SAFETY = "20260801T120100Z-b1c2d3e4"
MANIFEST = "a" * 64
COMMIT = "b" * 40
TIME = "2026-08-01T12:00:00Z"


def _root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", tmp_path / "data" / "control.db")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "single.db")
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tmp_path / "data" / "users")
    monkeypatch.setattr(config, "OPERATOR_BACKUP_ROOT", tmp_path / "operator_backups")
    monkeypatch.setattr(config, "OPERATOR_RESTORE_ROOT", tmp_path / "restore_journals")
    return tmp_path / "restore_journals"


def _plan(mode="single_user", targets=("control", "single-user")):
    return create_restore_plan(
        selected_backup_id=BACKUP,
        selected_backup_manifest_sha256=MANIFEST,
        expected_application_commit=COMMIT,
        runtime_mode=mode,
        target_keys=targets,
        created_at=TIME,
    )


def test_plan_is_pure_deterministic_and_order_sensitive(monkeypatch):
    plan = _plan()
    assert plan.target_set_hash == target_set_hash(backup_id=BACKUP, manifest_sha256=MANIFEST, runtime_mode="single_user", target_keys=("control", "single-user"))
    assert plan.confirmation_value == confirmation_value(target_hash=plan.target_set_hash, expected_application_commit=COMMIT)
    multi = _plan("multi_user", ("control", "tenant:00000000-0000-0000-0000-000000000001"))
    assert multi.target_set_hash != plan.target_set_hash
    assert confirmation_value(target_hash=plan.target_set_hash, expected_application_commit="c" * 40) != plan.confirmation_value
    monkeypatch.setattr(Path, "open", lambda *_a, **_k: pytest.fail("planner touched filesystem"))
    assert _plan().selected_backup_id == BACKUP


@pytest.mark.parametrize("kwargs", [
    {"selected_backup_id": "../bad"},
    {"selected_backup_manifest_sha256": "A" * 64},
    {"expected_application_commit": "bad\ncommit"},
    {"runtime_mode": "other"},
    {"target_keys": ("control", "control")},
    {"target_keys": ("control", "../single-user")},
])
def test_plan_rejects_malformed_bounded_inputs(kwargs):
    values = dict(selected_backup_id=BACKUP, selected_backup_manifest_sha256=MANIFEST, expected_application_commit=COMMIT, runtime_mode="single_user", target_keys=("control", "single-user"), created_at=TIME)
    values.update(kwargs)
    with pytest.raises(RestorePlanError): create_restore_plan(**values)


def _advance_to_ready(root: Path):
    journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.VERIFIED, now="2026-08-01T12:00:01Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY, now="2026-08-01T12:00:02Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:03Z")
    for key in journal.target_keys:
        journal = update_restore_journal(journal.operation_id, root=root, target_key=key, target_state=TargetRestoreState.STAGED, now="2026-08-01T12:00:04Z")
    for key in journal.target_keys:
        journal = update_restore_journal(journal.operation_id, root=root, target_key=key, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
    return update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.REPLACEMENT_READY, now="2026-08-01T12:00:07Z")


def test_journal_round_trip_transitions_and_rollback(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    journal = _advance_to_ready(root)
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    journal = update_restore_journal(journal.operation_id, root=root, target_key="control", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")
    journal = update_restore_journal(journal.operation_id, root=root, target_key="control", target_state=TargetRestoreState.ROLLED_BACK, now="2026-08-01T12:00:11Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.ROLLED_BACK, now="2026-08-01T12:00:12Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.FAILED_SAFE, now="2026-08-01T12:00:13Z")
    assert journal.final_result is FinalResult.FAILED_SAFE
    assert load_restore_journal(journal.operation_id, root=root) == journal
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.VERIFIED)


def test_illegal_target_and_global_transitions_fail_closed(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.REPLACEMENT_READY)
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, target_key="control", target_state=TargetRestoreState.REPLACED)
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED)


def test_journal_rejects_malformed_bytes_and_preserves_previous(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch); journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    path = root / f"operation-{journal.operation_id}" / "journal.json"; before = path.read_bytes()
    path.write_bytes(b"\xff")
    with pytest.raises(RestoreJournalError): load_restore_journal(journal.operation_id, root=root)
    path.write_bytes(before)
    payload = json.loads(before); payload["target_keys"] = ["single-user", "control"]
    import guarded_restore
    path.write_bytes(guarded_restore.canonical_json(payload))
    with pytest.raises(RestoreJournalError): load_restore_journal(journal.operation_id, root=root)
    path.write_bytes(before)
    assert load_restore_journal(journal.operation_id, root=root) == journal


def test_loaded_journal_identity_is_bound_to_requested_operation(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    a = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    b = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120001Z-a1b2c3d4", now=TIME)
    a_path = root / f"operation-{a.operation_id}" / "journal.json"; b_path = root / f"operation-{b.operation_id}" / "journal.json"
    before_b = b_path.read_bytes(); payload = json.loads(a_path.read_text(encoding="utf-8")); payload["operation_id"] = b.operation_id
    import guarded_restore
    a_path.write_bytes(guarded_restore.canonical_json(payload))
    with pytest.raises(RestoreJournalError, match="identity"):
        load_restore_journal(a.operation_id, root=root)
    with pytest.raises(RestoreJournalError):
        update_restore_journal(a.operation_id, root=root, stage=RestoreStage.VERIFIED)
    assert b_path.read_bytes() == before_b


def test_update_timestamps_are_monotonic_and_failed_update_preserves_bytes(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch); journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    path = root / f"operation-{journal.operation_id}" / "journal.json"; before = path.read_bytes()
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.VERIFIED, now="2026-08-01T11:59:59Z")
    assert path.read_bytes() == before
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.VERIFIED, now=TIME)
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=SAFETY, now="2026-08-01T12:00:03Z")
    after = path.read_bytes()
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.RESTORE_STAGED, now="2026-08-01T12:00:02Z")
    assert path.read_bytes() == after


def test_manual_recovery_preserves_partial_rollback_facts(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch); journal = _advance_to_ready(root)
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    journal = update_restore_journal(journal.operation_id, root=root, target_key="control", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(journal.operation_id, root=root, target_key="single-user", target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:10Z")
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:11Z")
    before_rollback = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, now="2026-08-01T12:00:12Z")
    assert load_restore_journal(journal.operation_id, root=root) == before_rollback
    with pytest.raises(RestoreTransitionError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.FAILED_SAFE)

    # A second operation records a rollback that failed after only one target.
    root2 = _root(tmp_path / "second", monkeypatch); journal = _advance_to_ready(root2)
    journal = update_restore_journal(journal.operation_id, root=root2, stage=RestoreStage.REPLACING, now="2026-08-01T12:00:08Z")
    for key in journal.target_keys:
        journal = update_restore_journal(journal.operation_id, root=root2, target_key=key, target_state=TargetRestoreState.REPLACED, now="2026-08-01T12:00:09Z")
    journal = update_restore_journal(journal.operation_id, root=root2, stage=RestoreStage.ROLLBACK_REQUIRED, now="2026-08-01T12:00:10Z")
    journal = update_restore_journal(journal.operation_id, root=root2, target_key="control", target_state=TargetRestoreState.ROLLED_BACK, now="2026-08-01T12:00:11Z")
    journal = update_restore_journal(journal.operation_id, root=root2, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, now="2026-08-01T12:00:12Z")
    assert {fact.state for fact in journal.targets} == {TargetRestoreState.ROLLED_BACK, TargetRestoreState.REPLACED}


@pytest.mark.parametrize("field,value", [
    ("format_version", "wrong"),
    ("target_set_hash", "0" * 64),
    ("confirmation_value", "0" * 64),
    ("target_keys", ("single-user", "control")),
    ("created_at", "bad"),
])
def test_create_journal_validates_all_supplied_plan_fields(tmp_path, monkeypatch, field, value):
    root = _root(tmp_path, monkeypatch)
    with pytest.raises(RestorePlanError):
        create_restore_journal(replace(_plan(), **{field: value}), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)


def test_restore_lock_cleans_up_handle_after_permission_failure(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    import guarded_restore
    lock = RestoreLock(root); original = guarded_restore._private
    def fail_lock(path, directory=False):
        if path == lock.path: raise guarded_restore.RestoreJournalPersistenceError("internal")
        return original(path, directory)
    monkeypatch.setattr(guarded_restore, "_private", fail_lock)
    with pytest.raises(RestoreLockError): lock.__enter__()
    assert lock.handle is None
    monkeypatch.setattr(guarded_restore, "_private", original)
    with RestoreLock(root): pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are not asserted on Windows")
def test_journal_temporary_file_is_private_from_creation(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch); journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    path = root / f"operation-{journal.operation_id}" / "journal.json"
    before = path.read_bytes()
    import guarded_restore
    observed = []
    def fail_after_creation(source, destination):
        observed.append(Path(source).stat().st_mode & 0o777)
        raise OSError("injected")
    monkeypatch.setattr(guarded_restore.os, "replace", fail_after_creation)
    with pytest.raises(RestoreJournalPersistenceError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.VERIFIED)
    assert observed == [0o600]
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_root_safety_and_atomic_write_failure_preserves_journal(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    with pytest.raises(RestoreJournalError): validate_restore_root(tmp_path)
    journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    path = root / f"operation-{journal.operation_id}" / "journal.json"; before = path.read_bytes()
    import guarded_restore
    monkeypatch.setattr(guarded_restore.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(RestoreJournalPersistenceError): update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.VERIFIED)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_restore_lock_is_nonblocking_and_releases(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    with RestoreLock(root):
        with pytest.raises(RestoreLockError):
            with RestoreLock(root): pass
    with RestoreLock(root): pass


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable")
def test_journal_rejects_symlinked_operation(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch); root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir(); (root / "operation-restore-20260801T120000Z-a1b2c3d4").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RestoreJournalError): load_restore_journal("restore-20260801T120000Z-a1b2c3d4", root=root)


def test_mutation_boundary_never_calls_backup_or_sqlite(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    import guarded_restore
    monkeypatch.setattr(guarded_restore, "create_verified_backup", lambda: pytest.fail("backup called"), raising=False)
    monkeypatch.setattr(guarded_restore, "verify_verified_backup", lambda: pytest.fail("verification called"), raising=False)
    journal = create_restore_journal(_plan(), root=root, operation_id="restore-20260801T120000Z-a1b2c3d4", now=TIME)
    assert journal.stage is RestoreStage.PRECHECK


def test_transition_tables_are_closed_and_not_enum_order_based():
    import guarded_restore
    assert guarded_restore._GLOBAL_TRANSITIONS == {
        RestoreStage.PRECHECK: {RestoreStage.VERIFIED, RestoreStage.FAILED_SAFE},
        RestoreStage.VERIFIED: {RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.FAILED_SAFE},
        RestoreStage.CURRENT_SNAPSHOT_CREATED: {RestoreStage.RESTORE_STAGED, RestoreStage.FAILED_SAFE},
        RestoreStage.RESTORE_STAGED: {RestoreStage.STAGED_VERIFIED, RestoreStage.FAILED_SAFE},
        RestoreStage.STAGED_VERIFIED: {RestoreStage.REPLACEMENT_READY, RestoreStage.FAILED_SAFE},
        RestoreStage.REPLACEMENT_READY: {RestoreStage.REPLACING, RestoreStage.FAILED_SAFE},
        RestoreStage.REPLACING: {RestoreStage.REPLACED, RestoreStage.ROLLBACK_REQUIRED},
        RestoreStage.REPLACED: {RestoreStage.POSTCHECK_PASSED, RestoreStage.ROLLBACK_REQUIRED},
        RestoreStage.POSTCHECK_PASSED: {RestoreStage.COMPLETED, RestoreStage.ROLLBACK_REQUIRED},
        RestoreStage.ROLLBACK_REQUIRED: {RestoreStage.ROLLED_BACK, RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED},
        RestoreStage.ROLLED_BACK: {RestoreStage.FAILED_SAFE, RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED},
        RestoreStage.COMPLETED: set(), RestoreStage.FAILED_SAFE: set(),
        RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED: set(),
    }
    assert guarded_restore._TARGET_TRANSITIONS == {
        TargetRestoreState.PENDING: {TargetRestoreState.STAGED},
        TargetRestoreState.STAGED: {TargetRestoreState.STAGED_VERIFIED},
        TargetRestoreState.STAGED_VERIFIED: {TargetRestoreState.REPLACED},
        TargetRestoreState.REPLACED: {TargetRestoreState.ROLLED_BACK},
        TargetRestoreState.ROLLED_BACK: set(),
    }


def test_planning_and_inspector_never_invoke_mutation_functions(monkeypatch):
    import subprocess
    import process_lock
    import verified_backup

    def fail_mutation(*args, **kwargs):
        pytest.fail("Forbidden mutation function invoked by Phase 6B3A CLI")

    monkeypatch.setattr(verified_backup, "create_verified_backup", fail_mutation)
    monkeypatch.setattr(process_lock, "acquire_process_lock", fail_mutation)
    monkeypatch.setattr(subprocess, "run", fail_mutation)
    monkeypatch.setattr(subprocess, "Popen", fail_mutation)
    monkeypatch.setattr(subprocess, "call", fail_mutation)
