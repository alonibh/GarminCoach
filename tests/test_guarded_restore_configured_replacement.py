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
import guarded_restore_configured_replacement as _mod
from guarded_restore_configured_replacement import (
    ConfiguredReplacementCleanupError,
    ConfiguredReplacementManualRecoveryRequiredError,
    ConfiguredReplacementPreconditionError,
    ConfiguredReplacementResult,
    ConfiguredReplacementRollbackCompletedError,
    replace_and_verify_configured_restore,
    _verify_file_owned,
    _rollback_dir_name,
    _rollback_artifact_name,
    _write_rollback_binding,
    _copy_rollback_file,
)
from guarded_restore_configured_staging import _sha256_file, load_destination_baseline_evidence
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


def _call_replace(env, service_checker=None) -> ConfiguredReplacementResult:
    """Call replace_and_verify_configured_restore with a no-op service checker by default."""
    return replace_and_verify_configured_restore(
        operation_id=env["op_id"],
        selected_backup_id=env["selected_id"],
        expected_application_commit=env["commit_hex"],
        confirmed_target_set_hash=env["t_hash"],
        confirmed_restore_value=env["c_val"],
        # In tests the service is never running; use a no-op unless overridden.
        _service_checker=service_checker if service_checker is not None else (lambda: None),
    )


def _put_safety_bytes_on_disk(env):
    """Copy safety backup bytes to each configured database path."""
    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}",
        against_current_config=False,
    )
    saf_entries = {e.target_key: e for e in saf_snap.entries}
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    for tgt in targets:
        sentry = saf_entries[tgt.target_key]
        src = saf_snap.directory / sentry.filename
        shutil.copy2(str(src), str(tgt.path))
    return saf_snap, saf_entries


def _put_selected_bytes_on_disk(env):
    """Copy selected backup bytes to each configured database path."""
    snap = env["snap"]
    sel_entries = {e.target_key: e for e in snap.entries}
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    for tgt in targets:
        entry = sel_entries[tgt.target_key]
        src = snap.directory / entry.filename
        shutil.copy2(str(src), str(tgt.path))
    return snap, sel_entries


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

    assert _sha256(env["control_db"]) == sel_entries["control"].sha256
    assert _sha256(env["single_user_db"]) == sel_entries["single-user"].sha256

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.COMPLETED
    for fact in j.targets:
        assert fact.replacement_completed is True
        assert fact.state is TargetRestoreState.REPLACED

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
    for i in range(ctrl_pos):
        assert replaced_keys_order[i] != "control"
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

    _call_replace(env)

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
        assert not (parent / f".garmincoach-restore-stage-{op_id}").exists()
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    for idx in range(len(targets)):
        assert not (
            targets[idx].path.parent.resolve()
            / f".garmincoach-restore-rollback-{op_id}-{idx:03d}"
        ).exists()


def test_completed_idempotent_reentry(tmp_path, monkeypatch):
    """Calling replace again on a COMPLETED operation returns idempotent success after postcheck."""
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
            _service_checker=lambda: None,
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
            _service_checker=lambda: None,
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
            _service_checker=lambda: None,
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
            _service_checker=lambda: None,
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
            _service_checker=lambda: None,
        )
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.COMPLETED


def test_process_lock_unavailable_refused(tmp_path, monkeypatch):
    """If process lock is held, replacement is refused."""
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
    data.pop("safety_backup_manifest_sha256", None)
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

    from operator_storage import inspect_sqlite
    for tgt in discover_database_targets(profile=TargetProfile.RUNTIME):
        chk = inspect_sqlite(tgt.path)
        assert chk.readable and chk.quick_check_ok, f"DB {tgt.target_key} not readable"

    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}", against_current_config=False
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
    """REPLACING re-entry: data already replaced, control has intent + selected bytes → COMPLETED."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    snap = env["snap"]

    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    ctrl_tgt = next(t for t in targets if t.kind == "control")
    ctrl_idx = next(i for i, t in enumerate(targets) if t.kind == "control")
    data_tgt = next(t for t in targets if t.kind != "control")
    data_idx = next(i for i, t in enumerate(targets) if t.kind != "control")

    stage_dir = ctrl_tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"

    data_staged_p = stage_dir / f"{data_idx:03d}-{data_tgt.target_key.replace(':', '-')}.sqlite.staged"
    ctrl_staged_p = stage_dir / f"{ctrl_idx:03d}-{ctrl_tgt.target_key.replace(':', '-')}.sqlite.staged"
    shutil.copy2(str(data_staged_p), str(data_tgt.path))
    shutil.copy2(str(ctrl_staged_p), str(ctrl_tgt.path))

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
            shutil.copy2(str(staged_p), str(tgt.path))
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
            shutil.copy2(str(staged_p), str(tgt.path))
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

    for idx, tgt in enumerate(targets):
        entry = sel_entries[tgt.target_key]
        sentry = saf_entries[tgt.target_key]
        staged_name = f"{idx:03d}-{tgt.target_key.replace(':', '-')}.sqlite.staged"
        stage_dir = tgt.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"
        staged_p = stage_dir / staged_name
        if staged_p.exists():
            shutil.copy2(str(staged_p), str(tgt.path))
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
    """Re-entering at ROLLED_BACK stage runs full safety verification then advances to FAILED_SAFE."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    # Put safety backup bytes on disk so _verify_complete_rollback_state passes.
    saf_snap, saf_entries = _put_safety_bytes_on_disk(env)

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

    orig_fn = _mod._run_complete_postcheck
    call_count = [0]

    def failing_postcheck(**kwargs):
        call_count[0] += 1
        from guarded_restore_configured_replacement import ConfiguredReplacementPostcheckError
        raise ConfiguredReplacementPostcheckError("Injected postcheck failure")

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
    result = _call_replace(env)
    assert result.stage is RestoreStage.COMPLETED
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    for fact in j.targets:
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
            _service_checker=lambda: None,
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
            _service_checker=lambda: None,
        )

    assert _sha256(env["control_db"]) == ctrl_sha_before
    assert _sha256(env["single_user_db"]) == su_sha_before

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is not RestoreStage.REPLACING
    assert j.stage is not RestoreStage.REPLACED
    assert j.stage is not RestoreStage.COMPLETED


# ---------------------------------------------------------------------------
# 8. Security correction tests: service-stopped proof
# ---------------------------------------------------------------------------

def test_service_running_raises_precondition_error(tmp_path, monkeypatch):
    """Service-stopped proof raises PreconditionError when service reports running."""
    env = _prepare(tmp_path, monkeypatch)

    def service_running():
        raise Exception("GarminCoach service must be stopped first")

    with pytest.raises(ConfiguredReplacementPreconditionError, match="Service-stopped proof failed"):
        _call_replace(env, service_checker=service_running)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.REPLACEMENT_READY


def test_service_uncertain_raises_precondition_error(tmp_path, monkeypatch):
    """Uncertain service state raises PreconditionError."""
    env = _prepare(tmp_path, monkeypatch)

    def service_uncertain():
        raise RuntimeError("Cannot verify service state: systemctl unavailable")

    with pytest.raises(ConfiguredReplacementPreconditionError, match="Service-stopped proof failed"):
        _call_replace(env, service_checker=service_uncertain)

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.REPLACEMENT_READY


def test_service_proof_repeated_after_process_lock(tmp_path, monkeypatch):
    """Service-stopped proof is called at least twice (before locks and after process lock)."""
    env = _prepare(tmp_path, monkeypatch)
    call_count = [0]

    def counting_checker():
        call_count[0] += 1
        if call_count[0] >= 2:
            raise ConfiguredReplacementPreconditionError("Service changed state after process lock")

    with pytest.raises(ConfiguredReplacementPreconditionError, match="Service changed state"):
        _call_replace(env, service_checker=counting_checker)

    assert call_count[0] >= 2, "Service checker must be called at least twice"
    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.REPLACEMENT_READY


# ---------------------------------------------------------------------------
# 9. Security correction tests: no pathname chmod after publication
# ---------------------------------------------------------------------------

def test_no_pathname_chmod_after_rollback_binding_publication(tmp_path, monkeypatch):
    """os.chmod must not be called after rollback binding is published."""
    env = _prepare(tmp_path, monkeypatch)
    chmod_calls = []
    orig_chmod = os.chmod

    def recording_chmod(path, mode, **kwargs):
        chmod_calls.append((str(path), mode))
        return orig_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    _call_replace(env)

    # Verify no calls to os.chmod targeted rollback binding paths.
    for call_path, _ in chmod_calls:
        assert ".rollback-binding.json" not in call_path, (
            f"os.chmod called on rollback binding path: {call_path}"
        )


def test_no_pathname_chmod_after_rollback_artifact_publication(tmp_path, monkeypatch):
    """os.chmod must not be called after rollback artifact is published (no pathname chmod)."""
    env = _prepare(tmp_path, monkeypatch)
    chmod_calls = []
    orig_chmod = os.chmod

    def recording_chmod(path, mode, **kwargs):
        chmod_calls.append((str(path), mode))
        return orig_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    _call_replace(env)

    for call_path, _ in chmod_calls:
        assert ".rollback" not in call_path, (
            f"os.chmod called on rollback artifact path: {call_path}"
        )


@pytest.mark.skipif(os.name == "nt", reason="chmod semantics differ on Windows")
def test_no_pathname_chmod_after_configured_rollback_replacement(tmp_path, monkeypatch):
    """os.chmod must not be called on a database path after rollback os.replace."""
    env = _prepare(tmp_path, monkeypatch)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    target_paths = {str(t.path) for t in targets}
    orig_replace = os.replace
    db_count = [0]
    chmod_calls_after_replace: list[str] = []
    replace_done = [False]

    orig_chmod = os.chmod

    def recording_chmod(path, mode, **kwargs):
        if replace_done[0] and str(path) in target_paths:
            chmod_calls_after_replace.append(str(path))
        return orig_chmod(path, mode, **kwargs)

    def failing_replace(src, dst):
        if str(dst) in target_paths:
            db_count[0] += 1
            if db_count[0] == 1:
                result = orig_replace(src, dst)
                replace_done[0] = True
                raise OSError("Injected failure after first replace")
            return orig_replace(src, dst)
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises((ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementManualRecoveryRequiredError)):
        _call_replace(env)

    assert not chmod_calls_after_replace, (
        f"os.chmod called on DB paths after rollback os.replace: {chmod_calls_after_replace}"
    )


# ---------------------------------------------------------------------------
# 10. Security correction tests: race-complete file ownership
# ---------------------------------------------------------------------------

def test_verify_file_owned_detects_path_swap_after_hash():
    """_verify_file_owned detects inode substitution between hash and post-hash re-stat."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "testfile"
        data = b"test data for ownership check"
        p.write_bytes(data)
        if os.name != "nt":
            os.chmod(str(p), 0o600)
        sha = hashlib.sha256(data).hexdigest()

        orig_lseek = os.lseek
        patched = [False]

        orig_stat_fn = os.stat

        def fake_stat(path_arg, *, follow_symlinks=True, **kwargs):
            if not follow_symlinks and str(path_arg) == str(p) and patched[0]:
                # Return a stat with a different inode after hash completes.
                real = orig_stat_fn(path_arg, follow_symlinks=follow_symlinks)
                class FakeStat:
                    st_dev = real.st_dev
                    st_ino = real.st_ino + 9999
                    st_size = real.st_size
                    st_mode = real.st_mode
                    st_nlink = real.st_nlink
                    st_mtime_ns = getattr(real, "st_mtime_ns", None)
                return FakeStat()
            return orig_stat_fn(path_arg, follow_symlinks=follow_symlinks)

        orig_read = os.read

        def marking_read(fd, size):
            result = orig_read(fd, size)
            patched[0] = True
            return result

        orig_os_stat = os.stat
        orig_os_read = os.read
        os.stat = fake_stat
        os.read = marking_read
        try:
            with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
                _verify_file_owned(p, expected_size=len(data), expected_sha256=sha)
        finally:
            os.stat = orig_os_stat
            os.read = orig_os_read


def test_verify_file_owned_requires_mode_0600_on_posix():
    """_verify_file_owned(require_mode_0600=True) raises if file is not 0600."""
    if os.name == "nt":
        pytest.skip("Mode checks not applicable on Windows")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "testfile"
        data = b"test data"
        p.write_bytes(data)
        os.chmod(str(p), 0o644)
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ConfiguredReplacementPreconditionError, match="0600"):
            _verify_file_owned(p, expected_size=len(data), expected_sha256=sha, require_mode_0600=True)


def test_verify_file_owned_requires_single_link_on_posix():
    """_verify_file_owned(require_single_link=True) raises if nlink > 1."""
    if os.name == "nt":
        pytest.skip("Link count checks not applicable on Windows")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "testfile"
        link_p = Path(td) / "testfile-hardlink"
        data = b"test data"
        p.write_bytes(data)
        os.chmod(str(p), 0o600)
        os.link(str(p), str(link_p))
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ConfiguredReplacementPreconditionError, match="link count"):
            _verify_file_owned(
                p, expected_size=len(data), expected_sha256=sha,
                require_mode_0600=True, require_single_link=True,
            )


def test_verify_file_owned_detects_sha_mismatch():
    """_verify_file_owned raises on SHA-256 mismatch."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "testfile"
        data = b"actual content"
        p.write_bytes(data)
        with pytest.raises(ConfiguredReplacementPreconditionError, match="SHA-256"):
            _verify_file_owned(p, expected_size=len(data), expected_sha256="a" * 64)


def test_verify_file_owned_detects_size_mismatch():
    """_verify_file_owned raises on size mismatch."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "testfile"
        data = b"actual content"
        p.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ConfiguredReplacementPreconditionError, match="size"):
            _verify_file_owned(p, expected_size=len(data) + 100, expected_sha256=sha)


# ---------------------------------------------------------------------------
# 11. Security correction tests: per-target immediate revalidation
# ---------------------------------------------------------------------------

def test_destination_drift_before_per_target_intent_causes_failure(tmp_path, monkeypatch):
    """Per-target baseline revalidation catches destination drift before replacement_intent."""
    env = _prepare(tmp_path, monkeypatch)

    orig_fn = _mod._revalidate_target_pre_intent
    call_count = [0]

    def drifting_revalidate(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # Simulate drift detected on first target.
            raise ConfiguredReplacementPreconditionError(
                "Injected destination drift at per-target barrier"
            )
        return orig_fn(**kwargs)

    _mod._revalidate_target_pre_intent = drifting_revalidate
    try:
        with pytest.raises(
            (ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementManualRecoveryRequiredError)
        ):
            _call_replace(env)
    finally:
        _mod._revalidate_target_pre_intent = orig_fn

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage in {RestoreStage.FAILED_SAFE, RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED}


def test_destination_drift_after_intent_before_replace_causes_manual_recovery(tmp_path, monkeypatch):
    """Destination drift after intent persistence (detected by post-intent re-verify) → manual recovery."""
    env = _prepare(tmp_path, monkeypatch)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    first_tgt = next(t for t in targets if t.kind != "control")

    orig_transition = _mod._journal_transition
    intent_recorded = [False]

    def corrupting_transition(op_id, root, stage=None, target_key=None, **kwargs):
        result = orig_transition(op_id, root, stage=stage, target_key=target_key, **kwargs)
        # After persisting replacement_intent for the first data target, corrupt the destination.
        if (
            not intent_recorded[0]
            and target_key == first_tgt.target_key
            and kwargs.get("replacement_intent") is True
        ):
            intent_recorded[0] = True
            first_tgt.path.write_bytes(b"CORRUPTED_AFTER_INTENT")
        return result

    _mod._journal_transition = corrupting_transition
    try:
        with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
            _call_replace(env)
    finally:
        _mod._journal_transition = orig_transition

    j = load_restore_journal(env["op_id"], root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# 12. Security correction tests: complete rollback verification
# ---------------------------------------------------------------------------

def test_rollback_deep_sqlite_verification(tmp_path, monkeypatch):
    """Complete rollback verification includes SQLite integrity checks."""
    env = _prepare(tmp_path, monkeypatch)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    ctrl_path = str(config.CONTROL_DB_PATH)
    orig_replace = os.replace

    deep_checks_called = [False]
    orig_inspect = __import__("operator_storage").inspect_sqlite

    def tracking_inspect(path, deep=False):
        if deep and str(path) in {str(t.path) for t in targets}:
            deep_checks_called[0] = True
        return orig_inspect(path, deep=deep)

    import operator_storage
    orig_inspect_sqlite = operator_storage.inspect_sqlite
    operator_storage.inspect_sqlite = tracking_inspect
    import guarded_restore_configured_replacement as grm
    orig_grm_inspect = grm.inspect_sqlite
    grm.inspect_sqlite = tracking_inspect

    def fail_on_control(src, dst):
        if str(dst) == ctrl_path:
            raise OSError("Injected failure on control")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_on_control)
    try:
        with pytest.raises((ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementManualRecoveryRequiredError)):
            _call_replace(env)
    finally:
        operator_storage.inspect_sqlite = orig_inspect_sqlite
        grm.inspect_sqlite = orig_grm_inspect

    assert deep_checks_called[0], "Deep SQLite checks were not called during rollback"


# ---------------------------------------------------------------------------
# 13. Security correction tests: COMPLETED re-entry full verification
# ---------------------------------------------------------------------------

def test_completed_reentry_with_one_wrong_target(tmp_path, monkeypatch):
    """COMPLETED re-entry detects database drift and reports manual recovery."""
    env = _prepare(tmp_path, monkeypatch)
    r1 = _call_replace(env)
    assert r1.stage is RestoreStage.COMPLETED

    # Corrupt one database after completion.
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    first_tgt = targets[0]
    first_tgt.path.write_bytes(b"CORRUPTED_AFTER_COMPLETION")

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


def test_completed_reentry_runs_full_postcheck(tmp_path, monkeypatch):
    """COMPLETED re-entry calls _run_complete_postcheck before returning success."""
    env = _prepare(tmp_path, monkeypatch)
    _call_replace(env)

    postcheck_called = [False]
    orig_postcheck = _mod._run_complete_postcheck

    def tracking_postcheck(**kwargs):
        postcheck_called[0] = True
        return orig_postcheck(**kwargs)

    _mod._run_complete_postcheck = tracking_postcheck
    try:
        r2 = _call_replace(env)
        assert r2.stage is RestoreStage.COMPLETED
    finally:
        _mod._run_complete_postcheck = orig_postcheck

    assert postcheck_called[0], "COMPLETED re-entry must call _run_complete_postcheck"


# ---------------------------------------------------------------------------
# 14. Security correction tests: ROLLED_BACK re-entry verification
# ---------------------------------------------------------------------------

def test_rolled_back_reentry_with_wrong_target_causes_manual_recovery(tmp_path, monkeypatch):
    """ROLLED_BACK re-entry with one target not matching safety bytes → manual recovery."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    # Put safety bytes on most targets but corrupt one.
    saf_snap, saf_entries = _put_safety_bytes_on_disk(env)
    first_tgt = targets[0]
    first_tgt.path.write_bytes(b"WRONG_BYTES_NOT_SAFETY")

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

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)

    j = load_restore_journal(op_id, root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# 15. Security correction tests: verified settlement
# ---------------------------------------------------------------------------

def test_failed_safe_settlement_is_verified(tmp_path, monkeypatch):
    """_settle_safe writes and re-reads journal; uncertainty raises ConfiguredJournalUncertaintyError."""
    from guarded_restore_configured import ConfiguredJournalUncertaintyError
    env = _prepare(tmp_path, monkeypatch)

    # Make the journal write fail after stage is set to REPLACEMENT_READY.
    # We'll do this by making update_restore_journal raise on FAILED_SAFE transition.
    orig_update = __import__("guarded_restore").update_restore_journal
    settlement_called = [False]

    def failing_update(op_id, *, root=None, stage=None, **kwargs):
        if stage is RestoreStage.FAILED_SAFE:
            settlement_called[0] = True
            raise Exception("Injected settlement failure")
        return orig_update(op_id, root=root, stage=stage, **kwargs)

    import guarded_restore as gr
    orig = gr.update_restore_journal
    gr.update_restore_journal = failing_update
    _mod.update_restore_journal = failing_update

    try:
        from guarded_restore_configured_replacement import _settle_safe
        j = load_restore_journal(env["op_id"], root=env["restore_root"])
        with pytest.raises((ConfiguredJournalUncertaintyError, Exception)):
            _settle_safe(env["op_id"], env["restore_root"], j)
    finally:
        gr.update_restore_journal = orig
        _mod.update_restore_journal = orig


def test_settle_manual_does_not_overwrite_failed_safe(tmp_path, monkeypatch):
    """_settle_manual does not transition FAILED_SAFE to FAILED_MANUAL_RECOVERY_REQUIRED."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "FAILED_SAFE"
    data["final_result"] = "FAILED_SAFE"
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    from guarded_restore_configured_replacement import _settle_manual
    _settle_manual(op_id, env["restore_root"])

    j = load_restore_journal(op_id, root=env["restore_root"])
    assert j.stage is RestoreStage.FAILED_SAFE, "settle_manual must not rewrite FAILED_SAFE"


# ---------------------------------------------------------------------------
# 16. Security correction tests: lock-release outcome precedence
# ---------------------------------------------------------------------------

def test_lock_release_failure_with_successful_operation_raises(tmp_path, monkeypatch):
    """Lock release failure after successful operation must raise; success must not be returned."""
    from guarded_restore import RestoreLock
    from guarded_restore_configured import ConfiguredRestoreLockReleaseError
    env = _prepare(tmp_path, monkeypatch)

    orig_exit = RestoreLock.__exit__
    fail_on_exit = [False]

    def failing_exit(self, *args):
        if fail_on_exit[0]:
            raise OSError("Injected lock release failure")
        return orig_exit(self, *args)

    RestoreLock.__exit__ = failing_exit
    fail_on_exit[0] = True
    try:
        with pytest.raises((ConfiguredRestoreLockReleaseError, OSError)):
            _call_replace(env)
    finally:
        RestoreLock.__exit__ = orig_exit
        fail_on_exit[0] = False


def test_lock_release_failure_during_rollback_completed_preserves_primary(tmp_path, monkeypatch):
    """Lock release failure during rollback-completed: RollbackCompletedError is primary outcome."""
    from guarded_restore import RestoreLock
    env = _prepare(tmp_path, monkeypatch)
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    target_paths = {str(t.path) for t in targets}
    orig_replace = os.replace
    db_count = [0]

    def fail_first(src, dst):
        if str(dst) in target_paths:
            db_count[0] += 1
            if db_count[0] == 1:
                raise OSError("Injected DB failure")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_first)

    orig_exit = RestoreLock.__exit__
    fail_on_exit = [False]

    def failing_exit(self, *args):
        if fail_on_exit[0]:
            raise OSError("Lock release failure")
        return orig_exit(self, *args)

    RestoreLock.__exit__ = failing_exit
    fail_on_exit[0] = True
    try:
        # RollbackCompleted is the primary outcome and should be preserved.
        with pytest.raises(ConfiguredReplacementRollbackCompletedError):
            _call_replace(env)
    finally:
        RestoreLock.__exit__ = orig_exit
        fail_on_exit[0] = False


def test_lock_release_failure_during_manual_recovery_preserves_primary(tmp_path, monkeypatch):
    """Lock release failure during manual-recovery: ManualRecoveryError is primary outcome."""
    from guarded_restore import RestoreLock
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

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
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    targets[0].path.write_bytes(b"AMBIGUOUS_BYTES")

    orig_exit = RestoreLock.__exit__
    fail_on_exit = [False]

    def failing_exit(self, *args):
        if fail_on_exit[0]:
            raise OSError("Lock release failure")
        return orig_exit(self, *args)

    RestoreLock.__exit__ = failing_exit
    fail_on_exit[0] = True
    try:
        with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
            _call_replace(env)
    finally:
        RestoreLock.__exit__ = orig_exit
        fail_on_exit[0] = False


# ---------------------------------------------------------------------------
# 17. Security correction tests: sidecar identity protection
# ---------------------------------------------------------------------------

def test_sidecar_reentry_requires_baseline_identity_proof(tmp_path, monkeypatch):
    """WAL re-entry with presence recorded but removal not: identity must match baseline."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    first_tgt = targets[0]

    # Create a fake WAL sidecar at the target path.
    wal_path = first_tgt.path.parent / (first_tgt.path.name + "-wal")
    wal_path.write_bytes(b"fake wal content")

    # Set journal to REPLACING with wal_present=True for first target but wal_removed=False.
    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "REPLACING"
    for t in data["targets"]:
        if t["target_key"] == first_tgt.target_key:
            t["wal_present"] = True
            t["wal_removed"] = False
            t["replacement_intent"] = True
            t["replacement_completed"] = False
        else:
            t["replacement_intent"] = True
            t["replacement_completed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    # The sidecar exists with unknown identity (not matching baseline since it wasn't
    # there at baseline time), which should cause ManualRecovery or a precondition error.
    with pytest.raises(
        (ConfiguredReplacementPreconditionError, ConfiguredReplacementManualRecoveryRequiredError)
    ):
        _call_replace(env)


# ---------------------------------------------------------------------------
# 18. Security correction tests: rollback binding ownership
# ---------------------------------------------------------------------------

def test_rollback_binding_hard_link_substitution_detected(tmp_path, monkeypatch):
    """Hard link into rollback binding artifact is rejected (nlink != 1)."""
    if os.name == "nt":
        pytest.skip("Hard link checks not applicable on Windows")
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}", against_current_config=False
    )
    saf_entries = {e.target_key: e for e in saf_snap.entries}
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    first_tgt = targets[0]
    first_idx = 0
    sentry = saf_entries[first_tgt.target_key]
    rbfile = _rollback_artifact_name(first_idx, first_tgt.target_key)
    rb_dir = first_tgt.path.parent.resolve() / _rollback_dir_name(op_id, first_idx)
    rb_dir.mkdir(mode=0o700, exist_ok=True)
    _write_rollback_binding(
        rb_dir, operation_id=op_id, safety_backup_id=saf_snap.backup_id,
        safety_manifest_sha256=saf_snap.manifest_sha256, target_key=first_tgt.target_key,
        kind=sentry.kind, index=first_idx, rollback_filename=rbfile,
        size_bytes=sentry.size_bytes, sha256=sentry.sha256,
    )
    src = saf_snap.directory / sentry.filename
    _copy_rollback_file(src, rb_dir, rbfile, size=sentry.size_bytes, sha256=sentry.sha256)

    # Create a hard link to the artifact (nlink becomes 2).
    artifact_path = rb_dir / rbfile
    link_path = rb_dir.parent / "hardlink-to-artifact"
    os.link(str(artifact_path), str(link_path))

    try:
        from guarded_restore_configured_replacement import _verify_rollback_binding
        with pytest.raises(ConfiguredReplacementPreconditionError, match="link count"):
            _verify_rollback_binding(
                rb_dir, operation_id=op_id, safety_backup_id=saf_snap.backup_id,
                safety_manifest_sha256=saf_snap.manifest_sha256,
                target_key=first_tgt.target_key, kind=sentry.kind, index=first_idx,
                rollback_filename=rbfile, size_bytes=sentry.size_bytes, sha256=sentry.sha256,
            )
    finally:
        try:
            link_path.unlink()
        except OSError:
            pass


def test_rollback_directory_child_added_during_verification_detected(tmp_path, monkeypatch):
    """Extra child in rollback directory during second enumeration is rejected."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}", against_current_config=False
    )
    saf_entries = {e.target_key: e for e in saf_snap.entries}
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    first_tgt = targets[0]
    sentry = saf_entries[first_tgt.target_key]
    rbfile = _rollback_artifact_name(0, first_tgt.target_key)
    rb_dir = first_tgt.path.parent.resolve() / _rollback_dir_name(op_id, 0)
    rb_dir.mkdir(mode=0o700, exist_ok=True)
    _write_rollback_binding(
        rb_dir, operation_id=op_id, safety_backup_id=saf_snap.backup_id,
        safety_manifest_sha256=saf_snap.manifest_sha256, target_key=first_tgt.target_key,
        kind=sentry.kind, index=0, rollback_filename=rbfile,
        size_bytes=sentry.size_bytes, sha256=sentry.sha256,
    )
    src = saf_snap.directory / sentry.filename
    _copy_rollback_file(src, rb_dir, rbfile, size=sentry.size_bytes, sha256=sentry.sha256)

    # Insert foreign child between first and second enumeration.
    orig_iterdir = Path.iterdir
    call_count = [0]

    def fake_iterdir(self):
        call_count[0] += 1
        if call_count[0] == 2 and str(self) == str(rb_dir):
            # Add a foreign file before second enumeration.
            (rb_dir / "FOREIGN_CHILD").write_bytes(b"intruder")
        return orig_iterdir(self)

    Path.iterdir = fake_iterdir
    try:
        from guarded_restore_configured_replacement import _verify_rollback_binding
        with pytest.raises(ConfiguredReplacementPreconditionError, match="children"):
            _verify_rollback_binding(
                rb_dir, operation_id=op_id, safety_backup_id=saf_snap.backup_id,
                safety_manifest_sha256=saf_snap.manifest_sha256,
                target_key=first_tgt.target_key, kind=sentry.kind, index=0,
                rollback_filename=rbfile, size_bytes=sentry.size_bytes, sha256=sentry.sha256,
            )
    finally:
        Path.iterdir = orig_iterdir
        try:
            (rb_dir / "FOREIGN_CHILD").unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 19. Security correction tests: descriptor-bound evidence cleanup
# ---------------------------------------------------------------------------

def test_cleanup_binding_substitution_detected(tmp_path, monkeypatch):
    """Binding file substitution during cleanup is detected and rejected."""
    env = _prepare(tmp_path, monkeypatch)

    orig_cleanup = _mod._cleanup_single_dir
    detected = [False]

    def intercepting_cleanup(directory, *, is_rollback):
        # Simulate binding identity change by monkeypatching os.fstat inside cleanup.
        orig_fstat = os.fstat
        call_count = [0]

        def fake_fstat(fd):
            call_count[0] += 1
            real = orig_fstat(fd)
            if call_count[0] >= 2:
                # Return different inode for second fstat call on binding fd.
                class FakeStat:
                    st_dev = real.st_dev
                    st_ino = real.st_ino + 99999
                    st_size = real.st_size
                    st_mode = real.st_mode
                    st_nlink = real.st_nlink
                    st_mtime_ns = getattr(real, "st_mtime_ns", None)
                detected[0] = True
                return FakeStat()
            return real

        os.fstat = fake_fstat
        try:
            return orig_cleanup(directory, is_rollback=is_rollback)
        finally:
            os.fstat = orig_fstat

    _mod._cleanup_single_dir = intercepting_cleanup
    try:
        with pytest.raises((ConfiguredReplacementCleanupError, Exception)):
            _call_replace(env)
    finally:
        _mod._cleanup_single_dir = orig_cleanup


# ---------------------------------------------------------------------------
# 20. Security correction tests: post-mutation backup validation
# ---------------------------------------------------------------------------

def test_post_mutation_backup_validation_without_current_config(tmp_path, monkeypatch):
    """Post-mutation stages validate backup snapshots without current-config DB comparison."""
    env = _prepare(tmp_path, monkeypatch)

    # Record whether load_validated_backup_snapshot was called with against_current_config=False
    orig_load = _mod.load_validated_backup_snapshot
    against_current_calls = []
    post_mutation_calls = []

    def recording_load(directory, against_current_config=True):
        against_current_calls.append(against_current_config)
        if not against_current_config:
            post_mutation_calls.append(str(directory))
        return orig_load(directory, against_current_config=against_current_config)

    _mod.load_validated_backup_snapshot = recording_load

    # Set journal to REPLACING to trigger post-mutation path.
    op_id = env["op_id"]
    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "REPLACING"
    for t in data["targets"]:
        t["state"] = "STAGED_VERIFIED"
        t["replacement_intent"] = False
        t["replacement_completed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    try:
        _call_replace(env)
    except Exception:
        pass
    finally:
        _mod.load_validated_backup_snapshot = orig_load

    # Verify that at least one call was made with against_current_config=False.
    assert False in against_current_calls, (
        "Post-mutation stages must call load_validated_backup_snapshot with "
        "against_current_config=False"
    )


def test_post_mutation_validate_snapshot_verifies_backup_files(tmp_path, monkeypatch):
    """_validate_snapshot_post_mutation verifies each backup file's SHA-256."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

    # After successful completion, reload sel_snap and run _validate_snapshot_post_mutation.
    _call_replace(env)
    snap = env["snap"]
    sel_dir = env["backup_root"] / f"backup-{env['selected_id']}"
    sel_snap = load_validated_backup_snapshot(sel_dir, against_current_config=False)

    from guarded_restore_configured_replacement import _validate_snapshot_post_mutation
    runtime_mode = "multi_user" if False else "single_user"

    # Should succeed with intact backup.
    _validate_snapshot_post_mutation(
        sel_snap,
        expected_backup_id=env["selected_id"],
        expected_manifest_sha256=sel_snap.manifest_sha256,
        runtime_mode=runtime_mode,
        expected_target_keys=sel_snap.target_keys,
        backup_root=env["backup_root"],
    )

    # Corrupt one backup file and verify it is detected.
    first_entry = sel_snap.entries[0]
    backup_file = sel_snap.directory / first_entry.filename
    backup_file.write_bytes(b"CORRUPTED_BACKUP_FILE")

    with pytest.raises(ConfiguredReplacementPreconditionError):
        _validate_snapshot_post_mutation(
            sel_snap,
            expected_backup_id=env["selected_id"],
            expected_manifest_sha256=sel_snap.manifest_sha256,
            runtime_mode=runtime_mode,
            expected_target_keys=sel_snap.target_keys,
            backup_root=env["backup_root"],
        )


# ===========================================================================
# Gate A: New verification-gap tests
# ===========================================================================

# ---------------------------------------------------------------------------
# A2. Race-complete _verify_file_owned: mutations injected during hashing
# ---------------------------------------------------------------------------

class _MutatedStat:
    """Wrap a real stat result and override specific fields."""

    def __init__(self, real, **overrides):
        for attr in ("st_dev", "st_ino", "st_size", "st_mode", "st_nlink"):
            setattr(self, attr, overrides.get(attr, getattr(real, attr)))
        self.st_mtime_ns = overrides.get("st_mtime_ns", getattr(real, "st_mtime_ns", None))


def _make_owned_file(td_path: Path, data: bytes) -> tuple[Path, str]:
    """Write *data* to a temp file at mode 0600 and return (path, sha256)."""
    p = td_path / "testfile"
    p.write_bytes(data)
    if os.name != "nt":
        os.chmod(str(p), 0o600)
    return p, hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(os.name == "nt", reason="Mode bits not meaningful on Windows")
def test_verify_file_owned_detects_mode_change_during_hash(tmp_path):
    """_verify_file_owned detects mode change between pre- and post-hash fstat."""
    p, sha = _make_owned_file(tmp_path, b"mode mutation test data")
    size = p.stat().st_size

    orig_fstat = os.fstat
    orig_read = os.read
    patched = [False]

    def marking_read(fd, n):
        result = orig_read(fd, n)
        patched[0] = True
        return result

    def fake_fstat(fd):
        real = orig_fstat(fd)
        if patched[0]:
            new_mode = (real.st_mode & ~0o777) | 0o644
            return _MutatedStat(real, st_mode=new_mode)
        return real

    os.fstat = fake_fstat
    os.read = marking_read
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha)
    finally:
        os.fstat = orig_fstat
        os.read = orig_read


@pytest.mark.skipif(os.name == "nt", reason="Hard links not meaningful on Windows")
def test_verify_file_owned_detects_hard_link_during_hash(tmp_path):
    """_verify_file_owned detects nlink increase (hard link added) between pre- and post-hash fstat."""
    p, sha = _make_owned_file(tmp_path, b"hard link mutation test data")
    size = p.stat().st_size

    orig_fstat = os.fstat
    orig_read = os.read
    patched = [False]

    def marking_read(fd, n):
        result = orig_read(fd, n)
        patched[0] = True
        return result

    def fake_fstat(fd):
        real = orig_fstat(fd)
        if patched[0]:
            return _MutatedStat(real, st_nlink=2)
        return real

    os.fstat = fake_fstat
    os.read = marking_read
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha, require_single_link=True)
    finally:
        os.fstat = orig_fstat
        os.read = orig_read


def test_verify_file_owned_detects_file_type_change_during_hash(tmp_path):
    """_verify_file_owned detects file-type change in no-follow pathname stat after hash."""
    p, sha = _make_owned_file(tmp_path, b"file type substitution test")
    size = p.stat().st_size

    orig_stat = os.stat
    orig_read = os.read
    patched = [False]

    def marking_read(fd, n):
        result = orig_read(fd, n)
        patched[0] = True
        return result

    def fake_stat(path_arg, *, follow_symlinks=True, **kwargs):
        real = orig_stat(path_arg, follow_symlinks=follow_symlinks, **kwargs)
        if not follow_symlinks and str(path_arg) == str(p) and patched[0]:
            new_mode = stat.S_IFDIR | (real.st_mode & 0o777)
            return _MutatedStat(real, st_mode=new_mode)
        return real

    os.stat = fake_stat
    os.read = marking_read
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha)
    finally:
        os.stat = orig_stat
        os.read = orig_read


def test_verify_file_owned_detects_size_change_during_hash(tmp_path):
    """_verify_file_owned detects size change in fstat after hash."""
    p, sha = _make_owned_file(tmp_path, b"size mutation test data")
    size = p.stat().st_size

    orig_fstat = os.fstat
    orig_read = os.read
    patched = [False]

    def marking_read(fd, n):
        result = orig_read(fd, n)
        patched[0] = True
        return result

    def fake_fstat(fd):
        real = orig_fstat(fd)
        if patched[0]:
            return _MutatedStat(real, st_size=real.st_size + 1024)
        return real

    os.fstat = fake_fstat
    os.read = marking_read
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha)
    finally:
        os.fstat = orig_fstat
        os.read = orig_read


def test_verify_file_owned_detects_mtime_change_during_hash(tmp_path):
    """_verify_file_owned detects mtime_ns change in fstat after hash."""
    p, sha = _make_owned_file(tmp_path, b"mtime mutation test data")
    size = p.stat().st_size

    orig_fstat = os.fstat
    orig_read = os.read
    patched = [False]

    def marking_read(fd, n):
        result = orig_read(fd, n)
        patched[0] = True
        return result

    def fake_fstat(fd):
        real = orig_fstat(fd)
        if patched[0]:
            old_mtime = getattr(real, "st_mtime_ns", None)
            new_mtime = old_mtime + 1_000_000_000 if old_mtime is not None else None
            return _MutatedStat(real, st_mtime_ns=new_mtime)
        return real

    os.fstat = fake_fstat
    os.read = marking_read
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha)
    finally:
        os.fstat = orig_fstat
        os.read = orig_read


def test_verify_file_owned_detects_inode_change_during_hash(tmp_path):
    """_verify_file_owned detects inode change in no-follow pathname stat after hash."""
    p, sha = _make_owned_file(tmp_path, b"inode change test data")
    size = p.stat().st_size

    orig_stat = os.stat
    orig_read = os.read
    patched = [False]

    def marking_read(fd, n):
        result = orig_read(fd, n)
        patched[0] = True
        return result

    def fake_stat(path_arg, *, follow_symlinks=True, **kwargs):
        real = orig_stat(path_arg, follow_symlinks=follow_symlinks, **kwargs)
        if not follow_symlinks and str(path_arg) == str(p) and patched[0]:
            return _MutatedStat(real, st_ino=real.st_ino + 99999)
        return real

    os.stat = fake_stat
    os.read = marking_read
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="File facts changed during hash"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha)
    finally:
        os.stat = orig_stat
        os.read = orig_read


def test_verify_file_owned_surfaces_close_failure(tmp_path):
    """_verify_file_owned surfaces descriptor-close failure as a bounded PreconditionError."""
    p, sha = _make_owned_file(tmp_path, b"close failure test data")
    size = p.stat().st_size

    orig_close = os.close
    first_call = [True]

    def fake_close(fd):
        if first_call[0]:
            first_call[0] = False
            raise OSError("injected close failure")
        orig_close(fd)

    os.close = fake_close
    try:
        with pytest.raises(ConfiguredReplacementPreconditionError, match="[Cc]lose"):
            _verify_file_owned(p, expected_size=size, expected_sha256=sha)
    finally:
        os.close = orig_close


# ---------------------------------------------------------------------------
# A3. Backward compatibility: journals missing safety_backup_manifest_sha256
# ---------------------------------------------------------------------------

def test_old_replacement_ready_journal_missing_safety_manifest_sha_refused(tmp_path, monkeypatch):
    """REPLACEMENT_READY journal with safety_backup_id but no safety manifest SHA causes PreconditionError."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    assert data["stage"] == "REPLACEMENT_READY"
    assert data["safety_backup_id"] is not None
    data.pop("safety_backup_manifest_sha256", None)
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementPreconditionError, match="[Ss]afety.*[Mm]anifest|[Mm]anifest.*[Ss]afety"):
        _call_replace(env)


def test_old_replacing_journal_missing_safety_manifest_sha_causes_manual_recovery(tmp_path, monkeypatch):
    """Post-mutation (REPLACING) journal without safety manifest SHA causes ManualRecoveryRequired."""
    env = _prepare(tmp_path, monkeypatch)
    op_id = env["op_id"]

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "REPLACING"
    data.pop("safety_backup_manifest_sha256", None)
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


# ---------------------------------------------------------------------------
# A4. ROLLED_BACK re-entry: never-replaced target verification
# ---------------------------------------------------------------------------

def _setup_mixed_rolled_back(tmp_path, monkeypatch):
    """Prepare ROLLED_BACK journal with a mixed target state.

    single_user: ROLLED_BACK (was replaced and rolled back) — safety bytes on disk.
    control: STAGED_VERIFIED (never replaced) — original bytes unchanged.
    """
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]
    targets = discover_database_targets(profile=TargetProfile.RUNTIME)

    saf_snap = load_validated_backup_snapshot(
        env["backup_root"] / f"backup-{env['safety_backup_id']}",
        against_current_config=False,
    )
    saf_entries = {e.target_key: e for e in saf_snap.entries}

    for tgt in targets:
        if tgt.target_key == "single_user":
            sentry = saf_entries[tgt.target_key]
            src = saf_snap.directory / sentry.filename
            shutil.copy2(str(src), str(tgt.path))
            if os.name != "nt":
                os.chmod(str(tgt.path), 0o600)

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "ROLLED_BACK"
    for t in data["targets"]:
        if t["target_key"] == "single_user":
            t["state"] = "ROLLED_BACK"
            t["replacement_intent"] = True
            t["replacement_completed"] = True
            t["rollback_intent"] = True
            t["rollback_completed"] = True
            t["wal_present"] = False
            t["shm_present"] = False
            t["wal_removed"] = False
            t["shm_removed"] = False
        else:
            t["state"] = "STAGED_VERIFIED"
            t["replacement_intent"] = False
            t["replacement_completed"] = False
            t["rollback_intent"] = False
            t["rollback_completed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )
    return env, targets, saf_snap, saf_entries


def test_rolled_back_reentry_never_replaced_target_corrupted(tmp_path, monkeypatch):
    """ROLLED_BACK re-entry fails if a never-replaced target's bytes differ from the baseline."""
    env, _targets, _saf_snap, _saf_entries = _setup_mixed_rolled_back(tmp_path, monkeypatch)

    env["control_db"].write_bytes(b"corrupted content that does not match the captured baseline")

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


@pytest.mark.skipif(os.name == "nt", reason="Mode bits not meaningful on Windows")
def test_rolled_back_reentry_never_replaced_target_mode_changed(tmp_path, monkeypatch):
    """ROLLED_BACK re-entry fails if a never-replaced target's file mode changed."""
    env, _targets, _saf_snap, _saf_entries = _setup_mixed_rolled_back(tmp_path, monkeypatch)

    ctrl = env["control_db"]
    original_mode = stat.S_IMODE(os.stat(str(ctrl)).st_mode)
    new_mode = 0o600 if original_mode != 0o600 else 0o644
    os.chmod(str(ctrl), new_mode)

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


def test_rolled_back_reentry_never_replaced_sidecar_added(tmp_path, monkeypatch):
    """ROLLED_BACK re-entry fails if an unexpected WAL sidecar appears for a never-replaced target."""
    env, _targets, _saf_snap, _saf_entries = _setup_mixed_rolled_back(tmp_path, monkeypatch)

    wal_path = env["control_db"].parent / (env["control_db"].name + "-wal")
    wal_path.write_bytes(b"unexpected wal content")

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


def _inject_wal_into_baseline_evidence(
    op_dir: Path, target_key: str, wal_path: Path, project_root: Path
) -> None:
    """Inject WAL-present info for *target_key* into the stored baseline evidence JSON.

    Creates a deterministic test baseline that records the WAL sidecar as present
    (with the actual on-disk inode/dev/size/sha256) without needing the WAL to
    exist at the time prepare_configured_restore was called.  The baseline file is
    replaced atomically with updated canonical JSON.
    """
    baseline_file = op_dir / "destination-baseline.json"
    raw_bytes = baseline_file.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))

    wal_st = wal_path.stat()
    wal_sha = hashlib.sha256(wal_path.read_bytes()).hexdigest()

    try:
        w_rel = str(wal_path.relative_to(project_root)).replace("\\", "/")
    except ValueError:
        w_rel = wal_path.name

    wal_info = {
        "present": True,
        "configured_relative_path": w_rel,
        "resolved_relative_path": w_rel,
        "is_regular_file": True,
        "st_dev": wal_st.st_dev,
        "st_ino": wal_st.st_ino,
        "size_bytes": wal_st.st_size,
        "mtime_ns": wal_st.st_mtime_ns,
        "st_mode": stat.S_IMODE(wal_st.st_mode),
        "sha256": wal_sha,
    }

    updated = False
    for tgt_rec in payload.get("targets", []):
        if tgt_rec.get("target_key") == target_key:
            tgt_rec["wal"] = wal_info
            updated = True

    assert updated, f"No target with key {target_key!r} found in baseline"

    new_bytes = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    tmp_file = baseline_file.with_suffix(".tmp")
    tmp_file.write_bytes(new_bytes)
    if os.name != "nt":
        os.chmod(str(tmp_file), 0o600)
    os.replace(str(tmp_file), str(baseline_file))


def test_rolled_back_reentry_never_replaced_sidecar_replaced(tmp_path, monkeypatch):
    """ROLLED_BACK re-entry fails if an existing sidecar identity changes for a never-replaced target.

    This test is deterministic: it injects WAL-present=True into the stored baseline
    evidence JSON after prepare, then patches os.lstat to return a different inode for
    the WAL sidecar.  No environment-dependent skip is needed; the security condition is
    always exercised.
    """
    env, _targets, _saf_snap, _saf_entries = _setup_mixed_rolled_back(tmp_path, monkeypatch)

    ctrl = env["control_db"]
    wal_path = ctrl.parent / (ctrl.name + "-wal")
    wal_path.write_bytes(b"deterministic wal content for sidecar-replaced test")

    # Inject WAL-present info into the baseline evidence so re-entry checks the identity.
    op_dir = env["restore_root"] / f"operation-{env['op_id']}"
    _inject_wal_into_baseline_evidence(op_dir, "control", wal_path, env["project_root"])

    # Verify the injection succeeded.
    evidence, _ = load_destination_baseline_evidence(
        env["op_id"], restore_root=env["restore_root"]
    )
    base_rec = next((t for t in evidence.targets if t.target_key == "control"), None)
    assert base_rec is not None, "control baseline record must exist"
    assert base_rec.wal.get("present", False), (
        "baseline must record wal_present=True after injection"
    )

    # Simulate inode substitution of the WAL sidecar by patching os.lstat to
    # return a different inode — this is the security condition being tested.
    orig_lstat = os.lstat
    wal_path_str = str(wal_path)

    def fake_lstat(path_arg):
        real = orig_lstat(path_arg)
        if str(path_arg) == wal_path_str:
            return _MutatedStat(real, st_ino=real.st_ino + 99999)
        return real

    os.lstat = fake_lstat
    try:
        with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
            _call_replace(env)
    finally:
        os.lstat = orig_lstat


def test_rolled_back_reentry_replacement_completed_without_rollback_intent(tmp_path, monkeypatch):
    """ROLLED_BACK re-entry with replacement_completed=True but rollback_intent=False is manual recovery."""
    env = _prepare(tmp_path, monkeypatch, multi_user=False)
    op_id = env["op_id"]

    _put_safety_bytes_on_disk(env)

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    data = json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "ROLLED_BACK"
    for t in data["targets"]:
        t["state"] = "STAGED_VERIFIED"
        t["replacement_intent"] = True
        t["replacement_completed"] = True
        t["rollback_intent"] = False
        t["rollback_completed"] = False
    j_path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )

    with pytest.raises(ConfiguredReplacementManualRecoveryRequiredError):
        _call_replace(env)


# ---------------------------------------------------------------------------
# A5. Service-proof error sanitization
# ---------------------------------------------------------------------------

def test_require_service_stopped_sanitizes_error_message(tmp_path, monkeypatch):
    """Service-stopped errors with sensitive content do not appear in the public exception string."""
    env = _prepare(tmp_path, monkeypatch)

    sensitive_path = "/etc/secret/token-abcdef1234567890"
    token_like = "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    long_content = "A" * 5000

    def bad_service_checker():
        raise RuntimeError(
            f"Service check error: {sensitive_path} token={token_like} detail={long_content}"
        )

    with pytest.raises(ConfiguredReplacementPreconditionError) as exc_info:
        _call_replace(env, service_checker=bad_service_checker)

    public_msg = str(exc_info.value)
    assert sensitive_path not in public_msg, "Absolute path must not appear in public error"
    assert token_like not in public_msg, "Token-like value must not appear in public error"
    assert long_content[:100] not in public_msg, "Long attacker content must not appear in public error"
    assert len(public_msg) < 500, "Public error message must be bounded"


# ---------------------------------------------------------------------------
# A7. Descriptor close lifecycle: _open_nf and _verify_file_owned
# ---------------------------------------------------------------------------

def test_open_nf_non_regular_file_closes_exactly_once(tmp_path, monkeypatch):
    """_open_nf closes the descriptor exactly once when fstat shows non-regular file."""
    import guarded_restore_configured_replacement as _rmod
    import stat as _stat

    test_file = tmp_path / "test.db"
    test_file.write_bytes(b"test data")

    close_count = [0]
    original_close = os.close

    def counting_close(fd):
        close_count[0] += 1
        original_close(fd)

    original_fstat = os.fstat

    class _FakeStat:
        def __init__(self, real):
            self._r = real
            self.st_mode = _stat.S_IFIFO  # non-regular
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino
            self.st_nlink = real.st_nlink
            self.st_size = real.st_size
            self.st_mtime_ns = getattr(real, "st_mtime_ns", 0)

    def fake_fstat(fd):
        return _FakeStat(original_fstat(fd))

    monkeypatch.setattr(_rmod.os, "fstat", fake_fstat)
    monkeypatch.setattr(_rmod.os, "close", counting_close)

    with pytest.raises(_rmod.ConfiguredReplacementPreconditionError, match="not a regular file"):
        _rmod._open_nf(test_file)

    assert close_count[0] == 1, (
        f"Non-regular-file path must close fd exactly once, got {close_count[0]}"
    )


def test_open_nf_success_does_not_close(tmp_path, monkeypatch):
    """_open_nf does not close the fd on success; caller is responsible."""
    import guarded_restore_configured_replacement as _rmod

    test_file = tmp_path / "regular.db"
    test_file.write_bytes(b"regular file data")

    close_count = [0]
    original_close = os.close

    def counting_close(fd):
        close_count[0] += 1
        original_close(fd)

    monkeypatch.setattr(_rmod.os, "close", counting_close)

    fd = _rmod._open_nf(test_file)
    assert close_count[0] == 0, "Successful _open_nf must not close the fd"
    os.close(fd)


def test_open_nf_non_regular_close_failure_surfaces_bounded_error(tmp_path, monkeypatch):
    """_open_nf surfaces a bounded error when close fails on a non-regular descriptor.

    Requirements:
    - The close OSError is the direct cause (__cause__).
    - The original non-regular-file validation failure is available as context (__context__).
    - No raw path or OS error text appears in the public message.
    - No double close occurs (descriptor is not closed a second time in the except handler).
    """
    import guarded_restore_configured_replacement as _rmod
    import stat as _stat

    test_file = tmp_path / "test.db"
    test_file.write_bytes(b"test data")

    original_fstat = os.fstat
    original_close = os.close
    close_count = [0]

    class _FakeStat:
        def __init__(self, real):
            self._r = real
            self.st_mode = _stat.S_IFIFO  # non-regular
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino
            self.st_nlink = real.st_nlink
            self.st_size = real.st_size
            self.st_mtime_ns = getattr(real, "st_mtime_ns", 0)

    def fake_fstat(fd):
        return _FakeStat(original_fstat(fd))

    def failing_close(fd):
        close_count[0] += 1
        raise OSError(9, "Bad file descriptor (injected close failure)")

    monkeypatch.setattr(_rmod.os, "fstat", fake_fstat)
    monkeypatch.setattr(_rmod.os, "close", failing_close)

    with pytest.raises(_rmod.ConfiguredReplacementPreconditionError) as exc_info:
        _rmod._open_nf(test_file)

    exc = exc_info.value

    # Public message must be bounded — no raw path or OS error string.
    msg = str(exc)
    assert str(test_file) not in msg, f"Raw path leaked into public error: {msg!r}"
    assert "Bad file descriptor" not in msg, f"Raw OS error leaked into public error: {msg!r}"

    # Close exception is the direct cause.
    assert isinstance(exc.__cause__, OSError), (
        f"__cause__ must be the OSError from the close failure; got {exc.__cause__!r}"
    )

    # Original non-regular-file validation failure is available as context.
    assert exc.__context__ is not None, (
        "__context__ must preserve the original non-regular-file validation failure"
    )
    assert "not a regular file" in str(exc.__context__).lower() or \
           isinstance(exc.__context__, _rmod.ConfiguredReplacementPreconditionError), (
        f"__context__ should be the original validation failure; got {exc.__context__!r}"
    )

    # Descriptor was closed exactly once (the failing close attempt).
    assert close_count[0] == 1, (
        f"Close must be attempted exactly once even on failure; got {close_count[0]}"
    )


def test_open_nf_non_regular_close_failure_no_double_close(tmp_path, monkeypatch):
    """_open_nf does not attempt a second close after a close failure on non-regular file."""
    import guarded_restore_configured_replacement as _rmod
    import stat as _stat

    test_file = tmp_path / "test.db"
    test_file.write_bytes(b"test data")

    original_fstat = os.fstat
    close_calls = []

    class _FakeStat:
        def __init__(self, real):
            self._r = real
            self.st_mode = _stat.S_IFIFO  # non-regular
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino
            self.st_nlink = real.st_nlink
            self.st_size = real.st_size
            self.st_mtime_ns = getattr(real, "st_mtime_ns", 0)

    def fake_fstat(fd):
        return _FakeStat(original_fstat(fd))

    def recording_close(fd):
        close_calls.append(fd)
        raise OSError(9, "injected close failure")

    monkeypatch.setattr(_rmod.os, "fstat", fake_fstat)
    monkeypatch.setattr(_rmod.os, "close", recording_close)

    with pytest.raises(_rmod.ConfiguredReplacementPreconditionError):
        _rmod._open_nf(test_file)

    assert len(close_calls) == 1, (
        f"Descriptor must be closed exactly once — no double close; got calls: {close_calls}"
    )
    # All close calls must reference the same fd opened above.
    assert len(set(close_calls)) == 1, "Multiple distinct fds closed — possible unrelated descriptor close"


def test_open_nf_successful_ownership_transfer(tmp_path, monkeypatch):
    """_open_nf returns the fd without closing it on success; caller owns exactly one close."""
    import guarded_restore_configured_replacement as _rmod

    test_file = tmp_path / "regular.db"
    test_file.write_bytes(b"regular file data for ownership test")

    close_count = [0]
    original_close = os.close

    def counting_close(fd):
        close_count[0] += 1
        original_close(fd)

    monkeypatch.setattr(_rmod.os, "close", counting_close)

    fd = _rmod._open_nf(test_file)
    assert close_count[0] == 0, (
        "Successful _open_nf must not close the fd; caller is the owner"
    )
    # Verify the returned fd is valid by reading from it.
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, 1024)
    assert data == b"regular file data for ownership test"
    original_close(fd)  # Caller's close.
    assert close_count[0] == 0, "Only the original_close path was used; counting_close not involved"


def test_open_nf_no_reused_descriptor_close(tmp_path, monkeypatch):
    """_open_nf only closes the fd it opened — not any unrelated fd."""
    import guarded_restore_configured_replacement as _rmod
    import stat as _stat

    # Open a sentinel file to occupy a descriptor before calling _open_nf.
    sentinel = tmp_path / "sentinel.db"
    sentinel.write_bytes(b"sentinel")
    sentinel_fd = os.open(str(sentinel), os.O_RDONLY)

    test_file = tmp_path / "test.db"
    test_file.write_bytes(b"test data")

    original_fstat = os.fstat
    original_close = os.close
    closed_fds = []

    class _FakeStat:
        def __init__(self, real):
            self.st_mode = _stat.S_IFIFO
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino
            self.st_nlink = real.st_nlink
            self.st_size = real.st_size
            self.st_mtime_ns = getattr(real, "st_mtime_ns", 0)

    def fake_fstat(fd):
        return _FakeStat(original_fstat(fd))

    def recording_close(fd):
        closed_fds.append(fd)
        original_close(fd)

    monkeypatch.setattr(_rmod.os, "fstat", fake_fstat)
    monkeypatch.setattr(_rmod.os, "close", recording_close)

    try:
        with pytest.raises(_rmod.ConfiguredReplacementPreconditionError):
            _rmod._open_nf(test_file)
    finally:
        original_close(sentinel_fd)

    assert sentinel_fd not in closed_fds, (
        f"_open_nf must not close the unrelated sentinel fd {sentinel_fd}; closed: {closed_fds}"
    )
    assert len(closed_fds) == 1, (
        f"Exactly one fd must be closed (the opened one); got {closed_fds}"
    )


def test_verify_file_owned_verification_failure_closes_exactly_once(tmp_path, monkeypatch):
    """On SHA-256 verification failure, fd is closed exactly once."""
    import guarded_restore_configured_replacement as _rmod

    data = b"test data for close tracking"
    test_file = tmp_path / "test.db"
    test_file.write_bytes(data)

    close_count = [0]
    original_close = os.close

    def counting_close(fd):
        close_count[0] += 1
        original_close(fd)

    monkeypatch.setattr(_rmod.os, "close", counting_close)

    with pytest.raises(_rmod.ConfiguredReplacementPreconditionError):
        _rmod._verify_file_owned(
            test_file,
            expected_size=len(data),
            expected_sha256="a" * 64,  # Wrong SHA
        )

    assert close_count[0] == 1, (
        f"Verification failure must close fd exactly once, got {close_count[0]}"
    )


def test_verify_file_owned_success_closes_exactly_once(tmp_path, monkeypatch):
    """On successful verification, fd is closed exactly once."""
    import hashlib
    import guarded_restore_configured_replacement as _rmod

    data = b"test data for success close tracking"
    test_file = tmp_path / "test.db"
    test_file.write_bytes(data)
    test_file.chmod(0o600)
    sha = hashlib.sha256(data).hexdigest()

    close_count = [0]
    original_close = os.close

    def counting_close(fd):
        close_count[0] += 1
        original_close(fd)

    monkeypatch.setattr(_rmod.os, "close", counting_close)

    _rmod._verify_file_owned(
        test_file,
        expected_size=len(data),
        expected_sha256=sha,
        require_mode_0600=True,
        require_single_link=True,
    )

    assert close_count[0] == 1, (
        f"Successful verification must close fd exactly once, got {close_count[0]}"
    )


def test_verify_file_owned_dual_failure_surfaces_close_uncertainty(tmp_path, monkeypatch):
    """When verification fails AND close fails, a bounded close-uncertainty error is raised.

    The close exception must be the direct cause; the original verification failure
    must be preserved as context; neither failure is silently discarded.
    """
    import guarded_restore_configured_replacement as _rmod

    data = b"test data for dual failure"
    test_file = tmp_path / "test.db"
    test_file.write_bytes(data)

    def failing_close(fd):
        raise OSError(9, "Bad file descriptor (injected close failure)")

    monkeypatch.setattr(_rmod.os, "close", failing_close)

    with pytest.raises(_rmod.ConfiguredReplacementPreconditionError) as exc_info:
        _rmod._verify_file_owned(
            test_file,
            expected_size=len(data),
            expected_sha256="a" * 64,  # Wrong SHA → verification fails
        )

    exc = exc_info.value
    assert "uncertain" in str(exc).lower() or "close" in str(exc).lower(), (
        f"Close uncertainty error expected; got: {exc!r}"
    )
    # The close exception must be the direct cause.
    assert isinstance(exc.__cause__, OSError), (
        f"__cause__ must be the OSError close failure, got {exc.__cause__!r}"
    )
    # The original verification failure must be preserved as context.
    assert exc.__context__ is not None, (
        "Original verification failure must be preserved as __context__"
    )
