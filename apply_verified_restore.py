"""Operator apply CLI for guarded verified restore (Phase 6B3B3).

Noninteractive wrapper around the approved Phase 6B3B1 preparation API and the
Phase 6B3B2 replacement API.  Requires all four confirmation arguments on every
invocation.  Supports explicit re-entry via --operation-id.

Exit codes
----------
0   success or verified idempotent COMPLETED re-entry
64  CLI usage error or invalid argument
65  precondition or verification refusal (no configured database mutated)
66  safe failure: automatic rollback succeeded and verified (FAILED_SAFE)
67  automatic rollback completed and verified (rollback path from live run)
68  manual recovery required; do not auto-resume
69  evidence cleanup required; database outcome is known
70  journal or persistence state is uncertain
71  lock acquisition or release uncertainty
72  unexpected internal failure
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config
from guarded_restore import (
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
    RestoreJournalError,
    RestoreLockError,
    RestoreStage,
    _BACKUP_ID,
    _COMMIT,
    _OPERATION_ID,
    load_restore_journal,
    validate_restore_root,
)
from guarded_restore_configured import (
    ConfiguredJournalUncertaintyError,
    ConfiguredRestoreLockReleaseError,
    ConfiguredRestorePreconditionError,
    ConfiguredRestorePreparationResult,
    prepare_configured_restore,
)
from guarded_restore_configured_replacement import (
    ConfiguredReplacementCleanupError,
    ConfiguredReplacementManualRecoveryRequiredError,
    ConfiguredReplacementPreconditionError,
    ConfiguredReplacementResult,
    ConfiguredReplacementRollbackCompletedError,
    replace_and_verify_configured_restore,
)
from operator_storage import has_symlink_component, safe_resolve

_APPLY_FORMAT_VERSION = "apply-v1"
_APPLY_ERROR_FORMAT_VERSION = "apply-error-v1"


def _bounded_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stdout_result(
    *,
    outcome: str,
    operation_id: str,
    stage: str,
    selected_backup_id: str,
    safety_backup_id: str | None,
    runtime_mode: str,
    target_key_count: int,
    rollback_occurred: bool,
    configured_database_mutated: bool,
    locks_released: bool,
    exit_code: int,
) -> str:
    return _bounded_json({
        "format_version": _APPLY_FORMAT_VERSION,
        "outcome": outcome,
        "operation_id": operation_id,
        "stage": stage,
        "selected_backup_id": selected_backup_id,
        "safety_backup_id": safety_backup_id,
        "runtime_mode": runtime_mode,
        "target_key_count": target_key_count,
        "rollback_occurred": rollback_occurred,
        "configured_database_mutated": configured_database_mutated,
        "locks_released": locks_released,
        "exit_code": exit_code,
    })


def _stderr_error(*, error_kind: str, message: str, exit_code: int) -> str:
    return _bounded_json({
        "format_version": _APPLY_ERROR_FORMAT_VERSION,
        "outcome": "error",
        "error_kind": error_kind,
        "message": message,
        "exit_code": exit_code,
    })


def _validate_backup_id(raw: str) -> str:
    val = raw.strip()
    if val.startswith("backup-"):
        val = val[7:]
    if not _BACKUP_ID.fullmatch(val):
        raise ValueError(f"Invalid backup ID format: must match YYYYMMDDTHHMMSSZ-<8hex>")
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("Backup ID cannot contain path separators or traversal components")
    return val


def _validate_operation_id(raw: str) -> str:
    val = raw.strip()
    if not _OPERATION_ID.fullmatch(val):
        raise ValueError(f"Invalid operation ID format: must match restore-YYYYMMDDTHHMMSSZ-<8hex>")
    if "/" in val or "\\" in val or ".." in val:
        raise ValueError("Operation ID cannot contain path separators or traversal components")
    return val


def _validate_hex64(raw: str, name: str) -> str:
    val = raw.strip()
    if len(val) != 64 or not all(c in "0123456789abcdef" for c in val):
        raise ValueError(f"{name} must be a 64-character lowercase hex string")
    return val


def _validate_commit(raw: str) -> str:
    val = raw.strip()
    if val == "unknown":
        return val
    if not _COMMIT.fullmatch(val):
        raise ValueError("Expected commit must be a 7-64 character hex string or 'unknown'")
    return val


def _verify_project_root() -> None:
    try:
        cwd = Path.cwd().resolve()
        project_root = safe_resolve(config.PROJECT_ROOT)
    except (ValueError, OSError) as exc:
        raise ConfiguredRestorePreconditionError("Project root path verification failed") from exc
    if cwd != project_root:
        raise ConfiguredRestorePreconditionError(
            "apply_verified_restore.py must be run from the exact configured GarminCoach project root"
        )
    if has_symlink_component(config.PROJECT_ROOT) or has_symlink_component(Path.cwd()):
        raise ConfiguredRestorePreconditionError("Project root path cannot contain symlinks")


def _result_from_replacement(
    result: ConfiguredReplacementResult,
    *,
    outcome: str,
    exit_code: int,
) -> str:
    return _stdout_result(
        outcome=outcome,
        operation_id=result.operation_id,
        stage=result.stage.value,
        selected_backup_id=result.selected_backup_id,
        safety_backup_id=result.safety_backup_id,
        runtime_mode=result.runtime_mode,
        target_key_count=len(result.target_keys),
        rollback_occurred=result.rollback_occurred,
        configured_database_mutated=result.configured_database_mutated,
        locks_released=result.locks_released,
        exit_code=exit_code,
    )


def _apply_fresh(
    *,
    backup_id: str,
    expected_commit: str,
    target_set_hash: str,
    confirm_restore: str,
) -> tuple[str, int]:
    """Execute a fresh apply: prepare → replace."""
    prep: ConfiguredRestorePreparationResult = prepare_configured_restore(
        selected_backup_id=backup_id,
        expected_application_commit=expected_commit,
        confirmed_target_set_hash=target_set_hash,
        confirmed_restore_value=confirm_restore,
    )
    if prep.stage is not RestoreStage.REPLACEMENT_READY:
        raise ConfiguredRestorePreconditionError(
            f"Preparation returned unexpected stage: {prep.stage.value}"
        )

    result: ConfiguredReplacementResult = replace_and_verify_configured_restore(
        operation_id=prep.operation_id,
        selected_backup_id=backup_id,
        expected_application_commit=expected_commit,
        confirmed_target_set_hash=target_set_hash,
        confirmed_restore_value=confirm_restore,
    )
    output = _result_from_replacement(result, outcome="success", exit_code=APPLY_EXIT_SUCCESS)
    return output, APPLY_EXIT_SUCCESS


def _apply_reentry(
    *,
    operation_id: str,
    backup_id: str,
    expected_commit: str,
    target_set_hash: str,
    confirm_restore: str,
) -> tuple[str, int]:
    """Execute explicit re-entry using an existing journal."""
    restore_root = validate_restore_root(config.OPERATOR_RESTORE_ROOT)

    try:
        journal = load_restore_journal(operation_id, root=restore_root)
    except (RestoreJournalError, OSError, ValueError) as exc:
        raise ConfiguredRestorePreconditionError(
            "Re-entry journal is invalid or unavailable"
        ) from exc

    if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Operation is in FAILED_MANUAL_RECOVERY_REQUIRED; inspect-only"
        )

    _terminal_stages = {RestoreStage.COMPLETED, RestoreStage.FAILED_SAFE}
    if journal.stage is RestoreStage.FAILED_SAFE:
        raise ConfiguredReplacementRollbackCompletedError(
            "Operation already settled FAILED_SAFE; no further apply possible"
        )

    result: ConfiguredReplacementResult = replace_and_verify_configured_restore(
        operation_id=operation_id,
        selected_backup_id=backup_id,
        expected_application_commit=expected_commit,
        confirmed_target_set_hash=target_set_hash,
        confirmed_restore_value=confirm_restore,
    )

    if result.stage is RestoreStage.COMPLETED and not result.configured_database_mutated:
        outcome = "success_idempotent"
    else:
        outcome = "success"
    output = _result_from_replacement(result, outcome=outcome, exit_code=APPLY_EXIT_SUCCESS)
    return output, APPLY_EXIT_SUCCESS


def apply_restore(
    *,
    backup_id: str,
    expected_commit: str,
    target_set_hash: str,
    confirm_restore: str,
    operation_id: str | None = None,
) -> tuple[str, int]:
    """Core apply logic. Returns (stdout_json, exit_code). Raises on errors."""
    _verify_project_root()

    if operation_id is not None:
        return _apply_reentry(
            operation_id=operation_id,
            backup_id=backup_id,
            expected_commit=expected_commit,
            target_set_hash=target_set_hash,
            confirm_restore=confirm_restore,
        )
    return _apply_fresh(
        backup_id=backup_id,
        expected_commit=expected_commit,
        target_set_hash=target_set_hash,
        confirm_restore=confirm_restore,
    )


_BOUNDED_MESSAGES: dict[type, tuple[str, int, str]] = {
    ConfiguredReplacementManualRecoveryRequiredError: (
        "manual_recovery_required",
        APPLY_EXIT_MANUAL_RECOVERY_REQUIRED,
        "Manual recovery required; inspect journal and do not auto-resume",
    ),
    ConfiguredReplacementCleanupError: (
        "cleanup_required",
        APPLY_EXIT_CLEANUP_REQUIRED,
        "Database outcome is known; evidence cleanup requires manual action",
    ),
    ConfiguredReplacementRollbackCompletedError: (
        "rollback_completed",
        APPLY_EXIT_ROLLBACK_COMPLETED,
        "Automatic rollback completed and verified; databases restored to safety backup",
    ),
    ConfiguredReplacementPreconditionError: (
        "precondition_failed",
        APPLY_EXIT_PRECONDITION_FAILED,
        "Precondition or verification failure; no configured database mutated",
    ),
    ConfiguredJournalUncertaintyError: (
        "journal_uncertainty",
        APPLY_EXIT_JOURNAL_UNCERTAINTY,
        "Journal or persistence state is uncertain; inspect journal before retrying",
    ),
    RestoreLockError: (
        "lock_uncertainty",
        APPLY_EXIT_LOCK_UNCERTAINTY,
        "Lock acquisition or release uncertainty; inspect journal before retrying",
    ),
    ConfiguredRestoreLockReleaseError: (
        "lock_uncertainty",
        APPLY_EXIT_LOCK_UNCERTAINTY,
        "Lock release uncertainty; inspect journal before retrying",
    ),
    ConfiguredRestorePreconditionError: (
        "precondition_failed",
        APPLY_EXIT_PRECONDITION_FAILED,
        "Preparation precondition or confirmation failure; no configured database mutated",
    ),
    RestoreJournalError: (
        "precondition_failed",
        APPLY_EXIT_PRECONDITION_FAILED,
        "Re-entry journal is invalid or unavailable",
    ),
}


def _map_exception_to_exit(exc: BaseException) -> tuple[str, int, str]:
    """Map a raised exception to (error_kind, exit_code, bounded_message). No message-text matching."""
    for exc_type, (kind, code, msg) in _BOUNDED_MESSAGES.items():
        if isinstance(exc, exc_type):
            return kind, code, msg
    return "unexpected_failure", APPLY_EXIT_UNEXPECTED_FAILURE, "An unexpected internal failure occurred"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator apply CLI for GarminCoach guarded verified restore (Phase 6B3B3)",
        prog="apply_verified_restore.py",
        add_help=True,
    )
    parser.add_argument(
        "--backup-id",
        dest="backup_id",
        required=True,
        help="Exact verified backup ID (e.g. 20260803T090000Z-12345678)",
    )
    parser.add_argument(
        "--expected-current-commit",
        dest="expected_commit",
        required=True,
        help="Expected application git commit SHA or 'unknown'",
    )
    parser.add_argument(
        "--confirm-target-set-hash",
        dest="target_set_hash",
        required=True,
        help="Target-set SHA-256 from plan output (64 hex chars)",
    )
    parser.add_argument(
        "--confirm-restore",
        dest="confirm_restore",
        required=True,
        help="Confirmation value from plan output (64 hex chars)",
    )
    parser.add_argument(
        "--operation-id",
        dest="operation_id",
        default=None,
        help="Existing operation ID for explicit re-entry (optional)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        err = _stderr_error(
            error_kind="invalid_arguments",
            message="Invalid or missing CLI arguments",
            exit_code=APPLY_EXIT_INVALID_ARGUMENTS,
        )
        print(err, file=sys.stderr)
        return APPLY_EXIT_INVALID_ARGUMENTS if exc.code != 0 else APPLY_EXIT_SUCCESS

    try:
        backup_id = _validate_backup_id(args.backup_id)
        expected_commit = _validate_commit(args.expected_commit)
        target_set_hash = _validate_hex64(args.target_set_hash, "--confirm-target-set-hash")
        confirm_restore = _validate_hex64(args.confirm_restore, "--confirm-restore")
        operation_id = _validate_operation_id(args.operation_id) if args.operation_id else None
    except ValueError as exc:
        err = _stderr_error(
            error_kind="invalid_arguments",
            message=str(exc),
            exit_code=APPLY_EXIT_INVALID_ARGUMENTS,
        )
        print(err, file=sys.stderr)
        return APPLY_EXIT_INVALID_ARGUMENTS

    try:
        stdout_json, exit_code = apply_restore(
            backup_id=backup_id,
            expected_commit=expected_commit,
            target_set_hash=target_set_hash,
            confirm_restore=confirm_restore,
            operation_id=operation_id,
        )
        print(stdout_json)
        return exit_code

    except KeyboardInterrupt:
        err = _stderr_error(
            error_kind="interrupted",
            message="Operation interrupted; journal remains authoritative; use --operation-id for re-entry",
            exit_code=APPLY_EXIT_UNEXPECTED_FAILURE,
        )
        print(err, file=sys.stderr)
        return APPLY_EXIT_UNEXPECTED_FAILURE

    except BaseException as exc:
        error_kind, exit_code, bounded_msg = _map_exception_to_exit(exc)
        err = _stderr_error(
            error_kind=error_kind,
            message=bounded_msg,
            exit_code=exit_code,
        )
        print(err, file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
