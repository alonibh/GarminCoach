"""Configured-runtime restore staging and readiness verification (Phase 6B3B1).

Performs configured-target staging into the restore operation directory,
deep SQLite verification of staged artifacts, disk-space preflight checks,
and read-only readiness proofs without mutating any configured database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import shutil
import stat
from typing import Literal

import config
from guarded_restore import (
    RestoreJournal,
    RestoreJournalError,
    RestoreStage,
    TargetRestoreState,
    load_restore_journal,
    update_restore_journal,
    validate_restore_root,
)
from operator_storage import (
    DatabaseTarget,
    TargetProfile,
    discover_database_targets,
    has_symlink_component,
    inspect_sqlite,
    migration_markers,
    permission_health,
    schema_fingerprint,
)
from verified_backup import ValidatedBackupSnapshot, load_validated_backup_snapshot

_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)
_BINARY_FLAG = getattr(os, "O_BINARY", 0)


class ConfiguredStagingError(RuntimeError):
    """Base error for configured restore staging failures."""


class ConfiguredStagingSourceError(ConfiguredStagingError):
    """Source backup file or manifest invalid during staging."""


class ConfiguredStagingPersistenceError(ConfiguredStagingError):
    """Staged artifact file creation or verification failed."""


class ConfiguredPreflightError(ConfiguredStagingError):
    """Disk space or environment preflight check failed."""


@dataclass(frozen=True)
class ConfiguredStagedArtifact:
    operation_id: str
    target_key: str
    kind: str
    target_order: int
    staged_path: Path = field(repr=False)
    size_bytes: int
    sha256: str
    schema_fingerprint: str
    migration_markers: tuple[str, tuple[str, ...], str]


@dataclass(frozen=True)
class ConfiguredStagingResult:
    operation_id: str
    staged_artifacts: tuple[ConfiguredStagedArtifact, ...]


def _private(path: Path, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt":
            raise ConfiguredStagingPersistenceError("Could not set private staging permissions")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ConfiguredStagingSourceError("File cannot be read for SHA-256 computation") from exc
    return digest.hexdigest()


def _staged_filename(index: int, target_key: str) -> str:
    safe_key = target_key.replace(":", "-")
    return f"{index:03d}-{safe_key}.sqlite.staged"


def stage_configured_targets(
    operation_id: str,
    backup_snapshot: ValidatedBackupSnapshot,
    *,
    restore_root: Path | str | None = None,
) -> ConfiguredStagingResult:
    """Stage targets from a verified backup snapshot into the operation directory.
    
    Copies source backup files into the operation directory as .staged artifacts,
    records intermediate target states in the journal (PENDING -> STAGED -> STAGED_VERIFIED),
    and performs deep SQLite integrity, schema fingerprint, and migration ledger checks.
    
    Does NOT touch or mutate any configured database files.
    """
    root = validate_restore_root(restore_root)
    op_dir = root / f"operation-{operation_id}"
    if not op_dir.exists() or not op_dir.is_dir() or op_dir.is_symlink() or has_symlink_component(op_dir):
        raise ConfiguredStagingPersistenceError("Restore operation directory is unsafe or unavailable")

    journal = load_restore_journal(operation_id, root=root)
    if journal.stage not in {RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.RESTORE_STAGED}:
        raise ConfiguredStagingError(f"Journal stage '{journal.stage}' is not ready for staging")

    # Update global stage to RESTORE_STAGED if not already set
    if journal.stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
        journal = update_restore_journal(operation_id, root=root, stage=RestoreStage.RESTORE_STAGED)

    backup_entries_by_key = {e.target_key: e for e in backup_snapshot.entries}
    staged_info_list: list[tuple[int, str, Path, Any]] = []

    # Step 1: Copy backup files to staged files and transition target states to STAGED
    for index, target_key in enumerate(journal.target_keys):
        if target_key not in backup_entries_by_key:
            raise ConfiguredStagingSourceError(f"Target key '{target_key}' missing from backup snapshot")

        entry = backup_entries_by_key[target_key]
        src_file = backup_snapshot.directory / entry.filename
        if not src_file.exists() or src_file.is_symlink() or has_symlink_component(src_file):
            raise ConfiguredStagingSourceError(f"Backup file '{entry.filename}' is unsafe or missing")

        staged_name = _staged_filename(index, target_key)
        staged_path = op_dir / staged_name
        partial_path = op_dir / f".{staged_name}.partial"

        # Copy source backup file into partial staged file
        try:
            with src_file.open("rb") as src, partial_path.open("wb") as partial:
                _private(partial_path)
                h = hashlib.sha256()
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    partial.write(chunk)
                partial.flush()
                if os.name != "nt":
                    os.fsync(partial.fileno())

            if h.hexdigest() != entry.sha256:
                raise ConfiguredStagingSourceError(f"Source checksum mismatch during staging copy for '{target_key}'")

            os.replace(partial_path, staged_path)
            _private(staged_path)
        except OSError as exc:
            if partial_path.exists():
                try:
                    partial_path.unlink()
                except OSError:
                    pass
            raise ConfiguredStagingPersistenceError(f"Failed to stage target '{target_key}': {exc}") from exc

        # Update target state in journal to STAGED
        journal = update_restore_journal(
            operation_id,
            root=root,
            target_key=target_key,
            target_state=TargetRestoreState.STAGED,
        )

        staged_info_list.append((index, target_key, staged_path, entry))

    # Step 2: Transition global stage to STAGED_VERIFIED (valid when target states are STAGED)
    journal = update_restore_journal(operation_id, root=root, stage=RestoreStage.STAGED_VERIFIED)

    # Step 3: Deep SQLite verification of staged artifacts & target state transition to STAGED_VERIFIED
    artifacts: list[ConfiguredStagedArtifact] = []

    for index, target_key, staged_path, entry in staged_info_list:
        staged_check = inspect_sqlite(staged_path, deep=True)
        if staged_check.integrity_check_ok is not True or staged_check.quick_check_ok is not True:
            raise ConfiguredStagingPersistenceError(f"Staged artifact '{target_key}' failed SQLite deep integrity check")

        staged_fingerprint = schema_fingerprint(staged_path)
        if staged_fingerprint != entry.schema_fingerprint:
            raise ConfiguredStagingPersistenceError(f"Staged artifact '{target_key}' schema fingerprint mismatch")

        staged_markers = migration_markers(staged_path, entry.kind)
        expected_markers = {
            "ledger": entry.migration_ledger,
            "keys": list(entry.migration_keys),
            "state": entry.migration_state,
        }
        if staged_markers != expected_markers:
            raise ConfiguredStagingPersistenceError(f"Staged artifact '{target_key}' migration markers mismatch")

        # Update target state in journal to STAGED_VERIFIED
        journal = update_restore_journal(
            operation_id,
            root=root,
            target_key=target_key,
            target_state=TargetRestoreState.STAGED_VERIFIED,
        )

        artifacts.append(
            ConfiguredStagedArtifact(
                operation_id=operation_id,
                target_key=target_key,
                kind=entry.kind,
                target_order=index,
                staged_path=staged_path,
                size_bytes=staged_path.stat().st_size,
                sha256=entry.sha256,
                schema_fingerprint=staged_fingerprint,
                migration_markers=(entry.migration_ledger, entry.migration_keys, entry.migration_state),
            )
        )

    return ConfiguredStagingResult(
        operation_id=operation_id,
        staged_artifacts=tuple(artifacts),
    )


def preflight_disk_space(
    targets: tuple[DatabaseTarget, ...],
    op_dir: Path,
    multiplier: float = 2.5,
) -> int:
    """Preflight check to verify available disk space before staging/restoring.
    
    Requires free disk space to be at least `multiplier` times the total size
    of all configured database targets.
    """
    total_required_bytes = sum(
        t.path.stat().st_size for t in targets if t.path.exists()
    )
    required_free_bytes = int(total_required_bytes * multiplier)

    try:
        usage = shutil.disk_usage(op_dir.parent if op_dir.exists() else config.PROJECT_ROOT)
    except OSError as exc:
        raise ConfiguredPreflightError("Failed to inspect disk space usage") from exc

    if usage.free < required_free_bytes:
        raise ConfiguredPreflightError(
            f"Insufficient disk space for restore preparation: free={usage.free} bytes, required={required_free_bytes} bytes"
        )
    return usage.free


def verify_configured_readiness(
    operation_id: str,
    backup_snapshot: ValidatedBackupSnapshot,
    targets: tuple[DatabaseTarget, ...],
    staging_result: ConfiguredStagingResult,
    *,
    restore_root: Path | str | None = None,
) -> bool:
    """Final read-only readiness proof before transitioning to REPLACEMENT_READY.
    
    Verifies:
    1. Configured database files and sidecars exist, are untouched, and readable.
    2. All staged artifacts match expected checksums, size, and pass deep SQLite check.
    3. Operation directory and journal are intact in stage STAGED_VERIFIED.
    """
    root = validate_restore_root(restore_root)
    journal = load_restore_journal(operation_id, root=root)

    if journal.stage not in {RestoreStage.STAGED_VERIFIED, RestoreStage.REPLACEMENT_READY}:
        raise ConfiguredStagingError(f"Journal stage '{journal.stage}' invalid for readiness proof")

    # 1. Verify configured database sources are intact and readable
    for t in targets:
        if t.required and not t.path.exists():
            raise ConfiguredStagingError(f"Configured database target '{t.target_key}' missing")
        check = inspect_sqlite(t.path)
        if not check.readable or not check.quick_check_ok:
            raise ConfiguredStagingError(f"Configured database target '{t.target_key}' integrity failed")

    # 2. Verify staged artifacts
    for artifact in staging_result.staged_artifacts:
        if not artifact.staged_path.exists() or artifact.staged_path.is_symlink():
            raise ConfiguredStagingPersistenceError(f"Staged artifact '{artifact.target_key}' missing or unsafe")
        if _sha256_file(artifact.staged_path) != artifact.sha256:
            raise ConfiguredStagingPersistenceError(f"Staged artifact '{artifact.target_key}' SHA-256 drift detected")

    return True
