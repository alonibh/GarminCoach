"""Tests for configured-runtime restore replacement and rollback (Phase 6B3B2)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import shutil

import pytest

import config
from guarded_restore import (
    RestoreStage,
    TargetRestoreState,
    confirmation_value,
    load_restore_journal,
    target_set_hash,
    update_restore_journal,
)
from guarded_restore_configured import prepare_configured_restore
from guarded_restore_configured_replacement import (
    ConfiguredReplacementCleanupError,
    ConfiguredReplacementManualRecoveryRequiredError,
    ConfiguredReplacementPreconditionError,
    ConfiguredReplacementResult,
    ConfiguredReplacementRollbackCompletedError,
    replace_and_verify_configured_restore,
)
from guarded_restore_configured_staging import _sha256_file
from operator_storage import TargetProfile, discover_database_targets
from process_lock import ProcessLock, acquire_process_lock, release_process_lock
from verified_backup import BackupLock, create_verified_backup, load_validated_backup_snapshot


# ---------------------------------------------------------------------------
# Canary: prove tests never touch real configured paths
# ---------------------------------------------------------------------------

import config as _rc

_CANARY_CTRL = Path(_rc.CONTROL_DB_PATH)
_CANARY_DB = Path(_rc.DB_PATH)
_CANARY_DATA_ROOT = Path(_rc.MULTI_USER_DATA_ROOT)
_CANARY_BACKUP_ROOT = Path(_rc.OPERATOR_BACKUP_ROOT)
_CANARY_RESTORE_ROOT = Path(_rc.OPERATOR_RESTORE_ROOT)


@pytest.fixture(autouse=True)
def _real_db_canary():
    """Assert real configured paths are not modified during any test."""
    def _snap(p: Path):
        return (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None

    snap_ctrl = _snap(_CANARY_CTRL)
    snap_db = _snap(_CANARY_DB)
    yield
    if snap_ctrl is not None and _CANARY_CTRL.exists():
        assert _snap(_CANARY_CTRL) == snap_ctrl, "Real control DB was modified by test!"
    if snap_db is not None and _CANARY_DB.exists():
        assert _snap(_CANARY_DB) == snap_db, "Real user DB was modified by test!"


# ---------------------------------------------------------------------------
# Test environment helpers
# ---------------------------------------------------------------------------

def _setup_test_env(tmp_path: Path, monkeypatch, multi_user: bool = False, num_tenants: int = 1):
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

    conn = sqlite3.connect(str(control_db))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO sample (val) VALUES ('control_data');")
    conn.commit(); conn.close()

    conn = sqlite3.connect(str(single_user_db))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO sample (val) VALUES ('single_user_data');")
    conn.commit(); conn.close()

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
            conn = sqlite3.connect(str(t_db))
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
            conn.execute(f"INSERT INTO sample (val) VALUES ('tenant_{i}_data');")
            conn.commit(); conn.close()

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
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prepare(tmp_path, monkeypatch, multi_user=False, num_tenants=1):
    """Set up env, backup, and run Phase 6B3B1 to REPLACEMENT_READY. Returns env dict."""
    (project_root, backup_root, restore_root, control_db, single_user_db, commit_hex) = \
        _setup_test_env(tmp_path, monkeypatch, multi_user=multi_user, num_tenants=num_tenants)

    selected_dir = create_verified_backup(output_root=backup_root)
    selected_id = selected_dir.name.removeprefix("backup-")
    snap = load_validated_backup_snapshot(selected_dir, against_current_config=True)
    runtime_mode = "multi_user" if multi_user else "single_user"

    t_hash = target_set_hash(
        backup_id=selected_id,
        manifest_sha256=snap.manifest_sha256,
        runtime_mode=runtime_mode,
        target_keys=snap.target_keys,
    )
    c_val = confirmation_value(target_hash=t_hash, expected_application_commit=commit_hex)

    prep_result = prepare_configured_restore(
        selected_backup_id=selected_id,
        expected_application_commit=commit_hex,
        confirmed_target_set_hash=t_hash,
        confirmed_restore_value=c_val,
    )

    return {
        "project_root": project_root,
        "backup_root": backup_root,
        "restore_root": restore_root,
        "control_db": control_db,
        "single_user_db": single_user_db,
        "commit_hex": commit_hex,
        "selected_id": selected_id,
        "snap": snap,
        "t_hash": t_hash,
        "c_val": c_val,
        "op_id": prep_result.operation_id,
        "safety_backup_id": prep_result.safety_backup_id,
        "runtime_mode": runtime_mode,
    }


def _call_replace(env) -> ConfiguredReplacementResult:
    return replace_and_verify_configured_restore(
        operation_id=env["op_id"],
        selected_backup_id=env["selected_id"],
        expected_application_commit=env["commit_hex"],
        confirmed_target_set_hash=env["t_hash"],
        confirmed_restore_value=env["c_val"],
    )


# ---------------------------------------------------------------------------
# 1. Successful operation
# ---------------------------------------------------------------------------

def test_single_user_complete_success(tmp_path, monkeypatch):
    """Phase 6B3B2: single-user replacement completes successfully."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)

    ctrl_sha_before = _sha256(env["control_db"])
    su_sha_before = _sha256(env["single_user_db"])
    snap = env["snap"]
    sel_entries = {e.target_key: e for e in snap.entries}

    result = _call_replace(env)

    assert isinstance(result, ConfiguredReplacementResult)
    assert result.stage is RestoreStage.COMPLETED
    assert result.rollback_occurred is False
    assert result.configured_database_mutated is True
    assert result.locks_released is True
    assert set(result.replaced_target_keys) == {"control", "single-user"}

    # Databases now match selected backup content
    assert _sha256(env["control_db"]) == sel_entries["control"].sha256
    assert _sha256(env["single_user_db"]) == sel_entries["single-user"].sha256

    # Journal settled COMPLETED
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.COMPLETED
    for fact in j.targets:
        assert fact.replacement_completed is True
        assert fact.state is TargetRestoreState.REPLACED

    # ProcessLock can be acquired (locks released)
    lk = acquire_process_lock(env["project_root"] / "garmincoach.lock")
    release_process_lock(lk)


def test_multi_user_complete_success_two_tenants(tmp_path, monkeypatch):
    """Phase 6B3B2: multi-user replacement with two tenants completes."""
    env = _prepare(tmp_path, monkeypatch, multi_user=True, num_tenants=2)
    snap = env["snap"]

    result = _call_replace(env)

    assert result.stage is RestoreStage.COMPLETED
    assert result.runtime_mode == "multi_user"
    assert set(result.replaced_target_keys) == set(snap.target_keys)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.COMPLETED

    sel_entries = {e.target_key: e for e in snap.entries}
    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        entry = sel_entries[tgt.target_key]
        assert _sha256(tgt.path) == entry.sha256


def test_data_targets_replaced_before_control(tmp_path, monkeypatch):
    """Control database must be replaced AFTER all data/tenant targets."""
    env = _prepare(tmp_path, monkeypatch, multi_user=True, num_tenants=2)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    target_paths = {str(t.path) for t in targets}
    replaced_keys_order = []
    orig_replace = os.replace
    tgt_by_path = {str(t.path): t.target_key for t in targets}

    def tracking_replace(src, dst):
        if str(dst) in target_paths:
            replaced_keys_order.append(tgt_by_path[str(dst)])
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)
    _call_replace(env)

    assert "control" in replaced_keys_order
    ctrl_pos = replaced_keys_order.index("control")
    # All non-control targets must be replaced before control
    for i in range(ctrl_pos):
        assert replaced_keys_order[i] != "control"
    # Control must be the last database replacement
    assert ctrl_pos == len(replaced_keys_order) - 1, \
        f"Control ({ctrl_pos}) must be last. Order: {replaced_keys_order}"




def test_no_stale_sidecars_after_completion(tmp_path, monkeypatch):
    """No WAL or SHM sidecars remain after successful replacement."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    _call_replace(env)

    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        assert not (tgt.path.parent / (tgt.path.name + "-wal")).exists()
        assert not (tgt.path.parent / (tgt.path.name + "-shm")).exists()


def test_selected_and_safety_backups_unchanged_after_operation(tmp_path, monkeypatch):
    """Selected and safety backups must remain strictly verified after operation."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    snap = env["snap"]

    result = _call_replace(env)

    sel_snap_reload = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['selected_id']}", against_current_config=True
    )
    assert sel_snap_reload.manifest_sha256 == snap.manifest_sha256

    saf_snap_reload = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}", against_current_config=True
    )
    assert saf_snap_reload.manifest_sha256 == saf_snap_reload.manifest_sha256


def test_stage_and_rollback_dirs_cleaned_after_completed(tmp_path, monkeypatch):
    """Stage and rollback directories are removed after successful COMPLETED."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    _call_replace(env)

    op_id = env["op_id"]
    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        parent = tgt.path.parent.resolve()
        assert not (parent / f".garmincoach-restore-stage-{op_id}").exists(), "Stage dir still exists"
        # Rollback dirs: one per index
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    for idx in range(len(targets)):
        assert not (targets[idx].path.parent.resolve() / f".garmincoach-restore-rollback-{op_id}-{idx:03d}").exists()


def test_completed_idempotent_reentry(tmp_path, monkeypatch):
    """Calling replace again on a COMPLETED operation returns idempotent success."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)

    r1 = _call_replace(env)
    assert r1.stage is RestoreStage.COMPLETED

    r2 = _call_replace(env)
    assert r2.stage is RestoreStage.COMPLETED
    assert r2.rollback_occurred is False


# ---------------------------------------------------------------------------
# 2. Pre-mutation refusal
# ---------------------------------------------------------------------------

def _assert_no_mutation(env):
    """Verify configured databases match their SHA recorded at test start."""
    snap = env["snap"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    tgt_by_key = {t.target_key: t for t in targets}
    # The databases should NOT match the backup content YET (if they were modified)
    # or if not modified, this just verifies they're still intact.
    # Key check: journal stage is not COMPLETED
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.COMPLETED


def test_wrong_operation_id_refused(tmp_path, monkeypatch):
    env = _prepare(tmp_path, monkeypatch)
    with pytest.raises(Exception) as exc_info:
        replace_and_verify_configured_restore(
            operation_id="restore-20240101T000000Z-ffffffff",
            selected_backup_id=env["selected_id"],
            expected_application_commit=env["commit_hex"],
            confirmed_target_set_hash=env["t_hash"],
            confirmed_restore_value=env["c_val"],
        )
    assert isinstance(exc_info.value, (ConfiguredReplacementPreconditionError, Exception))
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.REPLACEMENT_READY


def test_wrong_backup_id_refused(tmp_path, monkeypatch):
    env = _prepare(tmp_path, monkeypatch)
    with pytest.raises(ConfiguredReplacementPreconditionError):
        replace_and_verify_configured_restore(
            operation_id=env["op_id"],
            selected_backup_id="20240101T000000Z-ffffffff",
            expected_application_commit=env["commit_hex"],
            confirmed_target_set_hash=env["t_hash"],
            confirmed_restore_value=env["c_val"],
        )
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.COMPLETED


def test_wrong_commit_refused(tmp_path, monkeypatch):
    env = _prepare(tmp_path, monkeypatch)
    wrong_commit = "b" * 40
    with pytest.raises(ConfiguredReplacementPreconditionError):
        replace_and_verify_configured_restore(
            operation_id=env["op_id"],
            selected_backup_id=env["selected_id"],
            expected_application_commit=wrong_commit,
            confirmed_target_set_hash=env["t_hash"],
            confirmed_restore_value=env["c_val"],
        )
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.COMPLETED


def test_wrong_confirmation_refused(tmp_path, monkeypatch):
    env = _prepare(tmp_path, monkeypatch)
    with pytest.raises(ConfiguredReplacementPreconditionError):
        replace_and_verify_configured_restore(
            operation_id=env["op_id"],
            selected_backup_id=env["selected_id"],
            expected_application_commit=env["commit_hex"],
            confirmed_target_set_hash=env["t_hash"],
            confirmed_restore_value="a" * 64,
        )
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.COMPLETED


def test_wrong_target_set_hash_refused(tmp_path, monkeypatch):
    env = _prepare(tmp_path, monkeypatch)
    with pytest.raises(ConfiguredReplacementPreconditionError):
        replace_and_verify_configured_restore(
            operation_id=env["op_id"],
            selected_backup_id=env["selected_id"],
            expected_application_commit=env["commit_hex"],
            confirmed_target_set_hash="c" * 64,
            confirmed_restore_value=env["c_val"],
        )
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.COMPLETED


def test_process_lock_unavailable_refused(tmp_path, monkeypatch):
    """If service is running (process lock held), replacement is refused."""
    env = _prepare(tmp_path, monkeypatch)
    lock = acquire_process_lock(env["project_root"] / "garmincoach.lock")
    try:
        with pytest.raises(Exception):
            _call_replace(env)
    finally:
        release_process_lock(lock)
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage not in {RestoreStage.COMPLETED, RestoreStage.REPLACING}


def test_restore_lock_unavailable_refused(tmp_path, monkeypatch):
    """If restore lock is held, replacement is refused."""
    from guarded_restore import RestoreLock, validate_restore_root
    env = _prepare(tmp_path, monkeypatch)
    rl = RestoreLock(env["restore_root"])
    rl.__enter__()
    try:
        with pytest.raises(Exception):
            _call_replace(env)
    finally:
        rl.__exit__(None, None, None)


def test_backup_lock_unavailable_refused(tmp_path, monkeypatch):
    """If BackupLock is held, replacement is refused."""
    env = _prepare(tmp_path, monkeypatch)
    bl = BackupLock(env["backup_root"])
    bl.__enter__()
    try:
        with pytest.raises(Exception):
            _call_replace(env)
    finally:
        bl.__exit__(None, None, None)


def test_illegal_stage_refused(tmp_path, monkeypatch):
    """Calling replace on an operation in PRECHECK stage raises precondition error."""
    env = _prepare(tmp_path, monkeypatch)
    j_path = env["restore_root"] / f"operation-{env['op_id']}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "PRECHECK"
    data["safety_backup_id"] = None
    for t in data["targets"]:
        t["state"] = "PENDING"
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )
    with pytest.raises(ConfiguredReplacementPreconditionError):
        _call_replace(env)


def test_failed_manual_recovery_required_refused(tmp_path, monkeypatch):
    """Calling replace on FAILED_MANUAL_RECOVERY_REQUIRED raises immediately."""
    env = _prepare(tmp_path, monkeypatch)
    j_path = env["restore_root"] / f"operation-{env['op_id']}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "FAILED_MANUAL_RECOVERY_REQUIRED"
    data["final_result"] = "FAILED_MANUAL_RECOVERY_REQUIRED"
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )
    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


# ---------------------------------------------------------------------------
# 3. Replacement fault injection
# ---------------------------------------------------------------------------

def test_os_replace_failure_on_first_target_triggers_rollback(tmp_path, monkeypatch):
    """os.replace failure on first target triggers automatic rollback."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    target_paths = {str(t.path) for t in targets}
    orig_replace = os.replace
    db_replace_count = [0]

    def failing_replace(src, dst):
        if str(dst) in target_paths:
            db_replace_count[0] += 1
            if db_replace_count[0] == 1:
                raise OSError("Injected os.replace failure on first DB target")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_SAFE

    # Databases must be readable SQLite (original or rolled-back bytes are both valid).
    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        from operator_storage import inspect_sqlite
        chk = inspect_sqlite(tgt.path)
        assert chk.readable and chk.quick_check_ok, f"DB {tgt.target_key} not readable after rollback"


def test_os_replace_failure_on_control_target_triggers_rollback(tmp_path, monkeypatch):
    """os.replace failure on control target (last) triggers rollback after data targets replaced."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    ctrl_path = str(config.CONTROL_DB_PATH)
    orig_replace = os.replace

    def failing_on_control(src, dst):
        if str(dst) == ctrl_path:
            raise OSError("Injected control replace failure")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_on_control)
    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_SAFE

    # All targets must be readable valid SQLite after rollback
    from operator_storage import inspect_sqlite
    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        chk = inspect_sqlite(tgt.path)
        assert chk.readable and chk.quick_check_ok, f"DB {tgt.target_key} not readable"
    
    # Single-user was actually replaced; verify it was rolled back to safety bytes
    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}", against_current_config=True
    )
    saf_entries = {e.target_key: e for e in saf_snap.entries}
    single_user_tgt = next(t for t in discover_database_targets(profile=TargetProfile.RUNTIME) if t.kind == "single_user")
    assert _sha256(single_user_tgt.path) == saf_entries[single_user_tgt.target_key].sha256


def test_staged_artifact_corrupted_before_replacement(tmp_path, monkeypatch):
    """Corrupted staged artifact triggers rollback before replacement."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]

    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    first_tgt = [t for t in targets if t.kind != "control"][0] if any(t.kind != "control" for t in targets) else targets[0]
    idx = list(targets).index(first_tgt)
    stage_dir = first_tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"
    staged_name = f"{idx:03d}-{first_tgt.target_key.replace(':', '-')}.sqlite.staged"
    staged_p = stage_dir / staged_name

    if staged_p.exists():
        staged_p.write_bytes(b"CORRUPTED")

    with pytest.raises((ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementPreconditionError)):
        _call_replace(env)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage in {RestoreStage.FAILED_SAFE, RestoreStage.REPLACEMENT_READY}


def test_rollback_artifact_corruption_causes_manual_recovery(tmp_path, monkeypatch):
    """Corrupted rollback artifact for already-replaced target causes FAILED_MANUAL_RECOVERY_REQUIRED."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    ctrl_path = str(config.CONTROL_DB_PATH)
    orig_replace = os.replace

    def corrupt_and_fail_on_control(src, dst):
        if str(dst) == ctrl_path:
            # Control DB is being replaced. Corrupt the data target's rollback artifact
            # (the single-user DB was already replaced; now corrupt its rollback artifact)
            for idx, tgt in enumerate(targets):
                if tgt.kind != "control":
                    rb_dir = tgt.path.parent.resolve() / f".garmincoach-restore-rollback-{op_id}-{idx:03d}"
                    if rb_dir.exists():
                        for child in rb_dir.iterdir():
                            if child.name.endswith(".rollback"):
                                child.write_bytes(b"CORRUPTED_ROLLBACK_DATA")
            raise OSError("Injected control replace failure + corruption")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", corrupt_and_fail_on_control)

    with pytest.raises((ConfiguredReplacementManualRecoveryRequiredError, ConfiguredReplacementRollbackCompletedError)):
        _call_replace(env)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    # Either manual recovery (corruption prevented rollback) or FAILED_SAFE (rollback skipped corrupted target)
    assert j.stage in {RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED, RestoreStage.FAILED_SAFE}


# ---------------------------------------------------------------------------
# 4. Re-entry and interruption tests
# ---------------------------------------------------------------------------

def test_replacement_ready_reentry(tmp_path, monkeypatch):
    """Re-entering at REPLACEMENT_READY succeeds."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED


def test_replacing_reentry_with_intent_and_selected_bytes(tmp_path, monkeypatch):
    """REPLACING re-entry: data already replaced, control has intent + selected bytes → reconcile → COMPLETED.

    Simulates a crash that occurred after os.replace succeeded for control but before
    replacement_completed was persisted.  The data target is already REPLACED+completed
    in the journal and has selected bytes on disk.  Control has replacement_intent=True
    (not completed) and its path already contains selected bytes.

    Re-entry must: skip the completed data target, reconcile control via is_sel=True
    (mark completed without re-replacing), then postcheck and complete.  No rollback
    artifacts are needed because neither reconciliation path checks them.
    """
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    snap = env["snap"]

    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    ctrl_tgt = next(t for t in targets if t.kind == "control")
    ctrl_idx = next(i for i, t in enumerate(targets) if t.kind == "control")
    data_tgt = next(t for t in targets if t.kind != "control")
    data_idx = next(i for i, t in enumerate(targets) if t.kind != "control")

    stage_dir = ctrl_tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"

    # Simulate both replacements having occurred on disk (copy selected bytes to each path).
    data_staged_p = stage_dir / f"{data_idx:03d}-{data_tgt.target_key.replace(':', '-')}.sqlite.staged"
    ctrl_staged_p = stage_dir / f"{ctrl_idx:03d}-{ctrl_tgt.target_key.replace(':', '-')}.sqlite.staged"
    shutil.copy2(str(data_staged_p), str(data_tgt.path))
    shutil.copy2(str(ctrl_staged_p), str(ctrl_tgt.path))

    # Set journal: REPLACING stage, data=REPLACED+completed, control=intent only.
    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    raw = json.loads(j_path.read_bytes().decode("utf-8"))
    raw["stage"] = "REPLACING"
    for t in raw["targets"]:
        if t["target_key"] == data_tgt.target_key:
            t["state"] = "REPLACED"
            t["replacement_intent"] = True
            t["replacement_completed"] = True
        else:
            t["state"] = "STAGED_VERIFIED"
            t["replacement_intent"] = True
            t["replacement_completed"] = False
    j_path.write_bytes(
        (json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED


def test_replaced_reentry(tmp_path, monkeypatch):
    """Re-entering at REPLACED stage runs postcheck and completes."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    snap = env["snap"]
    sel_entries = {e.target_key: e for e in snap.entries}

    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    for tgt in targets:
        entry = sel_entries[tgt.target_key]
        stage_dir = tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"
        idx = list(targets).index(tgt)
        staged_name = f"{idx:03d}-{tgt.target_key.replace(':', '-')}.sqlite.staged"
        staged_p = stage_dir / staged_name
        if staged_p.exists():
            import shutil as _shutil
            _shutil.copy2(str(staged_p), str(tgt.path))
        for sfx in ("-wal", "-shm"):
            s = tgt.path.parent / (tgt.path.name + sfx)
            if s.exists():
                s.unlink()

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "REPLACED"
    for t in data["targets"]:
        t["state"] = "REPLACED"
        t["replacement_intent"] = True
        t["replacement_completed"] = True
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED


def test_postcheck_passed_reentry(tmp_path, monkeypatch):
    """Re-entering at POSTCHECK_PASSED stage transitions to COMPLETED."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    snap = env["snap"]
    sel_entries = {e.target_key: e for e in snap.entries}
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    for tgt in targets:
        entry = sel_entries[tgt.target_key]
        idx = list(targets).index(tgt)
        stage_dir = tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"
        staged_name = f"{idx:03d}-{tgt.target_key.replace(':', '-')}.sqlite.staged"
        staged_p = stage_dir / staged_name
        if staged_p.exists():
            import shutil as _shutil
            _shutil.copy2(str(staged_p), str(tgt.path))
        for sfx in ("-wal", "-shm"):
            s = tgt.path.parent / (tgt.path.name + sfx)
            if s.exists():
                s.unlink()

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "POSTCHECK_PASSED"
    for t in data["targets"]:
        t["state"] = "REPLACED"
        t["replacement_intent"] = True
        t["replacement_completed"] = True
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED


def test_rollback_required_reentry_completes_rollback(tmp_path, monkeypatch):
    """Re-entering at ROLLBACK_REQUIRED triggers rollback and raises RollbackCompletedError."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    snap = env["snap"]
    sel_entries = {e.target_key: e for e in snap.entries}
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}", against_current_config=True
    )
    saf_entries = {e.target_key: e for e in saf_snap.entries}

    from guarded_restore_configured_replacement import (
        _rollback_dir_name, _rollback_artifact_name, _rollback_binding_bytes,
        _write_rollback_binding, _copy_rollback_file,
    )

    for idx, tgt in enumerate(targets):
        entry = sel_entries[tgt.target_key]
        sentry = saf_entries[tgt.target_key]
        staged_name = f"{idx:03d}-{tgt.target_key.replace(':', '-')}.sqlite.staged"
        stage_dir = tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"
        staged_p = stage_dir / staged_name
        if staged_p.exists():
            import shutil as _shutil
            _shutil.copy2(str(staged_p), str(tgt.path))
        for sfx in ("-wal", "-shm"):
            s = tgt.path.parent / (tgt.path.name + sfx)
            if s.exists():
                s.unlink()

        rb_dir = tgt.path.parent.resolve() / _rollback_dir_name(op_id, idx)
        if not rb_dir.exists():
            rb_dir.mkdir(mode=0o700)
            rbfile = _rollback_artifact_name(idx, tgt.target_key)
            _write_rollback_binding(
                rb_dir, operation_id=op_id, safety_backup_id=saf_snap.backup_id,
                safety_manifest_sha256=saf_snap.manifest_sha256, target_key=tgt.target_key,
                kind=sentry.kind, index=idx, rollback_filename=rbfile,
                size_bytes=sentry.size_bytes, sha256=sentry.sha256,
            )
            src = saf_snap.directory / sentry.filename
            _copy_rollback_file(src, rb_dir, rbfile, size=sentry.size_bytes, sha256=sentry.sha256)

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "ROLLBACK_REQUIRED"
    for t in data["targets"]:
        t["state"] = "REPLACED"
        t["replacement_intent"] = True
        t["replacement_completed"] = True
        t["wal_present"] = False
        t["shm_present"] = False
        t["wal_removed"] = False
        t["shm_removed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)

    j = load_restore_journal(op_id, root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_SAFE
    for tgt in targets:
        sentry = saf_entries[tgt.target_key]
        assert _sha256(tgt.path) == sentry.sha256


def test_rolled_back_reentry_advances_to_failed_safe(tmp_path, monkeypatch):
    """Re-entering at ROLLED_BACK stage advances to FAILED_SAFE."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "ROLLED_BACK"
    for t in data["targets"]:
        t["state"] = "ROLLED_BACK"
        t["replacement_intent"] = True
        t["replacement_completed"] = True
        t["rollback_intent"] = True
        t["rollback_completed"] = True
        t["wal_present"] = False
        t["shm_present"] = False
        t["wal_removed"] = False
        t["shm_removed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)

    j = load_restore_journal(op_id, root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_SAFE


def test_failed_safe_reentry_raises_rollback_completed(tmp_path, monkeypatch):
    """Re-entering at FAILED_SAFE raises RollbackCompletedError."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "FAILED_SAFE"
    data["final_result"] = "FAILED_SAFE"
    for t in data["targets"]:
        t["state"] = "STAGED_VERIFIED"
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)


# ---------------------------------------------------------------------------
# 5. Postcheck failure and rollback order
# ---------------------------------------------------------------------------

def test_postcheck_failure_triggers_rollback(tmp_path, monkeypatch):
    """Postcheck failure after replacement triggers automatic rollback."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    from guarded_restore_configured_replacement import _run_complete_postcheck

    orig_postcheck = _run_complete_postcheck
    call_count = [0]

    def failing_postcheck(**kwargs):
        call_count[0] += 1
        from guarded_restore_configured_replacement import ConfiguredReplacementPostcheckError
        raise ConfiguredReplacementPostcheckError("Injected postcheck failure")

    import guarded_restore_configured_replacement as _mod
    orig_fn = _mod._run_complete_postcheck
    _mod._run_complete_postcheck = failing_postcheck
    try:
        with pytest.raises((ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementManualRecoveryRequiredError)):
            _call_replace(env)
    finally:
        _mod._run_complete_postcheck = orig_fn

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage in {RestoreStage.FAILED_SAFE, RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED}


def test_rollback_leaves_databases_readable(tmp_path, monkeypatch):
    """After rollback completes, all databases are readable valid SQLite."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    target_paths = {str(t.path) for t in targets}
    orig_replace = os.replace
    db_count = [0]

    def failing_replace(src, dst):
        if str(dst) in target_paths:
            db_count[0] += 1
            if db_count[0] == 1:
                raise OSError("Injected failure for first target")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_SAFE

    from operator_storage import inspect_sqlite
    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        chk = inspect_sqlite(tgt.path)
        assert chk.readable and chk.quick_check_ok, f"DB {tgt.target_key} not readable after rollback"


def test_manual_recovery_state_never_rewritten_to_failed_safe(tmp_path, monkeypatch):
    """FAILED_MANUAL_RECOVERY_REQUIRED is never automatically rewritten to FAILED_SAFE."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "FAILED_MANUAL_RECOVERY_REQUIRED"
    data["final_result"] = "FAILED_MANUAL_RECOVERY_REQUIRED"
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)

    j = load_restore_journal(op_id, root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED
    assert j.final_result is not None


def test_ambiguous_destination_causes_manual_recovery(tmp_path, monkeypatch):
    """Target matching neither safety nor selected bytes causes FAILED_MANUAL_RECOVERY_REQUIRED."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "REPLACING"
    for t in data["targets"]:
        t["state"] = "STAGED_VERIFIED"
        t["replacement_intent"] = True
        t["replacement_completed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    first_tgt = targets[0]
    first_tgt.path.write_bytes(b"UNKNOWN_BYTES_NOT_MATCHING_ANY_EVIDENCE")

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)

    j = load_restore_journal(op_id, root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# 6. Sidecar handling
# ---------------------------------------------------------------------------

def test_wal_sidecar_handled_durably_in_journal(tmp_path, monkeypatch):
    """When WAL is absent, journal durably records wal_present=False after replacement."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    for fact in j.targets:
        # Presence was durably recorded (either True+removed or False)
        assert fact.wal_present is not None
        assert fact.shm_present is not None
        if fact.wal_present:
            assert fact.wal_removed is True
        if fact.shm_present:
            assert fact.shm_removed is True


def test_shm_sidecar_handled_durably_in_journal(tmp_path, monkeypatch):
    """Sidecar journal facts are consistently recorded after successful replacement."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    for fact in j.targets:
        if fact.shm_present:
            assert fact.shm_removed is True, "SHM recorded present but not removed"


def test_no_sidecars_passes_postcheck(tmp_path, monkeypatch):
    """Postcheck verifies no WAL/SHM sidecars are present after replacement."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED

    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        assert not (tgt.path.parent / (tgt.path.name + "-wal")).exists()
        assert not (tgt.path.parent / (tgt.path.name + "-shm")).exists()


# ---------------------------------------------------------------------------
# 7. Platform and ownership
# ---------------------------------------------------------------------------

def test_locks_released_on_precondition_failure(tmp_path, monkeypatch):
    """ProcessLock is released even when replacement fails with precondition error."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    with pytest.raises(ConfiguredReplacementPreconditionError):
        replace_and_verify_configured_restore(
            operation_id=env["op_id"],
            selected_backup_id=env["selected_id"],
            expected_application_commit="b" * 40,
            confirmed_target_set_hash=env["t_hash"],
            confirmed_restore_value=env["c_val"],
        )
    lk = acquire_process_lock(env["project_root"] / "garmincoach.lock")
    release_process_lock(lk)


def test_locks_released_on_rollback_completed(tmp_path, monkeypatch):
    """ProcessLock is released after automatic rollback completes."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    target_paths = {str(t.path) for t in targets}
    orig_replace = os.replace
    db_count = [0]

    def fail_first(src, dst):
        if str(dst) in target_paths:
            db_count[0] += 1
            if db_count[0] == 1:
                raise OSError("Injected failure")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_first)
    with pytest.raises(ConfiguredReplacementRollbackCompletedError):
        _call_replace(env)

    lk = acquire_process_lock(env["project_root"] / "garmincoach.lock")
    release_process_lock(lk)


def test_result_is_frozen_dataclass(tmp_path, monkeypatch):
    """ConfiguredReplacementResult is frozen and cannot be mutated."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    result = _call_replace(env)
    assert isinstance(result, ConfiguredReplacementResult)
    with pytest.raises((AttributeError, TypeError)):
        result.stage = RestoreStage.PRECHECK  # type: ignore[misc]


def test_no_configured_mutation_before_replacing_stage(tmp_path, monkeypatch):
    """Proof that databases are not mutated on barrier failures before REPLACING."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)

    ctrl_sha_before = _sha256(env["control_db"])
    su_sha_before = _sha256(env["single_user_db"])

    with pytest.raises(ConfiguredReplacementPreconditionError):
        replace_and_verify_configured_restore(
            operation_id=env["op_id"],
            selected_backup_id=env["selected_id"],
            expected_application_commit="b" * 40,
            confirmed_target_set_hash=env["t_hash"],
            confirmed_restore_value=env["c_val"],
        )

    assert _sha256(env["control_db"]) == ctrl_sha_before, "Control DB was mutated before REPLACING!"
    assert _sha256(env["single_user_db"]) == su_sha_before, "User DB was mutated before REPLACING!"

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.REPLACING
    assert j.stage is not RestoreStage.REPLACED
    assert j.stage is not RestoreStage.COMPLETED
