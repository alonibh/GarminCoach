"""Tests for apply_verified_restore.py (Phase 6B3B3 apply CLI)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import config
import database_reset
from apply_verified_restore import (
    APPLY_EXIT_CLEANUP_REQUIRED,
    APPLY_EXIT_FAILED_SAFE,
    APPLY_EXIT_INVALID_ARGUMENTS,
    APPLY_EXIT_JOURNAL_UNCERTAINTY,
    APPLY_EXIT_LOCK_UNCERTAINTY,
    APPLY_EXIT_MANUAL_RECOVERY_REQUIRED,
    APPLY_EXIT_PRECONDITION_FAILED,
    APPLY_EXIT_ROLLBACK_COMPLETED,
    APPLY_EXIT_SUCCESS,
    APPLY_EXIT_UNEXPECTED_FAILURE,
    main,
    _validate_backup_id,
    _validate_operation_id,
    _validate_hex64,
    _validate_commit,
)
from guarded_restore import (
    RestoreStage,
    TargetRestoreState,
    confirmation_value,
    load_restore_journal,
    target_set_hash,
)
from guarded_restore_configured import prepare_configured_restore
from guarded_restore_configured_replacement import (
    ConfiguredReplacementManualRecoveryRequiredError,
    ConfiguredReplacementPreconditionError,
    ConfiguredReplacementRollbackCompletedError,
    replace_and_verify_configured_restore,
)
from operator_storage import TargetProfile, discover_database_targets
from verified_backup import create_verified_backup, load_validated_backup_snapshot


# ---------------------------------------------------------------------------
# Canary: prove tests never touch real configured paths
# ---------------------------------------------------------------------------

import config as _rc

_CANARY_CTRL = Path(_rc.CONTROL_DB_PATH)
_CANARY_DB = Path(_rc.DB_PATH)


@pytest.fixture(autouse=True)
def _real_db_canary():
    def _snap(p: Path):
        return (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None

    snap_ctrl = _snap(_CANARY_CTRL)
    snap_db = _snap(_CANARY_DB)
    yield
    if snap_ctrl is not None and _CANARY_CTRL.exists():
        assert _snap(_CANARY_CTRL) == snap_ctrl, "Real control DB was modified by test!"
    if snap_db is not None and _CANARY_DB.exists():
        assert _snap(_CANARY_DB) == snap_db, "Real user DB was modified by test!"


@pytest.fixture(autouse=True)
def _mock_service_stopped(monkeypatch):
    """Monkeypatch service-stopped check so tests do not require a stopped service."""
    monkeypatch.setattr(database_reset, "require_service_stopped", lambda *args, **kwargs: None)


# ---------------------------------------------------------------------------
# Test environment helpers
# ---------------------------------------------------------------------------

def _setup_test_env(tmp_path: Path, monkeypatch, multi_user: bool = False):
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
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(single_user_db))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, val TEXT);")
    conn.execute("INSERT INTO sample (val) VALUES ('single_user_data');")
    conn.commit()
    conn.close()

    tenant_root = data_dir / "tenants"
    tenant_root.mkdir(parents=True, exist_ok=True)

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


def _prepare_plan(tmp_path, monkeypatch, multi_user=False):
    """Set up env and return all confirmation arguments."""
    proj_root, backup_root, restore_root, control_db, single_user_db, commit_hex = \
        _setup_test_env(tmp_path, monkeypatch, multi_user=multi_user)

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

    return {
        "project_root": proj_root,
        "backup_root": backup_root,
        "restore_root": restore_root,
        "control_db": control_db,
        "single_user_db": single_user_db,
        "commit_hex": commit_hex,
        "selected_id": selected_id,
        "snap": snap,
        "t_hash": t_hash,
        "c_val": c_val,
        "runtime_mode": runtime_mode,
    }


def _cli_args(env) -> list[str]:
    """Build standard CLI arguments from env dict."""
    return [
        "--backup-id", env["selected_id"],
        "--expected-current-commit", env["commit_hex"],
        "--confirm-target-set-hash", env["t_hash"],
        "--confirm-restore", env["c_val"],
    ]


def _parse_stdout(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out.strip()), captured.err.strip()


def _parse_stderr(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.err.strip()), captured.out.strip()


# ---------------------------------------------------------------------------
# 1. Argument validation (B1)
# ---------------------------------------------------------------------------

def test_missing_backup_id_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = [
        "--expected-current-commit", env["commit_hex"],
        "--confirm-target-set-hash", env["t_hash"],
        "--confirm-restore", env["c_val"],
    ]
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_missing_expected_commit_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = [
        "--backup-id", env["selected_id"],
        "--confirm-target-set-hash", env["t_hash"],
        "--confirm-restore", env["c_val"],
    ]
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_missing_target_set_hash_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = [
        "--backup-id", env["selected_id"],
        "--expected-current-commit", env["commit_hex"],
        "--confirm-restore", env["c_val"],
    ]
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_missing_confirm_restore_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = [
        "--backup-id", env["selected_id"],
        "--expected-current-commit", env["commit_hex"],
        "--confirm-target-set-hash", env["t_hash"],
    ]
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_malformed_backup_id_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["selected_id"])] = "not-a-valid-id"
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_malformed_operation_id_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env) + ["--operation-id", "bad-op-id"]
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_malformed_target_set_hash_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["t_hash"])] = "not-a-sha256"
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_malformed_confirm_restore_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["c_val"])] = "short"
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_wrong_commit_format_exits_64(tmp_path, monkeypatch, capsys):
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["commit_hex"])] = "not-hex!!"
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


def test_commit_unknown_is_accepted(tmp_path, monkeypatch):
    val = _validate_commit("unknown")
    assert val == "unknown"


# ---------------------------------------------------------------------------
# 2. Argument validator unit tests
# ---------------------------------------------------------------------------

def test_validate_backup_id_strips_prefix():
    val = _validate_backup_id("backup-20260803T090000Z-12345678")
    assert val == "20260803T090000Z-12345678"


def test_validate_backup_id_rejects_traversal():
    with pytest.raises(ValueError):
        _validate_backup_id("20260803T090000Z-12345678/../etc/passwd")


def test_validate_backup_id_rejects_invalid_format():
    with pytest.raises(ValueError):
        _validate_backup_id("not-a-backup")


def test_validate_operation_id_accepts_valid():
    val = _validate_operation_id("restore-20260803T090000Z-12345678")
    assert val == "restore-20260803T090000Z-12345678"


def test_validate_operation_id_rejects_invalid():
    with pytest.raises(ValueError):
        _validate_operation_id("not-a-restore-id")


def test_validate_hex64_accepts_valid():
    val = _validate_hex64("a" * 64, "test")
    assert val == "a" * 64


def test_validate_hex64_rejects_short():
    with pytest.raises(ValueError):
        _validate_hex64("a" * 32, "test")


def test_validate_hex64_rejects_uppercase():
    with pytest.raises(ValueError):
        _validate_hex64("A" * 64, "test")


# ---------------------------------------------------------------------------
# 3. Fresh apply flow (B2)
# ---------------------------------------------------------------------------

def test_fresh_apply_succeeds(tmp_path, monkeypatch, capsys):
    """Fresh apply with valid args completes successfully."""
    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    out, err = _parse_stdout(capsys)
    assert out["format_version"] == "apply-v1"
    assert out["outcome"] == "success"
    assert out["stage"] == "COMPLETED"
    assert out["exit_code"] == APPLY_EXIT_SUCCESS
    assert out["configured_database_mutated"] is True
    assert out["locks_released"] is True
    assert err == ""


def test_fresh_apply_single_user(tmp_path, monkeypatch, capsys):
    """Fresh apply in single-user mode succeeds."""
    env = _prepare_plan(tmp_path, monkeypatch, multi_user=False)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    out, _ = _parse_stdout(capsys)
    assert out["runtime_mode"] == "single_user"
    assert out["target_key_count"] >= 2


def test_fresh_apply_wrong_commit_exits_65(tmp_path, monkeypatch, capsys):
    """Wrong expected commit causes precondition failure."""
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["commit_hex"])] = "b" * 40
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_PRECONDITION_FAILED


def test_fresh_apply_wrong_target_hash_exits_65(tmp_path, monkeypatch, capsys):
    """Wrong target-set hash causes precondition failure."""
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["t_hash"])] = "b" * 64
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_PRECONDITION_FAILED


def test_fresh_apply_wrong_confirm_restore_exits_65(tmp_path, monkeypatch, capsys):
    """Wrong confirmation value causes precondition failure."""
    env = _prepare_plan(tmp_path, monkeypatch)
    args = _cli_args(env)
    args[args.index(env["c_val"])] = "c" * 64
    exit_code = main(args)
    assert exit_code == APPLY_EXIT_PRECONDITION_FAILED


def test_fresh_apply_not_at_project_root_exits_65(tmp_path, monkeypatch, capsys):
    """Running from wrong directory causes precondition failure."""
    env = _prepare_plan(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_PRECONDITION_FAILED


def test_fresh_apply_service_running_exits_65(tmp_path, monkeypatch, capsys):
    """Service running causes precondition failure."""
    import guarded_restore_configured_replacement as _rmod

    env = _prepare_plan(tmp_path, monkeypatch)

    original_fn = prepare_configured_restore

    def mock_prepare(**kwargs):
        raise ConfiguredReplacementPreconditionError("Service-stopped proof failed")

    monkeypatch.setattr(
        "apply_verified_restore.prepare_configured_restore",
        mock_prepare,
    )

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_PRECONDITION_FAILED


def test_fresh_apply_calls_prepare_before_replace(tmp_path, monkeypatch, capsys):
    """Apply CLI calls prepare_configured_restore before replace_and_verify."""
    call_log = []

    original_prepare = prepare_configured_restore
    original_replace = replace_and_verify_configured_restore

    def tracked_prepare(**kwargs):
        call_log.append("prepare")
        return original_prepare(**kwargs)

    def tracked_replace(**kwargs):
        call_log.append("replace")
        return original_replace(**kwargs)

    monkeypatch.setattr("apply_verified_restore.prepare_configured_restore", tracked_prepare)
    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", tracked_replace)

    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS
    assert call_log == ["prepare", "replace"]


def test_fresh_apply_skips_prepare_for_reentry(tmp_path, monkeypatch):
    """Re-entry skips prepare_configured_restore and goes directly to replace."""
    call_log = []

    original_prepare = prepare_configured_restore
    original_replace = replace_and_verify_configured_restore

    def tracked_prepare(**kwargs):
        call_log.append("prepare")
        return original_prepare(**kwargs)

    def tracked_replace(**kwargs):
        call_log.append("replace")
        return original_replace(**kwargs)

    monkeypatch.setattr("apply_verified_restore.prepare_configured_restore", tracked_prepare)
    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", tracked_replace)

    env = _prepare_plan(tmp_path, monkeypatch)
    # First, do a fresh apply to get an operation ID.
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    call_log.clear()
    # Now do re-entry. We need an operation that is not COMPLETED.
    # Get the op_id from the restore_root.
    op_dirs = list(env["restore_root"].glob("operation-*"))
    assert len(op_dirs) == 1
    op_id = op_dirs[0].name.removeprefix("operation-")

    # Re-entry on COMPLETED should succeed idempotently.
    reentry_args = _cli_args(env) + ["--operation-id", op_id]
    exit_code = main(reentry_args)
    # COMPLETED re-entry: prepare not called, replace called.
    assert "prepare" not in call_log
    assert "replace" in call_log


# ---------------------------------------------------------------------------
# 4. Re-entry flows (B3)
# ---------------------------------------------------------------------------

def test_reentry_on_completed_returns_success_idempotent(tmp_path, monkeypatch, capsys):
    """Re-entry on a COMPLETED journal returns success_idempotent."""
    env = _prepare_plan(tmp_path, monkeypatch)
    # Fresh apply.
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    op_dirs = list(env["restore_root"].glob("operation-*"))
    op_id = op_dirs[0].name.removeprefix("operation-")

    capsys.readouterr()
    reentry_args = _cli_args(env) + ["--operation-id", op_id]
    exit_code = main(reentry_args)
    assert exit_code == APPLY_EXIT_SUCCESS

    out, _ = _parse_stdout(capsys)
    assert out["outcome"] in ("success", "success_idempotent")


def test_reentry_on_failed_manual_refuses(tmp_path, monkeypatch, capsys):
    """Re-entry on FAILED_MANUAL_RECOVERY_REQUIRED exits 68."""
    env = _prepare_plan(tmp_path, monkeypatch)
    prep = prepare_configured_restore(
        selected_backup_id=env["selected_id"],
        expected_application_commit=env["commit_hex"],
        confirmed_target_set_hash=env["t_hash"],
        confirmed_restore_value=env["c_val"],
    )
    op_id = prep.operation_id

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    import json as _json
    data = _json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "FAILED_MANUAL_RECOVERY_REQUIRED"
    data["final_result"] = "FAILED_MANUAL_RECOVERY_REQUIRED"
    j_path.write_bytes((_json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))

    reentry_args = _cli_args(env) + ["--operation-id", op_id]
    exit_code = main(reentry_args)
    assert exit_code == APPLY_EXIT_MANUAL_RECOVERY_REQUIRED

    err, _ = _parse_stderr(capsys)
    assert err["format_version"] == "apply-error-v1"
    assert err["exit_code"] == APPLY_EXIT_MANUAL_RECOVERY_REQUIRED


def test_reentry_on_failed_safe_refuses(tmp_path, monkeypatch, capsys):
    """Re-entry on FAILED_SAFE exits with rollback-completed code."""
    env = _prepare_plan(tmp_path, monkeypatch)
    prep = prepare_configured_restore(
        selected_backup_id=env["selected_id"],
        expected_application_commit=env["commit_hex"],
        confirmed_target_set_hash=env["t_hash"],
        confirmed_restore_value=env["c_val"],
    )
    op_id = prep.operation_id

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    import json as _json
    data = _json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "FAILED_SAFE"
    data["final_result"] = "FAILED_SAFE"
    j_path.write_bytes((_json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))

    reentry_args = _cli_args(env) + ["--operation-id", op_id]
    exit_code = main(reentry_args)
    assert exit_code == APPLY_EXIT_ROLLBACK_COMPLETED


def test_reentry_legal_replacing_delegates_to_engine(tmp_path, monkeypatch, capsys):
    """Legal REPLACING re-entry is delegated entirely to the replacement engine."""
    env = _prepare_plan(tmp_path, monkeypatch)
    prep = prepare_configured_restore(
        selected_backup_id=env["selected_id"],
        expected_application_commit=env["commit_hex"],
        confirmed_target_set_hash=env["t_hash"],
        confirmed_restore_value=env["c_val"],
    )
    op_id = prep.operation_id

    j_path = env["restore_root"] / f"operation-{op_id}" / "journal.json"
    import json as _json
    data = _json.loads(j_path.read_bytes().decode("utf-8"))
    data["stage"] = "REPLACING"
    for t in data["targets"]:
        t["replacement_intent"] = True
        t["state"] = "STAGED_VERIFIED"
    j_path.write_bytes((_json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))

    reentry_args = _cli_args(env) + ["--operation-id", op_id]
    exit_code = main(reentry_args)
    assert exit_code in (APPLY_EXIT_SUCCESS, APPLY_EXIT_ROLLBACK_COMPLETED, APPLY_EXIT_MANUAL_RECOVERY_REQUIRED)


def test_reentry_requires_all_four_confirmation_args(tmp_path, monkeypatch):
    """Re-entry must include all four confirmation arguments."""
    env = _prepare_plan(tmp_path, monkeypatch)
    prep = prepare_configured_restore(
        selected_backup_id=env["selected_id"],
        expected_application_commit=env["commit_hex"],
        confirmed_target_set_hash=env["t_hash"],
        confirmed_restore_value=env["c_val"],
    )
    op_id = prep.operation_id

    # Missing --backup-id → exit 64.
    args_no_backup = [
        "--expected-current-commit", env["commit_hex"],
        "--confirm-target-set-hash", env["t_hash"],
        "--confirm-restore", env["c_val"],
        "--operation-id", op_id,
    ]
    exit_code = main(args_no_backup)
    assert exit_code == APPLY_EXIT_INVALID_ARGUMENTS


# ---------------------------------------------------------------------------
# 5. Exit codes (B4)
# ---------------------------------------------------------------------------

def test_failed_safe_outcome_exits_66(tmp_path, monkeypatch, capsys):
    """FAILED_SAFE from replacement engine maps to exit 66 (rollback completed)."""
    env = _prepare_plan(tmp_path, monkeypatch)

    import guarded_restore_configured_replacement as _rmod

    original_replace = replace_and_verify_configured_restore

    def failing_replace(**kwargs):
        raise ConfiguredReplacementRollbackCompletedError("Injected rollback completion")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_ROLLBACK_COMPLETED

    err, _ = _parse_stderr(capsys)
    assert err["exit_code"] == APPLY_EXIT_ROLLBACK_COMPLETED


def test_manual_recovery_outcome_exits_68(tmp_path, monkeypatch, capsys):
    """Manual recovery required maps to exit 68."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise ConfiguredReplacementManualRecoveryRequiredError("Injected manual recovery")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_MANUAL_RECOVERY_REQUIRED

    err, _ = _parse_stderr(capsys)
    assert err["error_kind"] == "manual_recovery_required"


def test_precondition_failure_exits_65(tmp_path, monkeypatch, capsys):
    """ConfiguredReplacementPreconditionError maps to exit 65."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise ConfiguredReplacementPreconditionError("Injected precondition failure")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_PRECONDITION_FAILED


def test_cleanup_required_exits_69(tmp_path, monkeypatch, capsys):
    """ConfiguredReplacementCleanupError maps to exit 69."""
    from guarded_restore_configured_replacement import ConfiguredReplacementCleanupError
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise ConfiguredReplacementCleanupError("Injected cleanup error")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_CLEANUP_REQUIRED


def test_journal_uncertainty_exits_70(tmp_path, monkeypatch, capsys):
    """ConfiguredJournalUncertaintyError maps to exit 70."""
    from guarded_restore_configured import ConfiguredJournalUncertaintyError
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise ConfiguredJournalUncertaintyError("Injected journal uncertainty")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_JOURNAL_UNCERTAINTY


def test_lock_uncertainty_exits_71(tmp_path, monkeypatch, capsys):
    """RestoreLockError maps to exit 71."""
    from guarded_restore import RestoreLockError
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise RestoreLockError("Injected lock error")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_LOCK_UNCERTAINTY


def test_unexpected_failure_exits_72(tmp_path, monkeypatch, capsys):
    """An unexpected exception maps to exit 72."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise RuntimeError("completely unexpected")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_UNEXPECTED_FAILURE


# ---------------------------------------------------------------------------
# 6. Output contract (B5)
# ---------------------------------------------------------------------------

def test_success_output_is_bounded_json_to_stdout(tmp_path, monkeypatch, capsys):
    """Success output is valid JSON on stdout with required fields."""
    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    assert captured.err == ""
    out = json.loads(captured.out.strip())
    assert out["format_version"] == "apply-v1"
    assert "outcome" in out
    assert "operation_id" in out
    assert "stage" in out
    assert "selected_backup_id" in out
    assert "safety_backup_id" in out
    assert "runtime_mode" in out
    assert "target_key_count" in out
    assert "rollback_occurred" in out
    assert "configured_database_mutated" in out
    assert "locks_released" in out
    assert "exit_code" in out


def test_error_output_is_bounded_json_to_stderr(tmp_path, monkeypatch, capsys):
    """Error output is valid JSON on stderr."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise ConfiguredReplacementPreconditionError("Injected error")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code != APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    err = json.loads(captured.err.strip())
    assert err["format_version"] == "apply-error-v1"
    assert "outcome" in err
    assert "error_kind" in err
    assert "message" in err
    assert "exit_code" in err


def test_no_absolute_path_in_stdout(tmp_path, monkeypatch, capsys):
    """No absolute paths appear in JSON stdout output."""
    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    out_str = captured.out
    assert str(tmp_path) not in out_str
    assert str(env["project_root"]) not in out_str


def test_no_absolute_path_in_stderr(tmp_path, monkeypatch, capsys):
    """No absolute paths appear in JSON stderr output on error."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_prepare(**kwargs):
        raise ConfiguredRestorePreconditionError(f"Failed at {env['project_root']}/sensitive/path")

    from guarded_restore_configured import ConfiguredRestorePreconditionError
    monkeypatch.setattr("apply_verified_restore.prepare_configured_restore", failing_prepare)

    exit_code = main(_cli_args(env))
    assert exit_code != APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    err_str = captured.err
    assert str(tmp_path) not in err_str, "Absolute path must not appear in stderr"


def test_no_traceback_in_output(tmp_path, monkeypatch, capsys):
    """No Python tracebacks appear in stdout or stderr."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def failing_replace(**kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    assert exit_code != APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    assert "  File " not in captured.err


def test_no_token_in_output(tmp_path, monkeypatch, capsys):
    """Token-like content from an exception does not appear in output."""
    env = _prepare_plan(tmp_path, monkeypatch)
    secret_token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.secret.payload"

    def failing_replace(**kwargs):
        raise ConfiguredReplacementManualRecoveryRequiredError(f"auth={secret_token}")

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", failing_replace)

    exit_code = main(_cli_args(env))
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert secret_token not in combined, "Secret token must not appear in output"


def test_stdout_output_is_deterministic_json(tmp_path, monkeypatch, capsys):
    """JSON output is canonical (sort_keys, deterministic)."""
    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    out_str = captured.out.strip()
    parsed = json.loads(out_str)
    re_serialized = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert out_str == re_serialized


# ---------------------------------------------------------------------------
# 7. Crash and interruption behavior (B6)
# ---------------------------------------------------------------------------

def test_keyboard_interrupt_exits_without_traceback(tmp_path, monkeypatch, capsys):
    """KeyboardInterrupt is handled without traceback and without pretending success."""
    env = _prepare_plan(tmp_path, monkeypatch)

    def interrupting_replace(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("apply_verified_restore.replace_and_verify_configured_restore", interrupting_replace)

    exit_code = main(_cli_args(env))
    assert exit_code != APPLY_EXIT_SUCCESS

    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    assert "KeyboardInterrupt" not in captured.out


# ---------------------------------------------------------------------------
# 8. CLI safety constraints (no direct DB access, no service control)
# ---------------------------------------------------------------------------

def test_cli_never_calls_os_replace(tmp_path, monkeypatch, capsys):
    """CLI module does not import or call os.replace directly."""
    import apply_verified_restore as _av_mod
    assert not hasattr(_av_mod, "_replace"), "CLI must not expose os.replace alias"
    import inspect
    src = inspect.getsource(_av_mod)
    assert "os.replace" not in src, "CLI must not call os.replace directly"
    assert "os.unlink" not in src, "CLI must not call os.unlink directly"


def test_cli_does_not_import_database_reset_for_service_start(tmp_path, monkeypatch, capsys):
    """CLI does not start or stop the service."""
    import apply_verified_restore as _av_mod
    import inspect
    src = inspect.getsource(_av_mod)
    assert "start_service" not in src
    assert "stop_service" not in src
    assert "restart" not in src


def test_cli_confirmation_values_passed_unchanged_to_prepare(tmp_path, monkeypatch):
    """CLI passes confirmation arguments unchanged to prepare_configured_restore."""
    captured_prepare_kwargs = {}

    original_prepare = prepare_configured_restore

    def capturing_prepare(**kwargs):
        captured_prepare_kwargs.update(kwargs)
        return original_prepare(**kwargs)

    monkeypatch.setattr("apply_verified_restore.prepare_configured_restore", capturing_prepare)

    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    assert captured_prepare_kwargs["selected_backup_id"] == env["selected_id"]
    assert captured_prepare_kwargs["expected_application_commit"] == env["commit_hex"]
    assert captured_prepare_kwargs["confirmed_target_set_hash"] == env["t_hash"]
    assert captured_prepare_kwargs["confirmed_restore_value"] == env["c_val"]


def test_cli_confirmation_values_passed_unchanged_to_replace(tmp_path, monkeypatch):
    """CLI passes confirmation arguments unchanged to replace_and_verify_configured_restore."""
    captured_replace_kwargs = {}

    original_replace = replace_and_verify_configured_restore

    def capturing_replace(**kwargs):
        captured_replace_kwargs.update(kwargs)
        return original_replace(**kwargs)

    monkeypatch.setattr(
        "apply_verified_restore.replace_and_verify_configured_restore",
        capturing_replace,
    )

    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    assert exit_code == APPLY_EXIT_SUCCESS

    assert captured_replace_kwargs["selected_backup_id"] == env["selected_id"]
    assert captured_replace_kwargs["expected_application_commit"] == env["commit_hex"]
    assert captured_replace_kwargs["confirmed_target_set_hash"] == env["t_hash"]
    assert captured_replace_kwargs["confirmed_restore_value"] == env["c_val"]


# ---------------------------------------------------------------------------
# 9. Real configured path canary
# ---------------------------------------------------------------------------

def test_real_configured_path_canary(tmp_path, monkeypatch):
    """Verifies apply CLI never touches real configured DB paths (canary fixture)."""
    env = _prepare_plan(tmp_path, monkeypatch)
    exit_code = main(_cli_args(env))
    # The fixture validates real path integrity.
    assert exit_code in (APPLY_EXIT_SUCCESS, APPLY_EXIT_PRECONDITION_FAILED)
