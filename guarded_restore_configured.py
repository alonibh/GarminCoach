"""Configured-runtime restore orchestration through REPLACEMENT_READY (Phase 6B3B1).

Orchestrates configured-runtime restore preparation up to global stage REPLACEMENT_READY:
1. Keyword-only API with mandatory target-set hash and restore confirmation validation.
2. Exact project-root verification (CWD == PROJECT_ROOT, no symlinks, git toplevel/HEAD match).
3. Canonical runtime target discovery from config.PROJECT_ROOT.
4. ProcessLock -> RestoreLock -> safety backup creation -> long-held BackupLock acquisition chain.
5. Strict safety-backup verification before journal recording.
6. Same-filesystem target staging with canonical ownership bindings.
7. Descriptor-safe staged file publication and deep SQLite integrity checks (with foreign keys).
8. Destination database and sidecar baseline tracking and drift revalidation.
9. Grouped per-filesystem disk-space preflight checks.
10. Complete readiness proof barrier before global transition to REPLACEMENT_READY.
11. Reverse lock release (BackupLock -> RestoreLock -> ProcessLock) BEFORE returning result.
12. Immutable result object with locks_released=True and no internal lock/file handles.

Configured database files and sidecars are NOT modified by this module.
Phase 6B3B2 (replacement/rollback) and Phase 6B3B3 (CLI/apply) remain unimplemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Any

import config
from guarded_restore import (
    RestoreJournal,
    RestoreLock,
    RestoreLockError,
    RestorePlan,
    RestorePlanError,
    RestoreStage,
    TargetRestoreState,
    confirmation_value,
    create_restore_journal,
    create_restore_plan,
    load_restore_journal,
    target_set_hash,
    update_restore_journal,
    validate_restore_root,
)
from guarded_restore_configured_staging import (
    ConfiguredPreflightError,
    ConfiguredStagingError,
    ConfiguredStagingResult,
    capture_destination_baselines,
    preflight_backup_disk_space,
    preflight_staging_disk_space,
    revalidate_destination_baselines,
    stage_configured_targets,
    verify_configured_readiness,
)
from operator_storage import (
    DatabaseTarget,
    TargetProfile,
    discover_database_targets,
    has_symlink_component,
    inspect_sqlite,
    safe_resolve,
)
from process_lock import ProcessLock, acquire_process_lock, release_process_lock
from verified_backup import (
    BackupError,
    BackupLock,
    ValidatedBackupSnapshot,
    create_verified_backup,
    load_validated_backup_snapshot,
    validate_backup_root,
)

_BACKUP_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_OPERATION_ID_PATTERN = re.compile(r"^restore-\d{8}T\d{6}Z-[0-9a-f]{8}$")
_COMMIT_HEX_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")


class ConfiguredRestoreError(RuntimeError):
    """Base error for configured restore orchestration failures."""


class ConfiguredRestorePreconditionError(ConfiguredRestoreError):
    """Precondition or confirmation failure for configured restore preparation."""


class ConfiguredRestoreLockReleaseError(ConfiguredRestoreError):
    """Lock release failed during cleanup."""


@dataclass(frozen=True)
class ConfiguredRestorePreparationResult:
    """Frozen bounded result object representing successful Phase 6B3B1 preparation."""
    operation_id: str
    stage: RestoreStage
    selected_backup_id: str
    selected_backup_manifest_sha256: str
    safety_backup_id: str
    runtime_mode: str
    target_keys: tuple[str, ...]
    target_set_hash: str
    confirmation_value: str
    staged_artifact_count: int
    ready_for_future_apply: bool
    configured_database_mutated: bool
    locks_released: bool


def _verify_project_root(expected_application_commit: str) -> Path:
    """Enforce exact project-root execution boundary."""
    try:
        configured_root = safe_resolve(config.PROJECT_ROOT)
        cwd = Path.cwd().resolve()
    except (ValueError, OSError) as exc:
        raise ConfiguredRestorePreconditionError("Project root path verification failed") from exc

    if cwd != configured_root:
        raise ConfiguredRestorePreconditionError("Current working directory does not match configured project root")

    if has_symlink_component(config.PROJECT_ROOT) or has_symlink_component(Path.cwd()):
        raise ConfiguredRestorePreconditionError("Project root path cannot contain symlinks")

    try:
        toplevel_out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=configured_root,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        toplevel_str = toplevel_out.decode("utf-8") if isinstance(toplevel_out, bytes) else str(toplevel_out)
        toplevel_path = Path(toplevel_str).resolve()
        if toplevel_path != configured_root:
            raise ConfiguredRestorePreconditionError("Git repository top-level directory does not match project root")

        if _COMMIT_HEX_PATTERN.fullmatch(expected_application_commit):
            head_out = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=configured_root,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            git_head = head_out.decode("utf-8") if isinstance(head_out, bytes) else str(head_out)
            if git_head != expected_application_commit:
                raise ConfiguredRestorePreconditionError("Git HEAD commit does not match expected application commit")
    except (OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, ConfiguredRestorePreconditionError):
            raise
        raise ConfiguredRestorePreconditionError("Git execution boundary verification failed") from exc

    return configured_root


def _settle_journal_failed_safe(operation_id: str, root: Path) -> None:
    """Attempt to durably settle journal to FAILED_SAFE upon ordinary preparation failure."""
    try:
        journal = load_restore_journal(operation_id, root=root)
        if journal.stage in {
            RestoreStage.VERIFIED,
            RestoreStage.CURRENT_SNAPSHOT_CREATED,
            RestoreStage.RESTORE_STAGED,
            RestoreStage.STAGED_VERIFIED,
            RestoreStage.REPLACEMENT_READY,
        } and journal.final_result is None:
            update_restore_journal(operation_id, root=root, stage=RestoreStage.FAILED_SAFE)
    except Exception:
        pass


def prepare_configured_restore(
    *,
    selected_backup_id: str,
    expected_application_commit: str,
    confirmed_target_set_hash: str,
    confirmed_restore_value: str,
    operation_id: str | None = None,
) -> ConfiguredRestorePreparationResult:
    """Prepare a configured-runtime restore up to global stage REPLACEMENT_READY.
    
    All arguments are keyword-only. No caller-supplied roots are accepted.
    Re-evaluates and enforces mandatory confirmation values and exact project root.
    Releases all locks before returning a frozen result.
    Does NOT modify configured databases or sidecars.
    """
    # 1. Exact project-root verification
    project_root = _verify_project_root(expected_application_commit)
    backup_root = validate_backup_root(config.OPERATOR_BACKUP_ROOT)
    restore_root = validate_restore_root(config.OPERATOR_RESTORE_ROOT)

    if not _BACKUP_ID_PATTERN.fullmatch(selected_backup_id):
        raise ConfiguredRestorePreconditionError("Selected backup ID format is invalid")

    selected_backup_dir = backup_root / f"backup-{selected_backup_id}"
    if not selected_backup_dir.exists() or not selected_backup_dir.is_dir():
        raise ConfiguredRestorePreconditionError("Selected backup directory does not exist under backup root")

    # Discover canonical runtime database targets
    try:
        configured_targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredRestorePreconditionError("Canonical database target discovery failed") from exc

    if not configured_targets:
        raise ConfiguredRestorePreconditionError("No configured database targets discovered")

    for t in configured_targets:
        if t.required and not t.path.exists():
            raise ConfiguredRestorePreconditionError("Required configured database is missing")
        check = inspect_sqlite(t.path)
        if not check.readable or not check.quick_check_ok:
            raise ConfiguredRestorePreconditionError("Configured database failed read-only integrity preflight")

    # Load and strictly validate selected source backup snapshot
    try:
        selected_snapshot = load_validated_backup_snapshot(selected_backup_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredRestorePreconditionError("Selected backup verification failed") from exc

    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    target_keys = tuple(t.target_key for t in configured_targets)

    # Recompute target-set hash and confirmation value
    try:
        recomputed_target_hash = target_set_hash(
            backup_id=selected_backup_id,
            manifest_sha256=selected_snapshot.manifest_sha256,
            runtime_mode=runtime_mode,
            target_keys=target_keys,
        )
        recomputed_confirmation_value = confirmation_value(
            target_hash=recomputed_target_hash,
            expected_application_commit=expected_application_commit,
        )
    except RestorePlanError as exc:
        raise ConfiguredRestorePreconditionError("Target set hash or confirmation value calculation failed") from exc

    # Enforce exact mandatory confirmation boundary
    if confirmed_target_set_hash != recomputed_target_hash:
        raise ConfiguredRestorePreconditionError("Confirmed target-set hash mismatch")
    if confirmed_restore_value != recomputed_confirmation_value:
        raise ConfiguredRestorePreconditionError("Confirmed restore confirmation value mismatch")

    op_id = operation_id or f"restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    if not _OPERATION_ID_PATTERN.fullmatch(op_id):
        raise ConfiguredRestorePreconditionError("Operation ID format is invalid")

    # Preflight backup disk space (before acquiring locks)
    preflight_backup_disk_space(configured_targets, backup_root)

    # Capture destination baselines
    baselines = capture_destination_baselines(configured_targets)

    # Lock acquisition objects tracking for safe cleanup
    proc_lock: ProcessLock | None = None
    rest_lock: RestoreLock | None = None
    long_held_backup_lock: BackupLock | None = None
    is_reentry = False

    try:
        # Step A: Application ProcessLock acquisition
        try:
            proc_lock = acquire_process_lock(project_root / "garmincoach.lock")
        except Exception as exc:
            raise RestoreLockError("Could not acquire application process lock") from exc

        # Step B: Dedicated RestoreLock acquisition
        rest_lock = RestoreLock(restore_root)
        try:
            rest_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire dedicated restore lock") from exc

        # Step C: Journal creation or legal re-entry loading
        op_dir = restore_root / f"operation-{op_id}"
        journal_path = op_dir / "journal.json"

        if journal_path.exists():
            is_reentry = True
            journal = load_restore_journal(op_id, root=restore_root)
            if (
                journal.selected_backup_id != selected_backup_id
                or journal.selected_backup_manifest_sha256 != selected_snapshot.manifest_sha256
                or journal.expected_application_commit != expected_application_commit
                or journal.runtime_mode != runtime_mode
                or journal.target_keys != target_keys
                or journal.target_set_hash != recomputed_target_hash
                or journal.confirmation_value != recomputed_confirmation_value
            ):
                raise ConfiguredRestorePreconditionError("Re-entry journal parameters mismatch")

            if journal.stage in {
                RestoreStage.REPLACING,
                RestoreStage.REPLACED,
                RestoreStage.POSTCHECK_PASSED,
                RestoreStage.ROLLBACK_REQUIRED,
                RestoreStage.ROLLED_BACK,
                RestoreStage.FAILED_SAFE,
                RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
                RestoreStage.COMPLETED,
            }:
                raise ConfiguredRestorePreconditionError(f"Re-entry refused for journal stage '{journal.stage}'")
        else:
            plan = create_restore_plan(
                selected_backup_id=selected_backup_id,
                selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
                expected_application_commit=expected_application_commit,
                runtime_mode=runtime_mode,
                target_keys=target_keys,
            )
            journal = create_restore_journal(plan, root=restore_root, operation_id=op_id)
            journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED)

        # Step D: Public safety-backup creation & strict verification ordering
        safety_backup_id = journal.safety_backup_id
        if safety_backup_id is None:
            try:
                safety_backup_dir = create_verified_backup(output_root=backup_root)
            except Exception as exc:
                raise ConfiguredRestoreError("Ordinary public safety-backup creation failed") from exc

            s_id = safety_backup_dir.name.removeprefix("backup-")
            if s_id == selected_backup_id:
                raise ConfiguredRestoreError("Safety backup ID matches selected backup ID")

            # Strictly verify safety backup BEFORE recording in journal
            try:
                safety_snapshot = load_validated_backup_snapshot(safety_backup_dir, against_current_config=True)
            except BackupError as exc:
                raise ConfiguredRestoreError("Newly created safety backup failed verification") from exc

            if safety_snapshot.runtime_mode != runtime_mode or safety_snapshot.target_keys != target_keys:
                raise ConfiguredRestoreError("Safety backup runtime mode or target set mismatch")

            # ONLY NOW record safety_backup_id and transition to CURRENT_SNAPSHOT_CREATED
            safety_backup_id = s_id
            journal = update_restore_journal(
                op_id,
                root=restore_root,
                stage=RestoreStage.CURRENT_SNAPSHOT_CREATED,
                safety_backup_id=safety_backup_id,
            )
        else:
            safety_backup_dir = backup_root / f"backup-{safety_backup_id}"
            safety_snapshot = load_validated_backup_snapshot(safety_backup_dir, against_current_config=True)

        # Step E: Non-recursive long-held BackupLock acquisition
        try:
            long_held_backup_lock = BackupLock(backup_root)
            long_held_backup_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire long-held BackupLock") from exc

        # Step F: Preflight staging disk space under long-held BackupLock
        preflight_staging_disk_space(configured_targets, selected_snapshot)

        # Step G: Staging configured targets beside destinations with ownership bindings
        stage_result = stage_configured_targets(
            op_id,
            selected_snapshot,
            configured_targets,
            restore_root=restore_root,
        )

        # Step H: Complete readiness proof barrier
        verify_configured_readiness(
            op_id,
            selected_snapshot,
            configured_targets,
            stage_result,
            baselines,
            restore_root=restore_root,
        )

        # Step I: Global stage transition to REPLACEMENT_READY
        journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY)

        # Step J: Release all locks BEFORE returning result in reverse acquisition order
        # 1. BackupLock -> 2. RestoreLock -> 3. ProcessLock
        lock_release_errors = []

        if long_held_backup_lock is not None:
            try:
                long_held_backup_lock.__exit__(None, None, None)
            except Exception as exc:
                lock_release_errors.append(exc)
            long_held_backup_lock = None

        if rest_lock is not None:
            try:
                rest_lock.__exit__(None, None, None)
            except Exception as exc:
                lock_release_errors.append(exc)
            rest_lock = None

        if proc_lock is not None:
            try:
                release_process_lock(proc_lock)
            except Exception as exc:
                lock_release_errors.append(exc)
            proc_lock = None

        if lock_release_errors:
            raise ConfiguredRestoreLockReleaseError("Failed to release all acquired locks cleanly")

        return ConfiguredRestorePreparationResult(
            operation_id=op_id,
            stage=RestoreStage.REPLACEMENT_READY,
            selected_backup_id=selected_backup_id,
            selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
            safety_backup_id=safety_backup_id,
            runtime_mode=runtime_mode,
            target_keys=target_keys,
            target_set_hash=recomputed_target_hash,
            confirmation_value=recomputed_confirmation_value,
            staged_artifact_count=len(stage_result.staged_artifacts),
            ready_for_future_apply=True,
            configured_database_mutated=False,
            locks_released=True,
        )

    except Exception as exc:
        # Failure settlement & cleanup
        _settle_journal_failed_safe(op_id, restore_root)

        if long_held_backup_lock is not None:
            try:
                long_held_backup_lock.__exit__(None, None, None)
            except Exception:
                pass
        if rest_lock is not None:
            try:
                rest_lock.__exit__(None, None, None)
            except Exception:
                pass
        if proc_lock is not None:
            try:
                release_process_lock(proc_lock)
            except Exception:
                pass

        if isinstance(exc, (ConfiguredRestoreError, RestoreLockError)):
            raise
        raise ConfiguredRestoreError("Guarded restore preparation failed") from exc
