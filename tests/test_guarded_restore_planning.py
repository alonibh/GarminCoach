from __future__ import annotations

import json
import os
from pathlib import Path

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
    journal = update_restore_journal(journal.operation_id, root=root, stage=RestoreStage.STAGED_VERIFIED, now="2026-08-01T12:00:05Z")
    for key in journal.target_keys:
        journal = update_restore_journal(journal.operation_id, root=root, target_key=key, target_state=TargetRestoreState.STAGED_VERIFIED, now="2026-08-01T12:00:06Z")
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
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RestoreJournalError): load_restore_journal(journal.operation_id, root=root)


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
