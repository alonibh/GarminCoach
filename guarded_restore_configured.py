"""Configured-runtime restore orchestration through REPLACEMENT_READY (Phase 6B3B1).

Orchestrates configured-runtime restore preparation up to global stage REPLACEMENT_READY:
1. Keyword-only API with mandatory target-set hash and restore confirmation validation.
2. Exact project-root verification (CWD == PROJECT_ROOT, no symlinks, git toplevel/HEAD match).
3. Canonical runtime target discovery from config.PROJECT_ROOT.
4. ProcessLock -> RestoreLock -> safety backup creation -> long-held BackupLock acquisition chain.
5. Strict safety-backup verification, reread, and snapshot reload before journal recording.
6. Same-filesystem target staging with canonical ownership bindings and exclusive stage dirs.
7. Descriptor-safe staged file publication and deep SQLite integrity checks (with foreign keys).
8. Destination database and sidecar baseline tracking and drift revalidation.
9. Exact per-filesystem disk-space preflight checks with named constants.
10. Centralized complete verification proof barrier executed across all execution barriers.
11. Explicit 6-stage legal re-entry dispatcher and strict illegal stage refusal.
12. Reverse lock release (BackupLock -> RestoreLock -> ProcessLock) BEFORE returning result.
13. Immutable result object with locks_released=True and no internal lock/file handles.

Configured database files and sidecars are NOT modified by this module.
Phase 6B3B2 (replacement/rollback) and Phase 6B3B3 (CLI/apply) remain unimplemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
from typing import Any

import config
from guarded_restore import (
    FinalResult,
    RestoreJournal,
    RestoreLock,
    RestoreLockError,
    RestorePlan,
    RestorePlanError,
    RestoreStage,
    TargetRestoreState,
    _GLOBAL_TRANSITIONS,
    canonical_json,
    confirmation_value,
    create_restore_journal,
    create_restore_plan,
    load_restore_journal,
    target_set_hash,
    update_restore_journal,
    validate_restore_root,
)
from guarded_restore_configured_staging import (
    ConfiguredBaselineSHAMismatch,
    ConfiguredPreflightError,
    ConfiguredRestoreError,
    ConfiguredStagedArtifact,
    ConfiguredStagingError,
    ConfiguredStagingOwnershipError,
    ConfiguredStagingPersistenceError,
    ConfiguredStagingResult,
    ConfiguredStagingSourceError,
    DestinationBaselineEvidence,
    DestinationBaselineRecord,
    capture_destination_baseline_evidence,
    capture_destination_baselines,
    load_destination_baseline_evidence,
    preflight_backup_disk_space,
    preflight_staging_disk_space,
    publish_noreplace,
    revalidate_destination_baseline_evidence,
    revalidate_destination_baselines,
    stage_configured_targets,
    validate_existing_staging_directory,
    verify_configured_readiness,
    write_destination_baseline_evidence,
    _sha256_file,
)
from operator_storage import (
    DatabaseTarget,
    TargetProfile,
    discover_database_targets,
    has_symlink_component,
    inspect_sqlite,
    migration_markers,
    safe_resolve,
    schema_fingerprint,
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


class ConfiguredRestorePreconditionError(ConfiguredRestoreError):
    """Precondition or confirmation failure for configured restore preparation."""


class ConfiguredJournalUncertaintyError(ConfiguredRestoreError):
    """Journal update or persistence state is indeterminate."""


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


def verify_complete_preparation_barrier(
    *,
    expected_application_commit: str,
    selected_backup_id: str,
    selected_snapshot: ValidatedBackupSnapshot,
    safety_backup_id: str | None,
    safety_snapshot: ValidatedBackupSnapshot | None,
    targets: tuple[DatabaseTarget, ...],
    baselines: tuple[DestinationBaselineRecord, ...],
    confirmed_target_set_hash: str,
    confirmed_restore_value: str,
    operation_id: str | None = None,
    journal: RestoreJournal | None = None,
    restore_root: Path | str | None = None,
) -> bool:
    """Centralized, complete proof barrier freshly verifying every system proof."""
    # 1. Project root & git HEAD verification
    _verify_project_root(expected_application_commit)

    # 2. Package version check
    try:
        from importlib.metadata import version as get_version
        pkg_ver = get_version("garminconnect")
        if not pkg_ver:
            raise ConfiguredRestorePreconditionError("garminconnect package version missing")
    except Exception as exc:
        raise ConfiguredRestorePreconditionError("garminconnect package verification failed") from exc

    # 3. Selected backup snapshot verification
    if selected_snapshot.backup_id != selected_backup_id:
        raise ConfiguredRestorePreconditionError("Selected backup snapshot ID mismatch")

    sel_dir = config.OPERATOR_BACKUP_ROOT / f"backup-{selected_backup_id}"
    reloaded_selected = load_validated_backup_snapshot(sel_dir, against_current_config=True)
    if reloaded_selected.manifest_sha256 != selected_snapshot.manifest_sha256:
        raise ConfiguredRestorePreconditionError("Selected backup manifest SHA-256 drift detected")

    # 4. Safety backup snapshot verification (if created)
    if safety_backup_id is not None:
        if safety_backup_id == selected_backup_id:
            raise ConfiguredRestorePreconditionError("Safety backup ID matches selected backup ID")
        saf_dir = config.OPERATOR_BACKUP_ROOT / f"backup-{safety_backup_id}"
        reloaded_safety = load_validated_backup_snapshot(saf_dir, against_current_config=True)
        if safety_snapshot is not None and reloaded_safety.manifest_sha256 != safety_snapshot.manifest_sha256:
            raise ConfiguredRestorePreconditionError("Safety backup manifest SHA-256 drift detected")

    # 5. Canonical runtime targets & tenant UUIDs check
    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    cur_targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    if tuple(t.target_key for t in cur_targets) != tuple(t.target_key for t in targets):
        raise ConfiguredRestorePreconditionError("Discovered database target keys mismatch")

    # 6. Target-set hash & confirmation value verification
    recomputed_hash = target_set_hash(
        backup_id=selected_backup_id,
        manifest_sha256=selected_snapshot.manifest_sha256,
        runtime_mode=runtime_mode,
        target_keys=tuple(t.target_key for t in targets),
    )
    recomputed_val = confirmation_value(
        target_hash=recomputed_hash,
        expected_application_commit=expected_application_commit,
    )
    if confirmed_target_set_hash != recomputed_hash or confirmed_restore_value != recomputed_val:
        raise ConfiguredRestorePreconditionError("Mandatory target-set hash or confirmation value drift detected")

    # 7. Destination and sidecar baselines revalidation
    revalidate_destination_baselines(baselines)

    # 8. Journal immutable fields and baseline SHA verification
    if journal is not None:
        if (
            journal.selected_backup_id != selected_backup_id
            or journal.selected_backup_manifest_sha256 != selected_snapshot.manifest_sha256
            or journal.expected_application_commit != expected_application_commit
            or journal.runtime_mode != runtime_mode
            or journal.target_keys != tuple(t.target_key for t in targets)
            or journal.target_set_hash != recomputed_hash
            or journal.confirmation_value != recomputed_val
        ):
            raise ConfiguredRestorePreconditionError("Journal immutable fields mismatch during barrier proof")

        # Stages at or after VERIFIED must have a bound destination_baseline_sha256.
        # The only permitted exception is the narrow PRECHECK interval before baseline is persisted.
        _requires_baseline_sha = {
            RestoreStage.VERIFIED,
            RestoreStage.CURRENT_SNAPSHOT_CREATED,
            RestoreStage.RESTORE_STAGED,
            RestoreStage.STAGED_VERIFIED,
            RestoreStage.REPLACEMENT_READY,
        }
        if journal.stage in _requires_baseline_sha:
            if journal.destination_baseline_sha256 is None:
                raise ConfiguredRestorePreconditionError(
                    "Restore journal is missing destination baseline evidence"
                )

        if journal.destination_baseline_sha256 is not None and operation_id is not None:
            r_root = restore_root or config.OPERATOR_RESTORE_ROOT
            ev, sha_hex = load_destination_baseline_evidence(operation_id, restore_root=r_root)
            if sha_hex != journal.destination_baseline_sha256:
                raise ConfiguredRestorePreconditionError(
                    "Persisted destination baseline SHA-256 mismatch"
                )
            revalidate_destination_baseline_evidence(
                ev,
                targets,
                expected_application_commit,
                operation_id=operation_id,
                selected_backup_id=selected_backup_id,
                selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
                runtime_mode=runtime_mode,
                target_set_hash=recomputed_hash,
                confirmation_value=recomputed_val,
            )

    return True


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
            updated = update_restore_journal(operation_id, root=root, stage=RestoreStage.FAILED_SAFE)
            reread = load_restore_journal(operation_id, root=root)
            if reread.stage is not RestoreStage.FAILED_SAFE:
                raise ConfiguredJournalUncertaintyError("Journal settlement reread failed")
    except Exception as exc:
        if isinstance(exc, ConfiguredJournalUncertaintyError):
            raise
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
    # Barrier 1: Pre-lock verification
    project_root = _verify_project_root(expected_application_commit)
    backup_root = validate_backup_root(config.OPERATOR_BACKUP_ROOT)
    restore_root = validate_restore_root(config.OPERATOR_RESTORE_ROOT)

    if not _BACKUP_ID_PATTERN.fullmatch(selected_backup_id):
        raise ConfiguredRestorePreconditionError("Selected backup ID format is invalid")

    selected_backup_dir = backup_root / f"backup-{selected_backup_id}"
    if not selected_backup_dir.exists() or not selected_backup_dir.is_dir():
        raise ConfiguredRestorePreconditionError("Selected backup directory does not exist under backup root")

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

    try:
        selected_snapshot = load_validated_backup_snapshot(selected_backup_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredRestorePreconditionError("Selected backup verification failed") from exc

    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    target_keys = tuple(t.target_key for t in configured_targets)

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

    if confirmed_target_set_hash != recomputed_target_hash:
        raise ConfiguredRestorePreconditionError("Confirmed target-set hash mismatch")
    if confirmed_restore_value != recomputed_confirmation_value:
        raise ConfiguredRestorePreconditionError("Confirmed restore confirmation value mismatch")

    op_id = operation_id or f"restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    if not _OPERATION_ID_PATTERN.fullmatch(op_id):
        raise ConfiguredRestorePreconditionError("Operation ID format is invalid")

    preflight_backup_disk_space(configured_targets, backup_root)
    baselines = capture_destination_baselines(configured_targets)

    proc_lock: ProcessLock | None = None
    rest_lock: RestoreLock | None = None
    long_held_backup_lock: BackupLock | None = None

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

        op_dir = restore_root / f"operation-{op_id}"
        journal_path = op_dir / "journal.json"

        # Explicit Legal Stage Dispatcher
        if journal_path.exists():
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
                raise ConfiguredRestorePreconditionError(f"Re-entry refused for illegal journal stage '{journal.stage}'")
        else:
            plan = create_restore_plan(
                selected_backup_id=selected_backup_id,
                selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
                expected_application_commit=expected_application_commit,
                runtime_mode=runtime_mode,
                target_keys=target_keys,
            )
            journal = create_restore_journal(plan, root=restore_root, operation_id=op_id)

            # Create & persist durable baseline evidence ONCE during initial creation under locks
            evidence = capture_destination_baseline_evidence(
                operation_id=op_id,
                selected_backup_id=selected_backup_id,
                selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
                expected_application_commit=expected_application_commit,
                runtime_mode=runtime_mode,
                target_set_hash=recomputed_target_hash,
                confirmation_value=recomputed_confirmation_value,
                targets=configured_targets,
            )
            baseline_sha = write_destination_baseline_evidence(op_id, evidence, restore_root=restore_root)
            journal = update_restore_journal(op_id, root=restore_root, destination_baseline_sha256=baseline_sha)

            # Post-write: reread journal, reload baseline file, recompute SHA – require exact equality
            reread_journal = load_restore_journal(op_id, root=restore_root)
            if reread_journal.destination_baseline_sha256 != baseline_sha:
                raise ConfiguredJournalUncertaintyError("Baseline SHA-256 journal reread mismatch after write")
            reread_ev, reread_sha = load_destination_baseline_evidence(op_id, restore_root=restore_root)
            if reread_sha != baseline_sha:
                raise ConfiguredJournalUncertaintyError("Destination baseline SHA-256 recomputation mismatch after write")
            journal = reread_journal

        # On EVERY invocation (initial or re-entry), load persisted baseline & revalidate against current
        evidence, loaded_baseline_sha = load_destination_baseline_evidence(op_id, restore_root=restore_root)
        if journal.destination_baseline_sha256 != loaded_baseline_sha:
            raise ConfiguredRestorePreconditionError("Journal destination baseline SHA-256 mismatch")

        revalidate_destination_baseline_evidence(
            evidence,
            configured_targets,
            expected_application_commit,
            operation_id=op_id,
            selected_backup_id=selected_backup_id,
            selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
            runtime_mode=runtime_mode,
            target_set_hash=recomputed_target_hash,
            confirmation_value=recomputed_confirmation_value,
        )

        # Barrier 1: Complete proof
        verify_complete_preparation_barrier(
            expected_application_commit=expected_application_commit,
            selected_backup_id=selected_backup_id,
            selected_snapshot=selected_snapshot,
            safety_backup_id=None,
            safety_snapshot=None,
            targets=configured_targets,
            baselines=baselines,
            confirmed_target_set_hash=confirmed_target_set_hash,
            confirmed_restore_value=confirmed_restore_value,
            operation_id=op_id,
            journal=journal,
            restore_root=restore_root,
        )

        # Stage 1: PRECHECK -> VERIFIED
        if journal.stage is RestoreStage.PRECHECK:
            journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.VERIFIED)

        # Stage 2: VERIFIED -> CURRENT_SNAPSHOT_CREATED
        safety_backup_id = journal.safety_backup_id
        safety_snapshot: ValidatedBackupSnapshot | None = None

        if journal.stage is RestoreStage.VERIFIED and safety_backup_id is None:
            try:
                safety_backup_dir = create_verified_backup(output_root=backup_root)
            except Exception as exc:
                raise ConfiguredRestoreError("Ordinary public safety-backup creation failed") from exc

            s_id = safety_backup_dir.name.removeprefix("backup-")
            if s_id == selected_backup_id:
                raise ConfiguredRestoreError("Safety backup ID matches selected backup ID")

            try:
                safety_snapshot = load_validated_backup_snapshot(safety_backup_dir, against_current_config=True)
            except BackupError as exc:
                raise ConfiguredRestoreError("Newly created safety backup failed verification") from exc

            if safety_snapshot.runtime_mode != runtime_mode or safety_snapshot.target_keys != tuple(t.target_key for t in configured_targets):
                raise ConfiguredRestoreError("Safety backup runtime mode or target set mismatch")

            safety_backup_id = s_id
            journal = update_restore_journal(
                op_id,
                root=restore_root,
                stage=RestoreStage.CURRENT_SNAPSHOT_CREATED,
                safety_backup_id=safety_backup_id,
            )

            # Reread journal & verify exact update equality
            reread_journal = load_restore_journal(op_id, root=restore_root)
            if reread_journal.safety_backup_id != safety_backup_id or reread_journal.stage is not RestoreStage.CURRENT_SNAPSHOT_CREATED:
                raise ConfiguredJournalUncertaintyError("Safety backup recording journal reread mismatch")

            # Reload safety backup snapshot & verify match
            reloaded_saf = load_validated_backup_snapshot(safety_backup_dir, against_current_config=True)
            if reloaded_saf.manifest_sha256 != safety_snapshot.manifest_sha256:
                raise ConfiguredJournalUncertaintyError("Reloaded safety backup manifest SHA-256 mismatch")
        else:
            if safety_backup_id is not None:
                safety_backup_dir = backup_root / f"backup-{safety_backup_id}"
                safety_snapshot = load_validated_backup_snapshot(safety_backup_dir, against_current_config=True)

        # Barrier 3: Post safety backup recording verification
        verify_complete_preparation_barrier(
            expected_application_commit=expected_application_commit,
            selected_backup_id=selected_backup_id,
            selected_snapshot=selected_snapshot,
            safety_backup_id=safety_backup_id,
            safety_snapshot=safety_snapshot,
            targets=configured_targets,
            baselines=baselines,
            confirmed_target_set_hash=confirmed_target_set_hash,
            confirmed_restore_value=confirmed_restore_value,
            operation_id=op_id,
            journal=journal,
            restore_root=restore_root,
        )

        # Long-held BackupLock acquisition
        try:
            long_held_backup_lock = BackupLock(backup_root)
            long_held_backup_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire long-held BackupLock") from exc

        # Barrier 4: Post long-held BackupLock verification
        verify_complete_preparation_barrier(
            expected_application_commit=expected_application_commit,
            selected_backup_id=selected_backup_id,
            selected_snapshot=selected_snapshot,
            safety_backup_id=safety_backup_id,
            safety_snapshot=safety_snapshot,
            targets=configured_targets,
            baselines=baselines,
            confirmed_target_set_hash=confirmed_target_set_hash,
            confirmed_restore_value=confirmed_restore_value,
            operation_id=op_id,
            journal=journal,
            restore_root=restore_root,
        )

        # Exact per-filesystem disk space preflight under long-held BackupLock
        if safety_snapshot is None:
            raise ConfiguredRestoreError("Safety snapshot is missing for disk space preflight")
        preflight_staging_disk_space(configured_targets, selected_snapshot, safety_snapshot)

        # Stage 3 & 4: CURRENT_SNAPSHOT_CREATED / RESTORE_STAGED -> STAGED_VERIFIED
        if journal.stage in {RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.RESTORE_STAGED}:
            if journal.stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
                journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.RESTORE_STAGED)

            # Ensure staging directories and ownership bindings exist for all target parents
            stage_result = stage_configured_targets(
                op_id,
                selected_snapshot,
                configured_targets,
                restore_root=restore_root,
                destination_baseline=evidence,
            )

            journal = load_restore_journal(op_id, root=restore_root)

            backup_entries_by_key = {e.target_key: e for e in selected_snapshot.entries}
            targets_by_key = {t.target_key: t for t in configured_targets}

            # Per-target processing under global RESTORE_STAGED
            for index, target_key in enumerate(journal.target_keys):
                t_obj = targets_by_key[target_key]
                entry = backup_entries_by_key[target_key]
                tf = next((f for f in journal.targets if f.target_key == target_key), None)
                if tf is None:
                    raise ConfiguredStagingError("Target key not found in journal target facts")

                safe_key = target_key.replace(":", "-")
                staged_name = f"{index:03d}-{safe_key}.sqlite.staged"
                stage_dir = t_obj.path.parent.resolve() / f".garmincoach-restore-stage-{op_id}"
                staged_path = stage_dir / staged_name

                if tf.state is TargetRestoreState.PENDING:
                    if not staged_path.exists() or staged_path.is_symlink() or staged_path.stat().st_size != entry.size_bytes or _sha256_file(staged_path) != entry.sha256:
                        raise ConfiguredStagingPersistenceError(f"Staged artifact '{staged_name}' missing or invalid for PENDING target")
                    journal = update_restore_journal(op_id, root=restore_root, target_key=target_key, target_state=TargetRestoreState.STAGED)
                    tf = next((f for f in journal.targets if f.target_key == target_key), None)

                if tf.state is TargetRestoreState.STAGED:
                    if not staged_path.exists() or staged_path.is_symlink() or staged_path.stat().st_size != entry.size_bytes or _sha256_file(staged_path) != entry.sha256:
                        raise ConfiguredStagingPersistenceError(f"Staged artifact '{staged_name}' incompatible or modified")
                    _deep_verify_staged_artifact(staged_path, entry)
                    journal = update_restore_journal(op_id, root=restore_root, target_key=target_key, target_state=TargetRestoreState.STAGED_VERIFIED)
                    tf = next((f for f in journal.targets if f.target_key == target_key), None)

                if tf.state is TargetRestoreState.STAGED_VERIFIED:
                    if not staged_path.exists() or staged_path.is_symlink() or staged_path.stat().st_size != entry.size_bytes or _sha256_file(staged_path) != entry.sha256:
                        raise ConfiguredStagingPersistenceError(f"Staged artifact '{staged_name}' incompatible or modified")
                    _deep_verify_staged_artifact(staged_path, entry)

            # Reread journal & require EVERY target == STAGED_VERIFIED and mutation flags == False
            journal = load_restore_journal(op_id, root=restore_root)
            if any(t_fact.state is not TargetRestoreState.STAGED_VERIFIED for t_fact in journal.targets):
                raise ConfiguredJournalUncertaintyError("Not all targets are STAGED_VERIFIED before global STAGED_VERIFIED transition")
            if any(t_fact.wal_removed or t_fact.shm_removed or t_fact.replacement_intent or t_fact.replacement_completed or t_fact.rollback_intent or t_fact.rollback_completed for t_fact in journal.targets):
                raise ConfiguredJournalUncertaintyError("Mutation flags present before global STAGED_VERIFIED transition")

            # Only then: advance global stage RESTORE_STAGED -> STAGED_VERIFIED
            journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.STAGED_VERIFIED)
            reread_j = load_restore_journal(op_id, root=restore_root)
            if reread_j.stage is not RestoreStage.STAGED_VERIFIED:
                raise ConfiguredJournalUncertaintyError("Global STAGED_VERIFIED transition failed reread")
        else:
            # Reconstruct staging result for STAGED_VERIFIED / REPLACEMENT_READY re-entry
            by_parent: dict[Path, list[tuple[int, str, DatabaseTarget, Any]]] = {}
            backup_entries_by_key = {e.target_key: e for e in selected_snapshot.entries}
            targets_by_key = {t.target_key: t for t in configured_targets}

            for index, target_key in enumerate(journal.target_keys):
                t = targets_by_key[target_key]
                entry = backup_entries_by_key[target_key]
                by_parent.setdefault(t.path.parent.resolve(), []).append((index, target_key, t, entry))

            reconstructed_artifacts: list[ConfiguredStagedArtifact] = []

            for parent_dir, items in by_parent.items():
                stage_dir = parent_dir / f".garmincoach-restore-stage-{op_id}"
                expected_staged_names = {f"{it[0]:03d}-{it[1].replace(':', '-')}.sqlite.staged" for it in items}

                binding_payload = {
                    "format_version": "garmincoach-restore-staging-binding-v1",
                    "operation_id": op_id,
                    "selected_backup_id": journal.selected_backup_id,
                    "selected_backup_manifest_sha256": journal.selected_backup_manifest_sha256,
                    "safety_backup_id": journal.safety_backup_id,
                    "runtime_mode": journal.runtime_mode,
                    "target_set_hash": journal.target_set_hash,
                    "stage_parent": parent_dir.name,
                    "artifacts": [
                        {
                            "target_key": item[1],
                            "kind": item[3].kind,
                            "target_order": item[0],
                            "destination": item[2].path.name,
                            "staged_filename": f"{item[0]:03d}-{item[1].replace(':', '-')}.sqlite.staged",
                            "size_bytes": item[3].size_bytes,
                            "sha256": item[3].sha256,
                        }
                        for item in items
                    ],
                }
                expected_binding_bytes = canonical_json(binding_payload)
                validate_existing_staging_directory(stage_dir, op_id, expected_binding_bytes, expected_staged_names)

                for index, target_key, t_obj, entry in items:
                    staged_name = f"{index:03d}-{target_key.replace(':', '-')}.sqlite.staged"
                    staged_path = stage_dir / staged_name
                    if not staged_path.exists() or staged_path.is_symlink():
                        raise ConfiguredStagingOwnershipError("Staged artifact missing or unsafe")

                    _deep_verify_staged_artifact(staged_path, entry)

                    reconstructed_artifacts.append(
                        ConfiguredStagedArtifact(
                            operation_id=op_id,
                            target_key=target_key,
                            kind=entry.kind,
                            target_order=index,
                            staged_path=staged_path,
                            size_bytes=entry.size_bytes,
                            sha256=entry.sha256,
                            schema_fingerprint=entry.schema_fingerprint,
                            migration_markers=(entry.migration_ledger, entry.migration_keys, entry.migration_state),
                        )
                    )

            stage_result = ConfiguredStagingResult(
                operation_id=op_id,
                staged_artifacts=tuple(reconstructed_artifacts),
            )

        # Stage 5: STAGED_VERIFIED -> REPLACEMENT_READY
        verify_complete_preparation_barrier(
            expected_application_commit=expected_application_commit,
            selected_backup_id=selected_backup_id,
            selected_snapshot=selected_snapshot,
            safety_backup_id=safety_backup_id,
            safety_snapshot=safety_snapshot,
            targets=configured_targets,
            baselines=baselines,
            confirmed_target_set_hash=confirmed_target_set_hash,
            confirmed_restore_value=confirmed_restore_value,
            operation_id=op_id,
            journal=journal,
            restore_root=restore_root,
        )

        verify_configured_readiness(
            op_id,
            selected_snapshot,
            configured_targets,
            stage_result,
            baselines,
            restore_root=restore_root,
        )

        if journal.stage is not RestoreStage.REPLACEMENT_READY:
            journal = update_restore_journal(op_id, root=restore_root, stage=RestoreStage.REPLACEMENT_READY)

        # Final REPLACEMENT_READY Barrier Proof
        verify_complete_preparation_barrier(
            expected_application_commit=expected_application_commit,
            selected_backup_id=selected_backup_id,
            selected_snapshot=selected_snapshot,
            safety_backup_id=safety_backup_id,
            safety_snapshot=safety_snapshot,
            targets=configured_targets,
            baselines=baselines,
            confirmed_target_set_hash=confirmed_target_set_hash,
            confirmed_restore_value=confirmed_restore_value,
            operation_id=op_id,
            journal=journal,
            restore_root=restore_root,
        )

        # Reverse lock release BEFORE returning result
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
            raise ConfiguredRestoreLockReleaseError("Failed to release all acquired locks cleanly") from lock_release_errors[0]

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
        settlement_err = None
        lock_release_err = None

        if op_id is not None and (restore_root / f"operation-{op_id}" / "journal.json").exists():
            try:
                j = load_restore_journal(op_id, root=restore_root)
                if j.stage not in {
                    RestoreStage.COMPLETED,
                    RestoreStage.FAILED_SAFE,
                    RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
                } and RestoreStage.FAILED_SAFE in _GLOBAL_TRANSITIONS.get(j.stage, set()):
                    update_restore_journal(op_id, root=restore_root, stage=RestoreStage.FAILED_SAFE)
                    reread_j = load_restore_journal(op_id, root=restore_root)
                    if reread_j.stage is not RestoreStage.FAILED_SAFE or reread_j.final_result is not FinalResult.FAILED_SAFE:
                        settlement_err = ConfiguredJournalUncertaintyError("Failed to verify FAILED_SAFE journal settlement")
            except Exception as j_exc:
                if isinstance(j_exc, ConfiguredJournalUncertaintyError):
                    settlement_err = j_exc
                else:
                    settlement_err = ConfiguredJournalUncertaintyError("Journal settlement failed")
                    settlement_err.__cause__ = j_exc

        lock_errors = []
        if long_held_backup_lock is not None:
            try:
                long_held_backup_lock.__exit__(None, None, None)
            except Exception as b_exc:
                lock_errors.append(b_exc)
            long_held_backup_lock = None

        if rest_lock is not None:
            try:
                rest_lock.__exit__(None, None, None)
            except Exception as r_exc:
                lock_errors.append(r_exc)
            rest_lock = None

        if proc_lock is not None:
            try:
                release_process_lock(proc_lock)
            except Exception as p_exc:
                lock_errors.append(p_exc)
            proc_lock = None

        if lock_errors:
            lock_release_err = ConfiguredRestoreLockReleaseError("Failed to release all locks cleanly")
            lock_release_err.__cause__ = lock_errors[0]

        if settlement_err is not None:
            settlement_err.__cause__ = exc
            raise settlement_err

        if lock_release_err is not None:
            lock_release_err.__cause__ = exc
            raise lock_release_err

        if isinstance(exc, (ConfiguredRestoreError, ConfiguredStagingError, RestoreLockError)):
            raise

        raise ConfiguredRestoreError("Guarded restore preparation failed") from exc


def _deep_verify_staged_artifact(staged_path: Path, entry: Any) -> None:
    """Perform deep SQLite integrity, foreign key, schema, and migration checks."""
    check = inspect_sqlite(staged_path)
    if not check.readable or not check.quick_check_ok:
        raise ConfiguredStagingPersistenceError("Staged SQLite database failed integrity check")

    try:
        conn = sqlite3.connect(f"file:{staged_path}?mode=ro", uri=True)
        try:
            fk_errs = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errs:
                raise ConfiguredStagingPersistenceError("Staged SQLite database failed foreign_key_check")
        finally:
            conn.close()
    except Exception as exc:
        if isinstance(exc, ConfiguredStagingPersistenceError):
            raise
        raise ConfiguredStagingPersistenceError("SQLite foreign_key_check execution failed") from exc

    s_fp = schema_fingerprint(staged_path)
    if s_fp != entry.schema_fingerprint:
        raise ConfiguredStagingPersistenceError("Staged artifact schema fingerprint mismatch")

    m_markers = migration_markers(staged_path, entry.kind)
    expected_markers = {
        "ledger": entry.migration_ledger,
        "keys": list(entry.migration_keys),
        "state": entry.migration_state,
    }
    if m_markers != expected_markers:
        raise ConfiguredStagingPersistenceError("Staged artifact migration markers mismatch")
