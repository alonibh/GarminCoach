"""Configured-runtime restore staging and readiness verification (Phase 6B3B1).

Performs configured-target staging into private owned staging directories
located beside each configured destination, creates canonical ownership bindings,
runs deep SQLite verification (including foreign keys), performs per-filesystem
disk space preflight, and executes read-only readiness proofs.

No configured database files or sidecars are modified or replaced by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sqlite3
from typing import Any, Literal

import config
from guarded_restore import (
    RestoreJournal,
    RestoreJournalError,
    RestoreStage,
    TargetRestoreState,
    canonical_json,
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
    safe_resolve,
    schema_fingerprint,
)
from verified_backup import ValidatedBackupSnapshot, load_validated_backup_snapshot

_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)
_BINARY_FLAG = getattr(os, "O_BINARY", 0)
_STAGING_BINDING_FORMAT = "garmincoach-restore-staging-binding-v1"
_STAGING_BINDING_NAME = ".staging-binding.json"
_MAX_BINDING_BYTES = 64 * 1024


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


@dataclass(frozen=True)
class DestinationBaselineRecord:
    target_key: str
    path: Path = field(repr=False)
    dev: int
    ino: int
    size: int
    sha256: str
    mtime_ns: int
    mode: int
    parent_dev: int
    parent_ino: int
    parent_mode: int
    wal_exists: bool
    shm_exists: bool
    wal_dev: int | None = None
    wal_ino: int | None = None
    wal_size: int | None = None
    wal_mtime_ns: int | None = None
    wal_sha256: str | None = None
    shm_dev: int | None = None
    shm_ino: int | None = None
    shm_size: int | None = None
    shm_mtime_ns: int | None = None
    shm_sha256: str | None = None


def _private(path: Path, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt":
            raise ConfiguredStagingPersistenceError("Could not set private permissions")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ConfiguredStagingSourceError("File cannot be read for SHA-256 computation") from exc
    return digest.hexdigest()


def _staged_dir_name(operation_id: str) -> str:
    return f".garmincoach-restore-stage-{operation_id}"


def _staged_artifact_name(index: int, target_key: str) -> str:
    safe_key = target_key.replace(":", "-")
    return f"{index:03d}-{safe_key}.sqlite.staged"


def capture_destination_baselines(targets: tuple[DatabaseTarget, ...]) -> tuple[DestinationBaselineRecord, ...]:
    """Capture snapshot baseline metrics for configured database files and sidecars."""
    records: list[DestinationBaselineRecord] = []
    for t in targets:
        if not t.path.exists():
            if t.required:
                raise ConfiguredStagingError(f"Configured database target missing")
            continue

        p = t.path.resolve()
        if p.is_symlink() or has_symlink_component(p):
            raise ConfiguredStagingError("Configured database target cannot contain symlinks")

        st = p.stat()
        parent_st = p.parent.stat()
        sha = _sha256_file(p)

        wal = p.with_name(p.name + "-wal")
        wal_exists = wal.exists()
        wal_dev = wal_ino = wal_size = wal_mtime = None
        wal_sha = None
        if wal_exists and wal.is_file() and not wal.is_symlink():
            wst = wal.stat()
            wal_dev, wal_ino, wal_size, wal_mtime = wst.st_dev, wst.st_ino, wst.st_size, wst.st_mtime_ns
            wal_sha = _sha256_file(wal)

        shm = p.with_name(p.name + "-shm")
        shm_exists = shm.exists()
        shm_dev = shm_ino = shm_size = shm_mtime = None
        shm_sha = None
        if shm_exists and shm.is_file() and not shm.is_symlink():
            sst = shm.stat()
            shm_dev, shm_ino, shm_size, shm_mtime = sst.st_dev, sst.st_ino, sst.st_size, sst.st_mtime_ns
            shm_sha = _sha256_file(shm)

        records.append(
            DestinationBaselineRecord(
                target_key=t.target_key,
                path=p,
                dev=st.st_dev,
                ino=st.st_ino,
                size=st.st_size,
                sha256=sha,
                mtime_ns=st.st_mtime_ns,
                mode=stat.S_IMODE(st.st_mode),
                parent_dev=parent_st.st_dev,
                parent_ino=parent_st.st_ino,
                parent_mode=stat.S_IMODE(parent_st.st_mode),
                wal_exists=wal_exists,
                wal_dev=wal_dev,
                wal_ino=wal_ino,
                wal_size=wal_size,
                wal_mtime_ns=wal_mtime,
                wal_sha256=wal_sha,
                shm_exists=shm_exists,
                shm_dev=shm_dev,
                shm_ino=shm_ino,
                shm_size=shm_size,
                shm_mtime_ns=shm_mtime,
                shm_sha256=shm_sha,
            )
        )
    return tuple(records)


def revalidate_destination_baselines(baselines: tuple[DestinationBaselineRecord, ...]) -> None:
    """Verify configured database destinations and sidecars have experienced ZERO mutation."""
    for b in baselines:
        if not b.path.exists() or b.path.is_symlink() or has_symlink_component(b.path):
            raise ConfiguredStagingError("Configured destination revalidation failed")

        st = b.path.stat()
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (b.dev, b.ino, b.size, b.mtime_ns):
            raise ConfiguredStagingError("Configured destination file metadata changed")

        if _sha256_file(b.path) != b.sha256:
            raise ConfiguredStagingError("Configured destination file SHA-256 changed")

        wal = b.path.with_name(b.path.name + "-wal")
        if wal.exists() != b.wal_exists:
            raise ConfiguredStagingError("Configured destination WAL sidecar existence changed")
        if b.wal_exists and wal.exists():
            wst = wal.stat()
            if (wst.st_dev, wst.st_ino, wst.st_size, wst.st_mtime_ns) != (b.wal_dev, b.wal_ino, b.wal_size, b.wal_mtime_ns):
                raise ConfiguredStagingError("Configured destination WAL sidecar metadata changed")
            if _sha256_file(wal) != b.wal_sha256:
                raise ConfiguredStagingError("Configured destination WAL sidecar SHA-256 changed")

        shm = b.path.with_name(b.path.name + "-shm")
        if shm.exists() != b.shm_exists:
            raise ConfiguredStagingError("Configured destination SHM sidecar existence changed")
        if b.shm_exists and shm.exists():
            sst = shm.stat()
            if (sst.st_dev, sst.st_ino, sst.st_size, sst.st_mtime_ns) != (b.shm_dev, b.shm_ino, b.shm_size, b.shm_mtime_ns):
                raise ConfiguredStagingError("Configured destination SHM sidecar metadata changed")
            if _sha256_file(shm) != b.shm_sha256:
                raise ConfiguredStagingError("Configured destination SHM sidecar SHA-256 changed")


def preflight_backup_disk_space(
    targets: tuple[DatabaseTarget, ...],
    backup_root: Path,
    multiplier: float = 2.0,
    overhead_bytes: int = 10_000_000,
) -> int:
    """Preflight check on the operator-backup filesystem before creating safety backup."""
    total_db_bytes = sum(t.path.stat().st_size for t in targets if t.path.exists())
    required_bytes = int(total_db_bytes * multiplier) + overhead_bytes

    try:
        usage = shutil.disk_usage(backup_root)
    except OSError as exc:
        raise ConfiguredPreflightError("Failed to inspect backup root disk usage") from exc

    if usage.free < required_bytes:
        raise ConfiguredPreflightError("Insufficient disk space on backup filesystem for safety backup")
    return usage.free


def preflight_staging_disk_space(
    targets: tuple[DatabaseTarget, ...],
    backup_snapshot: ValidatedBackupSnapshot,
    multiplier: float = 2.0,
    overhead_bytes: int = 10_000_000,
) -> None:
    """Preflight check grouped by destination filesystem (dev) under long-held BackupLock."""
    entries_by_key = {e.target_key: e for e in backup_snapshot.entries}
    by_filesystem: dict[int, list[tuple[DatabaseTarget, int]]] = {}

    for t in targets:
        if not t.path.exists():
            continue
        st = t.path.parent.stat()
        dev = st.st_dev
        size = entries_by_key[t.target_key].size_bytes if t.target_key in entries_by_key else t.path.stat().st_size
        by_filesystem.setdefault(dev, []).append((t, size))

    for dev, target_items in by_filesystem.items():
        filesystem_path = target_items[0][0].path.parent
        sum_staged_bytes = sum(size for _, size in target_items)
        required_bytes = int(sum_staged_bytes * multiplier) + overhead_bytes

        try:
            usage = shutil.disk_usage(filesystem_path)
        except OSError as exc:
            raise ConfiguredPreflightError("Failed to inspect destination filesystem disk usage") from exc

        if usage.free < required_bytes:
            raise ConfiguredPreflightError("Insufficient disk space on destination filesystem for staging")


def _write_staging_binding(
    stage_dir: Path,
    payload: dict[str, Any],
) -> None:
    """Write canonical ownership binding metadata file with descriptor-safe atomic replace."""
    binding_path = stage_dir / _STAGING_BINDING_NAME
    temp_path = stage_dir / f".{_STAGING_BINDING_NAME}.partial"
    data = canonical_json(payload)

    if binding_path.exists():
        # Check if existing binding matches exact payload
        try:
            existing_bytes = binding_path.read_bytes()
            existing_parsed = json.loads(existing_bytes.decode("utf-8"))
            if canonical_json(existing_parsed) == data:
                return
        except Exception:
            pass
        raise ConfiguredStagingPersistenceError("Foreign or incompatible staging binding exists")

    fd: int | None = None
    try:
        fd = os.open(
            str(temp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG,
            0o600,
        )
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("Write failed")
            offset += written

        if os.name != "nt":
            os.fsync(fd)
        os.close(fd)
        fd = None

        os.replace(temp_path, binding_path)
        _private(binding_path)

        if os.name != "nt":
            dir_fd = os.open(str(stage_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise ConfiguredStagingPersistenceError("Failed to persist canonical staging binding metadata") from exc


def stage_configured_targets(
    operation_id: str,
    backup_snapshot: ValidatedBackupSnapshot,
    targets: tuple[DatabaseTarget, ...],
    *,
    restore_root: Path | str | None = None,
) -> ConfiguredStagingResult:
    """Stage targets into private owned staging directories beside configured destinations."""
    root = validate_restore_root(restore_root)
    journal = load_restore_journal(operation_id, root=root)

    if journal.stage not in {RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.RESTORE_STAGED}:
        raise ConfiguredStagingError(f"Journal stage is invalid for staging")

    if journal.stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
        journal = update_restore_journal(operation_id, root=root, stage=RestoreStage.RESTORE_STAGED)

    backup_entries_by_key = {e.target_key: e for e in backup_snapshot.entries}
    targets_by_key = {t.target_key: t for t in targets}
    staged_info: list[tuple[int, str, Path, Any, Path]] = []

    # Map parent directory -> targets staged in that directory
    by_parent: dict[Path, list[tuple[int, str, DatabaseTarget, Any]]] = {}

    for index, target_key in enumerate(journal.target_keys):
        if target_key not in backup_entries_by_key or target_key not in targets_by_key:
            raise ConfiguredStagingSourceError("Target key mismatch between backup and configured runtime")

        t = targets_by_key[target_key]
        entry = backup_entries_by_key[target_key]
        parent_dir = t.path.parent.resolve()
        by_parent.setdefault(parent_dir, []).append((index, target_key, t, entry))

    # Perform staging directory setup and target copying per parent directory
    for parent_dir, items in by_parent.items():
        if parent_dir.is_symlink() or has_symlink_component(parent_dir):
            raise ConfiguredStagingPersistenceError("Destination parent directory cannot contain symlinks")

        stage_dir = parent_dir / _staged_dir_name(operation_id)
        if stage_dir.is_symlink():
            raise ConfiguredStagingPersistenceError("Staging directory cannot be a symlink")

        # Create private staging directory if it doesn't exist
        stage_dir.mkdir(parents=True, exist_ok=True)
        _private(stage_dir, directory=True)

        # Same filesystem check: stage_dir and parent_dir MUST have same st_dev
        if stage_dir.stat().st_dev != parent_dir.stat().st_dev:
            raise ConfiguredStagingPersistenceError("Staging directory and destination parent reside on different filesystems")

        # Create ownership binding metadata
        binding_payload = {
            "format_version": _STAGING_BINDING_FORMAT,
            "operation_id": operation_id,
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
                    "staged_filename": _staged_artifact_name(item[0], item[1]),
                    "size_bytes": item[3].size_bytes,
                    "sha256": item[3].sha256,
                }
                for item in items
            ],
        }
        _write_staging_binding(stage_dir, binding_payload)

        # Copy each target artifact into stage_dir
        for index, target_key, target_obj, entry in items:
            src_file = backup_snapshot.directory / entry.filename
            if not src_file.exists() or src_file.is_symlink() or has_symlink_component(src_file):
                raise ConfiguredStagingSourceError("Backup source file is missing or unsafe")

            staged_filename = _staged_artifact_name(index, target_key)
            staged_path = stage_dir / staged_filename
            partial_path = stage_dir / f".{staged_filename}.partial"

            # Check if compatible staged file already exists (for re-entry)
            if staged_path.exists():
                if staged_path.is_symlink() or staged_path.stat().st_size != entry.size_bytes or _sha256_file(staged_path) != entry.sha256:
                    raise ConfiguredStagingPersistenceError("Existing staged artifact is incompatible or modified")
            else:
                src_fd = None
                partial_fd = None
                try:
                    src_fd = os.open(str(src_file), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
                    before_st = os.fstat(src_fd)
                    if before_st.st_size != entry.size_bytes:
                        raise ConfiguredStagingSourceError("Source backup file size changed during read")

                    partial_fd = os.open(
                        str(partial_path),
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG,
                        0o600,
                    )
                    h = hashlib.sha256()
                    count = 0
                    while True:
                        chunk = os.read(src_fd, 1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                        count += len(chunk)
                        written = 0
                        while written < len(chunk):
                            n = os.write(partial_fd, chunk[written:])
                            if n <= 0:
                                raise OSError("Write failed")
                            written += n

                    if os.name != "nt":
                        os.fsync(partial_fd)

                    after_st = os.fstat(src_fd)
                    if (before_st.st_dev, before_st.st_ino, before_st.st_size) != (after_st.st_dev, after_st.st_ino, after_st.st_size) or count != entry.size_bytes or h.hexdigest() != entry.sha256:
                        raise ConfiguredStagingSourceError("Source backup file checksum or size drift detected")

                    os.close(src_fd)
                    src_fd = None
                    os.close(partial_fd)
                    partial_fd = None

                    os.replace(partial_path, staged_path)
                    _private(staged_path)

                    if os.name != "nt":
                        stage_dir_fd = os.open(str(stage_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                        try:
                            os.fsync(stage_dir_fd)
                        finally:
                            os.close(stage_dir_fd)
                except OSError as exc:
                    if src_fd is not None:
                        try:
                            os.close(src_fd)
                        except OSError:
                            pass
                    if partial_fd is not None:
                        try:
                            os.close(partial_fd)
                        except OSError:
                            pass
                    if partial_path.exists():
                        try:
                            partial_path.unlink()
                        except OSError:
                            pass
                    raise ConfiguredStagingPersistenceError("Descriptor-safe staged file publication failed") from exc

            # Record target state transition to STAGED in journal
            journal = update_restore_journal(
                operation_id,
                root=root,
                target_key=target_key,
                target_state=TargetRestoreState.STAGED,
            )

            staged_info.append((index, target_key, staged_path, entry, stage_dir))

    # Transition global stage to STAGED_VERIFIED
    journal = update_restore_journal(operation_id, root=root, stage=RestoreStage.STAGED_VERIFIED)

    # Deep verification of all staged SQLite artifacts (including foreign keys!)
    artifacts: list[ConfiguredStagedArtifact] = []

    for index, target_key, staged_path, entry, stage_dir in staged_info:
        if staged_path.is_symlink() or has_symlink_component(staged_path):
            raise ConfiguredStagingPersistenceError("Staged path is a symlink")

        st = staged_path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size != entry.size_bytes or _sha256_file(staged_path) != entry.sha256:
            raise ConfiguredStagingPersistenceError("Staged file size or SHA-256 verification failed")

        inspected = inspect_sqlite(staged_path, deep=True)
        if inspected.readable is not True or inspected.quick_check_ok is not True or inspected.integrity_check_ok is not True or inspected.foreign_keys_ok is not True:
            raise ConfiguredStagingPersistenceError(f"Staged file failed deep SQLite integrity or foreign-key check")

        staged_fingerprint = schema_fingerprint(staged_path)
        if staged_fingerprint != entry.schema_fingerprint:
            raise ConfiguredStagingPersistenceError("Staged file schema fingerprint mismatch")

        staged_markers = migration_markers(staged_path, entry.kind)
        expected_markers = {
            "ledger": entry.migration_ledger,
            "keys": list(entry.migration_keys),
            "state": entry.migration_state,
        }
        if staged_markers != expected_markers:
            raise ConfiguredStagingPersistenceError("Staged file migration markers mismatch")

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
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
                schema_fingerprint=staged_fingerprint,
                migration_markers=(entry.migration_ledger, entry.migration_keys, entry.migration_state),
            )
        )

    return ConfiguredStagingResult(
        operation_id=operation_id,
        staged_artifacts=tuple(artifacts),
    )


def verify_configured_readiness(
    operation_id: str,
    backup_snapshot: ValidatedBackupSnapshot,
    targets: tuple[DatabaseTarget, ...],
    staging_result: ConfiguredStagingResult,
    baselines: tuple[DestinationBaselineRecord, ...],
    *,
    restore_root: Path | str | None = None,
) -> bool:
    """Final read-only readiness proof barrier before transitioning to REPLACEMENT_READY."""
    root = validate_restore_root(restore_root)
    journal = load_restore_journal(operation_id, root=root)

    if journal.stage not in {RestoreStage.STAGED_VERIFIED, RestoreStage.REPLACEMENT_READY}:
        raise ConfiguredStagingError("Journal stage invalid for readiness proof")

    # 1. Re-verify configured database baselines and sidecars
    revalidate_destination_baselines(baselines)

    # 2. Re-verify staged artifacts and binding metadata
    for artifact in staging_result.staged_artifacts:
        if not artifact.staged_path.exists() or artifact.staged_path.is_symlink():
            raise ConfiguredStagingPersistenceError("Staged artifact missing or unsafe during readiness proof")
        if _sha256_file(artifact.staged_path) != artifact.sha256:
            raise ConfiguredStagingPersistenceError("Staged artifact SHA-256 drift during readiness proof")
        check = inspect_sqlite(artifact.staged_path, deep=True)
        if check.integrity_check_ok is not True or check.foreign_keys_ok is not True:
            raise ConfiguredStagingPersistenceError("Staged artifact SQLite check failed during readiness proof")

    # 3. Verify all journal target facts at REPLACEMENT_READY
    for tf in journal.targets:
        if tf.state is not TargetRestoreState.STAGED_VERIFIED:
            raise ConfiguredStagingError("Journal target state is not STAGED_VERIFIED")
        if tf.replacement_intent or tf.replacement_completed or tf.rollback_intent or tf.rollback_completed:
            raise ConfiguredStagingError("Journal target contains mutation flags before replacement")

    return True
