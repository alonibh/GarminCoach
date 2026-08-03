"""Configured-runtime restore orchestration through REPLACEMENT_READY (Phase 6B3B1).

Orchestrates configured-runtime restore preparation through global stage REPLACEMENT_READY:
1. Canonical configured target discovery
2. Project-root verification
3. Selected backup and confirmation verification
4. Application ProcessLock acquisition
5. Dedicated RestoreLock acquisition
6. Durable journal creation (PRECHECK -> VERIFIED)
7. Ordinary public safety-backup creation (create_verified_backup)
8. Strict safety-backup verification
9. Non-recursive long-held BackupLock acquisition
10. Configured-target staging and deep verification (RESTORE_STAGED -> STAGED_VERIFIED)
11. Disk-space preflight
12. Final read-only readiness proof
13. Global transition to REPLACEMENT_READY
14. Reverse lock release on exit

No configured database files or sidecars are modified or replaced by this module.
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
    ConfiguredStagingError,
    ConfiguredStagingResult,
    preflight_disk_space,
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


class ConfiguredRestoreError(RuntimeError):
    """Base error for configured restore orchestration failures."""


class ConfiguredRestorePreconditionError(ConfiguredRestoreError):
    """Precondition failure for configured restore preparation."""


def _commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=config.PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _verify_project_root(provided_root: Path | str | None = None) -> Path:
    target = Path(provided_root or config.PROJECT_ROOT).resolve()
    if has_symlink_component(target) or target.is_symlink():
        raise ConfiguredRestorePreconditionError("Project root cannot contain symlinks")
    if not target.exists() or not target.is_dir():
        raise ConfiguredRestorePreconditionError("Project root is invalid or missing")
    return target


@dataclass
class ConfiguredRestoreContext:
    """Active context holding acquired locks and preparation artifacts up to REPLACEMENT_READY."""
    operation_id: str
    journal: RestoreJournal
    selected_backup_id: str
    safety_backup_id: str
    selected_snapshot: ValidatedBackupSnapshot
    safety_snapshot: ValidatedBackupSnapshot
    staging_result: ConfiguredStagingResult
    process_lock: ProcessLock | None = None
    restore_lock: RestoreLock | None = None
    backup_lock: BackupLock | None = None
    released: bool = False

    def close(self) -> None:
        """Release locks in reverse acquisition order: BackupLock -> RestoreLock -> ProcessLock."""
        if self.released:
            return
        
        # 1. Release long-held BackupLock
        if self.backup_lock is not None:
            try:
                self.backup_lock.__exit__(None, None, None)
            except Exception:
                pass
            self.backup_lock = None

        # 2. Release dedicated RestoreLock
        if self.restore_lock is not None:
            try:
                self.restore_lock.__exit__(None, None, None)
            except Exception:
                pass
            self.restore_lock = None

        # 3. Release application ProcessLock
        if self.process_lock is not None:
            try:
                release_process_lock(self.process_lock)
            except Exception:
                pass
            self.process_lock = None

        self.released = True

    def __enter__(self) -> ConfiguredRestoreContext:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def prepare_configured_restore(
    selected_backup_id: str,
    *,
    expected_application_commit: str | None = None,
    operation_id: str | None = None,
    backup_root: Path | str | None = None,
    restore_root: Path | str | None = None,
    project_root: Path | str | None = None,
) -> ConfiguredRestoreContext:
    """Prepare a configured-runtime restore up to global stage REPLACEMENT_READY.
    
    This function performs all non-mutating preparation, validation, lock acquisitions,
    public safety-backup creation, and target staging.
    
    Configured database files and sidecars are NOT modified.
    """
    # 1. Verification of paths & preconditions
    proj_root = _verify_project_root(project_root)
    b_root = validate_backup_root(backup_root)
    r_root = validate_restore_root(restore_root)

    if not _BACKUP_ID_PATTERN.fullmatch(selected_backup_id):
        raise ConfiguredRestorePreconditionError(f"Invalid selected backup ID format: '{selected_backup_id}'")

    selected_backup_dir = b_root / f"backup-{selected_backup_id}"
    if not selected_backup_dir.exists() or not selected_backup_dir.is_dir():
        raise ConfiguredRestorePreconditionError(f"Selected backup '{selected_backup_id}' does not exist under backup root")

    # Discover current configured database targets
    try:
        configured_targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredRestorePreconditionError("Canonical database target discovery failed") from exc

    if not configured_targets:
        raise ConfiguredRestorePreconditionError("No configured database targets found")

    for t in configured_targets:
        if t.required and not t.path.exists():
            raise ConfiguredRestorePreconditionError(f"Required configured database '{t.target_key}' is missing")
        check = inspect_sqlite(t.path)
        if not check.readable or not check.quick_check_ok:
            raise ConfiguredRestorePreconditionError(f"Configured database '{t.target_key}' failed read-only integrity inspection")

    # Load and strictly validate selected source backup
    try:
        selected_snapshot = load_validated_backup_snapshot(selected_backup_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredRestorePreconditionError(f"Selected backup verification failed: {exc}") from exc

    commit_sha = expected_application_commit or _commit()
    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    target_keys = tuple(t.target_key for t in configured_targets)

    # Compute target set hash & confirmation value
    try:
        t_hash = target_set_hash(
            backup_id=selected_backup_id,
            manifest_sha256=selected_snapshot.manifest_sha256,
            runtime_mode=runtime_mode,
            target_keys=target_keys,
        )
        c_val = confirmation_value(
            target_hash=t_hash,
            expected_application_commit=commit_sha,
        )
        plan = create_restore_plan(
            selected_backup_id=selected_backup_id,
            selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
            expected_application_commit=commit_sha,
            runtime_mode=runtime_mode,
            target_keys=target_keys,
        )
    except RestorePlanError as exc:
        raise ConfiguredRestorePreconditionError(f"Restore planning failed: {exc}") from exc

    op_id = operation_id or f"restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"

    # Lock acquisition objects tracking for safe cleanup on failure
    process_lock: ProcessLock | None = None
    restore_lock: RestoreLock | None = None
    long_held_backup_lock: BackupLock | None = None

    try:
        # Step A: Application ProcessLock acquisition
        try:
            process_lock = acquire_process_lock(proj_root / "garmincoach.lock")
        except Exception as exc:
            raise RestoreLockError("Could not acquire application process lock (garmincoach.lock)") from exc

        # Step B: Dedicated RestoreLock acquisition
        restore_lock = RestoreLock(r_root)
        try:
            restore_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire dedicated restore lock (.garmincoach-restore.lock)") from exc

        # Step C: Journal creation (stage PRECHECK -> VERIFIED)
        journal = create_restore_journal(plan, root=r_root, operation_id=op_id)
        journal = update_restore_journal(op_id, root=r_root, stage=RestoreStage.VERIFIED)

        # Step D: Public safety-backup creation (create_verified_backup acquires and releases BackupLock internally)
        try:
            safety_backup_dir = create_verified_backup(output_root=b_root)
        except Exception as exc:
            raise ConfiguredRestoreError(f"Ordinary public safety-backup creation failed: {exc}") from exc

        safety_backup_id = safety_backup_dir.name.removeprefix("backup-")
        journal = update_restore_journal(
            op_id,
            root=r_root,
            stage=RestoreStage.CURRENT_SNAPSHOT_CREATED,
            safety_backup_id=safety_backup_id,
        )

        # Step E: Strict safety-backup verification
        try:
            safety_snapshot = load_validated_backup_snapshot(safety_backup_dir, against_current_config=True)
        except BackupError as exc:
            raise ConfiguredRestoreError(f"Safety-backup verification failed: {exc}") from exc

        # Step F: Non-recursive long-held BackupLock acquisition
        # ONLY AFTER public safety-backup creation completed and released BackupLock, acquire BackupLock nonblockingly.
        try:
            long_held_backup_lock = BackupLock(b_root)
            long_held_backup_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire long-held BackupLock after safety backup creation") from exc

        # Step G: Disk-space preflight check
        op_dir = r_root / f"operation-{op_id}"
        preflight_disk_space(configured_targets, op_dir)

        # Step H: Configured-target staging & deep verification
        staging_result = stage_configured_targets(
            op_id,
            selected_snapshot,
            restore_root=r_root,
        )

        # Step I: Final read-only readiness proof
        verify_configured_readiness(
            op_id,
            selected_snapshot,
            configured_targets,
            staging_result,
            restore_root=r_root,
        )

        # Step J: Transition to REPLACEMENT_READY
        journal = update_restore_journal(op_id, root=r_root, stage=RestoreStage.REPLACEMENT_READY)

        ctx = ConfiguredRestoreContext(
            operation_id=op_id,
            journal=journal,
            selected_backup_id=selected_backup_id,
            safety_backup_id=safety_backup_id,
            selected_snapshot=selected_snapshot,
            safety_snapshot=safety_snapshot,
            staging_result=staging_result,
            process_lock=process_lock,
            restore_lock=restore_lock,
            backup_lock=long_held_backup_lock,
        )
        return ctx

    except Exception:
        # Cleanup locks in reverse order on failure
        if long_held_backup_lock is not None:
            try:
                long_held_backup_lock.__exit__(None, None, None)
            except Exception:
                pass
        if restore_lock is not None:
            try:
                restore_lock.__exit__(None, None, None)
            except Exception:
                pass
        if process_lock is not None:
            try:
                release_process_lock(process_lock)
            except Exception:
                pass
        raise
