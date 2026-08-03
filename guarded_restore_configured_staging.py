"""Configured-runtime restore staging and readiness verification (Phase 6B3B1).

Performs configured-target staging into private owned staging directories
located beside each configured destination, creates canonical ownership bindings,
runs deep SQLite verification (including foreign keys), performs exact per-filesystem
disk space preflight, and executes complete read-only readiness proofs.

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

METADATA_OVERHEAD_BYTES = 10_000_000
SAFETY_MARGIN_BYTES = 10_000_000


class ConfiguredRestoreError(RuntimeError):
    """Base error for configured restore preparation and staging failures."""


class ConfiguredStagingError(ConfiguredRestoreError):
    """Base error for configured restore staging failures."""


class ConfiguredStagingSourceError(ConfiguredStagingError):
    """Source backup file or manifest invalid during staging."""


class ConfiguredStagingPersistenceError(ConfiguredStagingError):
    """Staged artifact file creation or verification failed."""


class ConfiguredStagingOwnershipError(ConfiguredStagingError):
    """Stage directory or ownership binding is foreign, modified, or indeterminate."""


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
    raw_path: Path = field(repr=False)
    resolved_path: Path = field(repr=False)
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
    except OSError as exc:
        if os.name != "nt":
            raise ConfiguredStagingPersistenceError("Could not set private permissions") from exc


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


def _strict_json_loads(data_bytes: bytes) -> dict[str, Any]:
    """Parse JSON bytes with duplicate key rejection and max byte check."""
    if len(data_bytes) > _MAX_BINDING_BYTES:
        raise ConfiguredStagingOwnershipError("Ownership binding file exceeds maximum size limit")

    def _reject_duplicates(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ConfiguredStagingOwnershipError(f"Duplicate key '{k}' in ownership binding")
            d[k] = v
        return d

    try:
        text = data_bytes.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicates)
        if not isinstance(parsed, dict):
            raise ConfiguredStagingOwnershipError("Ownership binding JSON root must be an object")
        return parsed
    except Exception as exc:
        if isinstance(exc, ConfiguredStagingOwnershipError):
            raise
        raise ConfiguredStagingOwnershipError("Failed to parse ownership binding JSON") from exc


def capture_destination_baselines(targets: tuple[DatabaseTarget, ...]) -> tuple[DestinationBaselineRecord, ...]:
    """Capture snapshot baseline metrics for configured database files and sidecars.

    Checks original raw paths for symlinks BEFORE calling resolve.
    """
    records: list[DestinationBaselineRecord] = []
    for t in targets:
        raw_p = t.path
        if has_symlink_component(raw_p) or raw_p.is_symlink():
            raise ConfiguredStagingError("Configured database target path contains symlinks")

        if not raw_p.exists():
            if t.required:
                raise ConfiguredStagingError("Required configured database target missing")
            continue

        p = raw_p.resolve()
        if p.is_symlink() or not stat.S_ISREG(p.stat().st_mode):
            raise ConfiguredStagingError("Configured database target must be a regular file")

        st = p.stat()
        parent_p = p.parent
        if has_symlink_component(parent_p) or parent_p.is_symlink():
            raise ConfiguredStagingError("Configured database target parent directory contains symlinks")
        parent_st = parent_p.stat()
        sha = _sha256_file(p)

        wal = raw_p.with_name(raw_p.name + "-wal")
        if has_symlink_component(wal) or wal.is_symlink():
            raise ConfiguredStagingError("WAL sidecar path contains symlinks")
        wal_exists = wal.exists()
        wal_dev = wal_ino = wal_size = wal_mtime = wal_sha = None
        if wal_exists:
            wal_resolved = wal.resolve()
            wst = wal_resolved.stat()
            if not stat.S_ISREG(wst.st_mode):
                raise ConfiguredStagingError("WAL sidecar must be a regular file")
            wal_dev, wal_ino, wal_size, wal_mtime = wst.st_dev, wst.st_ino, wst.st_size, wst.st_mtime_ns
            wal_sha = _sha256_file(wal_resolved)

        shm = raw_p.with_name(raw_p.name + "-shm")
        if has_symlink_component(shm) or shm.is_symlink():
            raise ConfiguredStagingError("SHM sidecar path contains symlinks")
        shm_exists = shm.exists()
        shm_dev = shm_ino = shm_size = shm_mtime = shm_sha = None
        if shm_exists:
            shm_resolved = shm.resolve()
            sst = shm_resolved.stat()
            if not stat.S_ISREG(sst.st_mode):
                raise ConfiguredStagingError("SHM sidecar must be a regular file")
            shm_dev, shm_ino, shm_size, shm_mtime = sst.st_dev, sst.st_ino, sst.st_size, sst.st_mtime_ns
            shm_sha = _sha256_file(shm_resolved)

        records.append(
            DestinationBaselineRecord(
                target_key=t.target_key,
                raw_path=raw_p,
                resolved_path=p,
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
                shm_exists=shm_exists,
                wal_dev=wal_dev,
                wal_ino=wal_ino,
                wal_size=wal_size,
                wal_mtime_ns=wal_mtime,
                wal_sha256=wal_sha,
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
        if has_symlink_component(b.raw_path) or b.raw_path.is_symlink() or not b.resolved_path.exists():
            raise ConfiguredStagingError("Configured destination revalidation failed")

        st = b.resolved_path.stat()
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (b.dev, b.ino, b.size, b.mtime_ns):
            raise ConfiguredStagingError("Configured destination file metadata changed")

        if _sha256_file(b.resolved_path) != b.sha256:
            raise ConfiguredStagingError("Configured destination file SHA-256 changed")

        wal = b.raw_path.with_name(b.raw_path.name + "-wal")
        if has_symlink_component(wal) or wal.is_symlink() or wal.exists() != b.wal_exists:
            raise ConfiguredStagingError("Configured destination WAL sidecar existence or path changed")
        if b.wal_exists and wal.exists():
            wst = wal.resolve().stat()
            if (wst.st_dev, wst.st_ino, wst.st_size, wst.st_mtime_ns) != (b.wal_dev, b.wal_ino, b.wal_size, b.wal_mtime_ns):
                raise ConfiguredStagingError("Configured destination WAL sidecar metadata changed")
            if _sha256_file(wal) != b.wal_sha256:
                raise ConfiguredStagingError("Configured destination WAL sidecar SHA-256 changed")

        shm = b.raw_path.with_name(b.raw_path.name + "-shm")
        if has_symlink_component(shm) or shm.is_symlink() or shm.exists() != b.shm_exists:
            raise ConfiguredStagingError("Configured destination SHM sidecar existence or path changed")
        if b.shm_exists and shm.exists():
            sst = shm.resolve().stat()
            if (sst.st_dev, sst.st_ino, sst.st_size, sst.st_mtime_ns) != (b.shm_dev, b.shm_ino, b.shm_size, b.shm_mtime_ns):
                raise ConfiguredStagingError("Configured destination SHM sidecar metadata changed")
            if _sha256_file(shm) != b.shm_sha256:
                raise ConfiguredStagingError("Configured destination SHM sidecar SHA-256 changed")


def preflight_backup_disk_space(
    targets: tuple[DatabaseTarget, ...],
    backup_root: Path,
) -> int:
    """Preflight check on the operator-backup filesystem before creating safety backup.

    Formula: sum of exact current DB sizes + METADATA_OVERHEAD_BYTES + SAFETY_MARGIN_BYTES.
    """
    total_db_bytes = sum(t.path.stat().st_size for t in targets if t.path.exists())
    required_bytes = total_db_bytes + METADATA_OVERHEAD_BYTES + SAFETY_MARGIN_BYTES

    try:
        usage = shutil.disk_usage(backup_root)
    except OSError as exc:
        raise ConfiguredPreflightError("Failed to inspect backup root disk usage") from exc

    if usage.free < required_bytes:
        raise ConfiguredPreflightError("Insufficient disk space on backup filesystem for safety backup")
    return usage.free


def preflight_staging_disk_space(
    targets: tuple[DatabaseTarget, ...],
    selected_snapshot: ValidatedBackupSnapshot,
    safety_snapshot: ValidatedBackupSnapshot,
) -> None:
    """Preflight check grouped by destination filesystem (dev) under long-held BackupLock.

    Formula per filesystem:
    sum(selected backup entry sizes on this filesystem)
    + sum(safety backup entry sizes on this filesystem)
    + METADATA_OVERHEAD_BYTES
    + SAFETY_MARGIN_BYTES.
    """
    selected_entries = {e.target_key: e for e in selected_snapshot.entries}
    safety_entries = {e.target_key: e for e in safety_snapshot.entries}
    by_filesystem: dict[int, list[tuple[DatabaseTarget, int, int]]] = {}

    for t in targets:
        if not t.path.exists():
            continue
        dev = t.path.parent.stat().st_dev
        sel_size = selected_entries[t.target_key].size_bytes if t.target_key in selected_entries else 0
        saf_size = safety_entries[t.target_key].size_bytes if t.target_key in safety_entries else 0
        by_filesystem.setdefault(dev, []).append((t, sel_size, saf_size))

    for dev, items in by_filesystem.items():
        filesystem_path = items[0][0].path.parent
        sum_selected = sum(sel for _, sel, _ in items)
        sum_safety = sum(saf for _, _, saf in items)
        required_bytes = sum_selected + sum_safety + METADATA_OVERHEAD_BYTES + SAFETY_MARGIN_BYTES

        try:
            usage = shutil.disk_usage(filesystem_path)
        except OSError as exc:
            raise ConfiguredPreflightError("Failed to inspect destination filesystem disk usage") from exc

        if usage.free < required_bytes:
            raise ConfiguredPreflightError("Insufficient disk space on destination filesystem for staging")


def _write_staging_binding(
    stage_dir: Path,
    expected_data: bytes,
) -> None:
    """Write canonical ownership binding metadata file with exclusive publication."""
    binding_path = stage_dir / _STAGING_BINDING_NAME
    temp_path = stage_dir / f".{_STAGING_BINDING_NAME}.partial"

    if binding_path.exists():
        try:
            existing_bytes = binding_path.read_bytes()
            if existing_bytes == expected_data:
                return
        except Exception:
            pass
        raise ConfiguredStagingOwnershipError("Foreign or incompatible staging binding exists")

    fd: int | None = None
    try:
        fd = os.open(
            str(temp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG,
            0o600,
        )
        offset = 0
        while offset < len(expected_data):
            written = os.write(fd, expected_data[offset:])
            if written <= 0:
                raise OSError("Write failed")
            offset += written

        if os.name != "nt":
            os.fsync(fd)
        os.close(fd)
        fd = None

        os.replace(temp_path, binding_path)
        _private(binding_path)

        if binding_path.read_bytes() != expected_data:
            raise ConfiguredStagingOwnershipError("Staging binding verification failed after publish")

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


def validate_existing_staging_directory(
    stage_dir: Path,
    expected_operation_id: str,
    expected_data: bytes,
    expected_staged_names: set[str],
) -> None:
    """Strictly validate existing stage directory for legal re-entry."""
    if stage_dir.is_symlink() or has_symlink_component(stage_dir):
        raise ConfiguredStagingOwnershipError("Stage directory path contains symlinks")

    if not stage_dir.exists() or not stat.S_ISDIR(stage_dir.stat().st_mode):
        raise ConfiguredStagingOwnershipError("Stage directory is not a regular directory")

    binding_file = stage_dir / _STAGING_BINDING_NAME
    if not binding_file.exists() or binding_file.is_symlink():
        raise ConfiguredStagingOwnershipError("Stage directory missing valid ownership binding")

    raw_bytes = binding_file.read_bytes()
    if raw_bytes != expected_data:
        raise ConfiguredStagingOwnershipError("Stage directory ownership binding bytes do not match expected canonical bytes")

    # Strict key set and strict duplicate rejection check
    _strict_json_loads(raw_bytes)

    # Allowed children check inside stage_dir
    allowed_children = {_STAGING_BINDING_NAME} | expected_staged_names
    for child in stage_dir.iterdir():
        c_name = child.name
        if c_name.startswith(".") and c_name.endswith(".partial"):
            continue
        if c_name not in allowed_children:
            raise ConfiguredStagingOwnershipError(f"Stage directory contains unexpected foreign child '{c_name}'")


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

    if journal.stage not in {RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.RESTORE_STAGED, RestoreStage.STAGED_VERIFIED, RestoreStage.REPLACEMENT_READY}:
        raise ConfiguredStagingError("Journal stage is invalid for staging")

    if journal.stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
        journal = update_restore_journal(operation_id, root=root, stage=RestoreStage.RESTORE_STAGED)

    backup_entries_by_key = {e.target_key: e for e in backup_snapshot.entries}
    targets_by_key = {t.target_key: t for t in targets}
    staged_info: list[tuple[int, str, Path, Any, Path]] = []

    by_parent: dict[Path, list[tuple[int, str, DatabaseTarget, Any]]] = {}

    for index, target_key in enumerate(journal.target_keys):
        if target_key not in backup_entries_by_key or target_key not in targets_by_key:
            raise ConfiguredStagingSourceError("Target key mismatch between backup and configured runtime")

        t = targets_by_key[target_key]
        entry = backup_entries_by_key[target_key]
        parent_dir = t.path.parent.resolve()
        by_parent.setdefault(parent_dir, []).append((index, target_key, t, entry))

    for parent_dir, items in by_parent.items():
        if not parent_dir.exists() or parent_dir.is_symlink() or has_symlink_component(parent_dir):
            raise ConfiguredStagingPersistenceError("Destination parent directory cannot contain symlinks")

        stage_dir = parent_dir / _staged_dir_name(operation_id)
        expected_staged_names = {_staged_artifact_name(it[0], it[1]) for it in items}

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
        expected_binding_bytes = canonical_json(binding_payload)

        if stage_dir.exists():
            validate_existing_staging_directory(stage_dir, operation_id, expected_binding_bytes, expected_staged_names)
        else:
            try:
                stage_dir.mkdir(parents=False, exist_ok=False)
                _private(stage_dir, directory=True)

                if os.name != "nt":
                    parent_fd = os.open(str(parent_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
            except OSError as exc:
                raise ConfiguredStagingPersistenceError("Could not create exclusive stage directory") from exc

        if stage_dir.stat().st_dev != parent_dir.stat().st_dev:
            raise ConfiguredStagingPersistenceError("Staging directory and destination parent reside on different filesystems")

        _write_staging_binding(stage_dir, expected_binding_bytes)

        for index, target_key, target_obj, entry in items:
            tf = next((f for f in journal.targets if f.target_key == target_key), None)
            if tf is None:
                raise ConfiguredStagingError("Target key not found in journal target facts")
            staged_filename = _staged_artifact_name(index, target_key)
            staged_path = stage_dir / staged_filename
            partial_path = stage_dir / f".{staged_filename}.partial"

            if tf.state in {TargetRestoreState.STAGED, TargetRestoreState.STAGED_VERIFIED} and staged_path.exists():
                if staged_path.is_symlink() or staged_path.stat().st_size != entry.size_bytes or _sha256_file(staged_path) != entry.sha256:
                    raise ConfiguredStagingPersistenceError("Existing staged artifact is incompatible or modified")
            else:
                src_file = backup_snapshot.directory / entry.filename
                if not src_file.exists() or src_file.is_symlink() or has_symlink_component(src_file):
                    raise ConfiguredStagingSourceError("Backup source file is missing or unsafe")

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

            if tf.state is TargetRestoreState.PENDING:
                journal = update_restore_journal(
                    operation_id,
                    root=root,
                    target_key=target_key,
                    target_state=TargetRestoreState.STAGED,
                )

            staged_info.append((index, target_key, staged_path, entry, stage_dir))

    if journal.stage is RestoreStage.RESTORE_STAGED:
        journal = update_restore_journal(operation_id, root=root, stage=RestoreStage.STAGED_VERIFIED)

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

        tf = next((f for f in journal.targets if f.target_key == target_key), None)
        if tf is not None and tf.state is TargetRestoreState.STAGED:
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

    revalidate_destination_baselines(baselines)

    for artifact in staging_result.staged_artifacts:
        if not artifact.staged_path.exists() or artifact.staged_path.is_symlink():
            raise ConfiguredStagingPersistenceError("Staged artifact missing or unsafe during readiness proof")
        if _sha256_file(artifact.staged_path) != artifact.sha256:
            raise ConfiguredStagingPersistenceError("Staged artifact SHA-256 drift during readiness proof")
        check = inspect_sqlite(artifact.staged_path, deep=True)
        if check.integrity_check_ok is not True or check.foreign_keys_ok is not True:
            raise ConfiguredStagingPersistenceError("Staged artifact SQLite check failed during readiness proof")

    for tf in journal.targets:
        if tf.state is not TargetRestoreState.STAGED_VERIFIED:
            raise ConfiguredStagingError("Journal target state is not STAGED_VERIFIED")
        if tf.replacement_intent or tf.replacement_completed or tf.rollback_intent or tf.rollback_completed:
            raise ConfiguredStagingError("Journal target contains mutation flags before replacement")

    return True
