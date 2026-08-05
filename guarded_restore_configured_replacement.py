"""Configured-runtime restore replacement, postcheck, rollback, and re-entry (Phase 6B3B2).

Orchestrates the actual database replacement for configured-runtime restore:
1. Keyword-only API: no caller-supplied paths, backup directories, or staging paths.
2. Rediscovers canonical targets from config; loads journal by validated operation ID.
3. Acquires ProcessLock -> RestoreLock -> BackupLock; holds all throughout mutation.
4. Runs full proof barrier after lock acquisition before any configured mutation.
5. Stages rollback artifacts from the operation recorded safety backup.
6. Replacement order: data/tenant targets first, control database last.
7. Atomic os.replace; durable replacement_intent before and replacement_completed after.
8. Named WAL/SHM sidecars removed with durable journal recording (no wildcard, no symlinks).
9. Descriptor-bound post-replacement verification: type, mode, link count, size, SHA-256.
10. Complete postcheck (SQLite, schema, markers, backup integrity) before COMPLETED.
11. Automatic rollback on any replacement or postcheck failure.
12. Legal re-entry for every stage from REPLACEMENT_READY through COMPLETED.
13. FAILED_MANUAL_RECOVERY_REQUIRED on ambiguous state; never auto-resuming.
14. Strict owned evidence cleanup: no recursive rmtree, no wildcard deletion.
15. BackupLock -> RestoreLock -> ProcessLock release before returning result.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import config
from guarded_restore import (
    FinalResult,
    RestoreJournal,
    RestoreJournalError,
    RestoreJournalPersistenceError,
    RestoreLock,
    RestoreLockError,
    RestoreStage,
    TargetRestoreState,
    _GLOBAL_TRANSITIONS,
    canonical_json,
    confirmation_value,
    load_restore_journal,
    target_set_hash,
    update_restore_journal,
    validate_restore_root,
)
from guarded_restore_configured import (
    ConfiguredJournalUncertaintyError,
    ConfiguredRestoreLockReleaseError,
    _verify_project_root,
)
from guarded_restore_configured_staging import (
    ConfiguredRestoreError,
    ConfiguredStagingError,
    ConfiguredStagingOwnershipError,
    DestinationBaselineEvidence,
    _sha256_file,
    _staged_artifact_name,
    _staged_dir_name,
    _verify_durable_parent,
    load_destination_baseline_evidence,
    revalidate_destination_baseline_evidence,
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
    load_validated_backup_snapshot,
    validate_backup_root,
)

_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)
_BINARY_FLAG = getattr(os, "O_BINARY", 0)
_ROLLBACK_BINDING_FORMAT = "garmincoach-rollback-binding-v1"
_ROLLBACK_BINDING_NAME = ".rollback-binding.json"
_MAX_BINDING_BYTES = 64 * 1024
_BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_OPERATION_ID_RE = re.compile(r"^restore-\d{8}T\d{6}Z-[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# Bounded error classes
# ---------------------------------------------------------------------------

class ConfiguredReplacementPreconditionError(ConfiguredRestoreError):
    """Pre-mutation barrier or precondition failure; no configured database mutated."""


class ConfiguredReplacementPostcheckError(ConfiguredRestoreError):
    """Postcheck failed after replacement; automatic rollback was triggered."""


class ConfiguredReplacementRollbackCompletedError(ConfiguredRestoreError):
    """Automatic rollback completed successfully; databases restored to safety bytes."""


class ConfiguredReplacementManualRecoveryRequiredError(ConfiguredRestoreError):
    """Ambiguous destination state; manual recovery required; never auto-resume."""


class ConfiguredReplacementCleanupError(ConfiguredRestoreError):
    """Database outcome is known but evidence cleanup requires manual action."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfiguredReplacementResult:
    """Frozen bounded result representing a completed Phase 6B3B2 operation."""
    operation_id: str
    stage: RestoreStage
    selected_backup_id: str
    safety_backup_id: str
    runtime_mode: str
    target_keys: tuple
    replaced_target_keys: tuple
    rollback_occurred: bool
    configured_database_mutated: bool
    locks_released: bool


# ---------------------------------------------------------------------------
# Internal file/descriptor helpers
# ---------------------------------------------------------------------------

def _fsync_path(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | _NOFOLLOW_FLAG | (getattr(os, "O_DIRECTORY", 0) if directory else _BINARY_FLAG)
    try:
        fd = os.open(str(path), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("fsync failed") from exc


def _open_nf(path: Path) -> int:
    """Open with no-follow; verify regular file; return fd."""
    try:
        fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("Could not open file no-follow") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ConfiguredReplacementPreconditionError("Opened descriptor is not a regular file")
    except ConfiguredReplacementPreconditionError:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _sha256_fd(fd: int, expected_size: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
    if total != expected_size:
        raise ConfiguredReplacementPreconditionError("Descriptor read size mismatch")
    return h.hexdigest()


def _verify_file_owned(path: Path, *, expected_size: int, expected_sha256: str) -> tuple:
    """Open no-follow, verify regular file, size, SHA-256. Returns (dev, ino, mode)."""
    fd = _open_nf(path)
    try:
        st = os.fstat(fd)
        if st.st_size != expected_size:
            raise ConfiguredReplacementPreconditionError(
                f"File size {st.st_size} != expected {expected_size}"
            )
        pst = os.stat(str(path), follow_symlinks=False)
        if (pst.st_dev, pst.st_ino) != (st.st_dev, st.st_ino):
            raise ConfiguredReplacementPreconditionError("File path identity mismatch")
        actual_sha = _sha256_fd(fd, expected_size)
        if actual_sha != expected_sha256:
            raise ConfiguredReplacementPreconditionError("File SHA-256 mismatch")
        return st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Rollback artifact naming
# ---------------------------------------------------------------------------

def _rollback_dir_name(operation_id: str, index: int) -> str:
    return f".garmincoach-restore-rollback-{operation_id}-{index:03d}"


def _rollback_artifact_name(index: int, target_key: str) -> str:
    return f"{index:03d}-{target_key.replace(':', '-')}.sqlite.rollback"


# ---------------------------------------------------------------------------
# Rollback artifact staging
# ---------------------------------------------------------------------------

def _rollback_binding_bytes(
    operation_id: str, safety_backup_id: str, safety_manifest_sha256: str,
    target_key: str, kind: str, index: int, rollback_filename: str,
    size_bytes: int, sha256: str,
) -> bytes:
    return canonical_json({
        "format_version": _ROLLBACK_BINDING_FORMAT,
        "operation_id": operation_id,
        "safety_backup_id": safety_backup_id,
        "safety_backup_manifest_sha256": safety_manifest_sha256,
        "target_key": target_key,
        "kind": kind,
        "target_order": index,
        "rollback_filename": rollback_filename,
        "size_bytes": size_bytes,
        "sha256": sha256,
    })


def _write_rollback_binding(
    rollback_dir: Path, *, operation_id: str, safety_backup_id: str,
    safety_manifest_sha256: str, target_key: str, kind: str, index: int,
    rollback_filename: str, size_bytes: int, sha256: str,
) -> None:
    data = _rollback_binding_bytes(
        operation_id, safety_backup_id, safety_manifest_sha256,
        target_key, kind, index, rollback_filename, size_bytes, sha256,
    )
    if len(data) > _MAX_BINDING_BYTES:
        raise ConfiguredReplacementPreconditionError("Rollback binding payload too large")
    final_p = rollback_dir / _ROLLBACK_BINDING_NAME
    partial_p = rollback_dir / ("." + _ROLLBACK_BINDING_NAME + ".partial")
    try:
        os.lstat(str(final_p))
        raise ConfiguredReplacementPreconditionError("Rollback binding already exists")
    except FileNotFoundError:
        pass
    try:
        os.lstat(str(partial_p))
        raise ConfiguredReplacementPreconditionError("Rollback binding partial already exists")
    except FileNotFoundError:
        pass
    fd = None
    try:
        fd = os.open(str(partial_p), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG, 0o600)
        offset = 0
        while offset < len(data):
            n = os.write(fd, data[offset:])
            if n <= 0:
                raise OSError("write failed")
            offset += n
        if os.name != "nt":
            os.fsync(fd)
        if os.fstat(fd).st_size != len(data):
            raise ConfiguredReplacementPreconditionError("Rollback binding write size mismatch")
        os.close(fd); fd = None
    except (OSError, ConfiguredReplacementPreconditionError):
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        try: partial_p.unlink()
        except OSError: pass
        raise
    try:
        os.lstat(str(final_p))
        try: partial_p.unlink()
        except OSError: pass
        raise ConfiguredReplacementPreconditionError("Rollback binding destination appeared before replace")
    except FileNotFoundError:
        pass
    try:
        os.replace(str(partial_p), str(final_p))
    except OSError as exc:
        try: partial_p.unlink()
        except OSError: pass
        raise ConfiguredReplacementPreconditionError("Rollback binding replace failed") from exc
    if os.name != "nt":
        try: os.chmod(str(final_p), 0o600)
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError("Rollback binding chmod failed") from exc
    vfd = None
    try:
        vfd = os.open(str(final_p), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        fst = os.fstat(vfd); pst = os.lstat(str(final_p))
        if (fst.st_dev, fst.st_ino, fst.st_size) != (pst.st_dev, pst.st_ino, pst.st_size):
            raise ConfiguredReplacementPreconditionError("Rollback binding path/fd identity mismatch")
        rd = b""
        while True:
            chunk = os.read(vfd, 65536)
            if not chunk: break
            rd += chunk
        if rd != data:
            raise ConfiguredReplacementPreconditionError("Rollback binding verification data mismatch")
        if os.name != "nt":
            os.fsync(vfd)
    finally:
        if vfd is not None:
            try: os.close(vfd)
            except OSError: pass
    _fsync_path(rollback_dir, directory=True)


def _verify_rollback_binding(
    rollback_dir: Path, *, operation_id: str, safety_backup_id: str,
    safety_manifest_sha256: str, target_key: str, kind: str, index: int,
    rollback_filename: str, size_bytes: int, sha256: str,
) -> Path:
    """Verify rollback binding and artifact; return artifact path."""
    binding_p = rollback_dir / _ROLLBACK_BINDING_NAME
    if binding_p.is_symlink() or has_symlink_component(binding_p):
        raise ConfiguredReplacementPreconditionError("Rollback binding path contains symlinks")
    expected = _rollback_binding_bytes(
        operation_id, safety_backup_id, safety_manifest_sha256,
        target_key, kind, index, rollback_filename, size_bytes, sha256,
    )
    fd = None
    try:
        fd = os.open(str(binding_p), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        bst = os.fstat(fd); pst = os.lstat(str(binding_p))
        if (bst.st_dev, bst.st_ino, bst.st_size) != (pst.st_dev, pst.st_ino, pst.st_size):
            raise ConfiguredReplacementPreconditionError("Rollback binding identity mismatch")
        if not stat.S_ISREG(bst.st_mode) or bst.st_size > _MAX_BINDING_BYTES:
            raise ConfiguredReplacementPreconditionError("Rollback binding invalid")
        rd = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk: break
            rd += chunk
        ast = os.fstat(fd); apst = os.lstat(str(binding_p))
        if (bst.st_dev, bst.st_ino, bst.st_size) != (ast.st_dev, ast.st_ino, ast.st_size):
            raise ConfiguredReplacementPreconditionError("Rollback binding changed during read")
        if (apst.st_dev, apst.st_ino) != (ast.st_dev, ast.st_ino):
            raise ConfiguredReplacementPreconditionError("Rollback binding path identity mismatch after read")
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
    if rd != expected:
        raise ConfiguredReplacementPreconditionError("Rollback binding bytes mismatch")
    artifact_p = rollback_dir / rollback_filename
    if has_symlink_component(artifact_p) or artifact_p.is_symlink():
        raise ConfiguredReplacementPreconditionError("Rollback artifact path contains symlinks")
    try:
        rdir_dev = os.lstat(str(rollback_dir)).st_dev
        art_dev = os.lstat(str(artifact_p)).st_dev
        if rdir_dev != art_dev:
            raise ConfiguredReplacementPreconditionError("Rollback artifact on different filesystem")
    except ConfiguredReplacementPreconditionError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("Rollback artifact filesystem check failed") from exc
    _verify_file_owned(artifact_p, expected_size=size_bytes, expected_sha256=sha256)
    return artifact_p


def _copy_rollback_file(source: Path, dest_dir: Path, filename: str, *, size: int, sha256: str) -> None:
    dest = dest_dir / filename
    partial = dest_dir / ("." + filename + ".partial")
    if dest.exists() or dest.is_symlink():
        raise ConfiguredReplacementPreconditionError("Rollback artifact destination already exists")
    try:
        partial.unlink()
    except FileNotFoundError:
        pass
    src_fd = out_fd = None
    try:
        src_fd = os.open(str(source), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        bst = os.fstat(src_fd)
        if not stat.S_ISREG(bst.st_mode) or bst.st_size != size:
            raise ConfiguredReplacementPreconditionError("Rollback source invalid")
        out_fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG, 0o600)
        h = hashlib.sha256(); copied = 0
        while True:
            chunk = os.read(src_fd, 1 << 20)
            if not chunk: break
            h.update(chunk); copied += len(chunk)
            off = 0
            while off < len(chunk):
                n = os.write(out_fd, chunk[off:])
                if n <= 0: raise OSError("write failed")
                off += n
        if os.name != "nt":
            os.fsync(out_fd)
        ast = os.fstat(src_fd)
        if (bst.st_dev, bst.st_ino, bst.st_size) != (ast.st_dev, ast.st_ino, ast.st_size) or copied != size or h.hexdigest() != sha256:
            raise ConfiguredReplacementPreconditionError("Rollback source changed during copy")
        os.close(src_fd); src_fd = None
        os.close(out_fd); out_fd = None
        if dest.exists() or dest.is_symlink():
            raise ConfiguredReplacementPreconditionError("Rollback artifact destination appeared during copy")
        os.replace(str(partial), str(dest))
        if os.name != "nt":
            try: os.chmod(str(dest), 0o600)
            except OSError: pass
        _verify_file_owned(dest, expected_size=size, expected_sha256=sha256)
        _fsync_path(dest)
        _fsync_path(dest_dir, directory=True)
    except (OSError, ConfiguredReplacementPreconditionError):
        for fd in (src_fd, out_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        try: partial.unlink()
        except OSError: pass
        raise
    finally:
        for fd in (src_fd, out_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass


def _stage_rollback_artifact(
    *, operation_id: str, safety_snapshot: ValidatedBackupSnapshot,
    target: DatabaseTarget, index: int,
) -> Path:
    """Stage rollback artifact from safety backup beside destination. Idempotent."""
    tkey = target.target_key
    entry = next((e for e in safety_snapshot.entries if e.target_key == tkey), None)
    if entry is None:
        raise ConfiguredReplacementPreconditionError(f"No safety backup entry for '{tkey}'")
    rbfile = _rollback_artifact_name(index, tkey)
    dest_parent = target.path.parent.resolve()
    rb_dir = dest_parent / _rollback_dir_name(operation_id, index)

    if rb_dir.exists():
        if rb_dir.is_symlink() or has_symlink_component(rb_dir):
            raise ConfiguredReplacementPreconditionError("Rollback directory path contains symlinks")
        rd_st = os.stat(str(rb_dir), follow_symlinks=False)
        if not stat.S_ISDIR(rd_st.st_mode):
            raise ConfiguredReplacementPreconditionError("Rollback directory is not a directory")
        if os.name != "nt" and stat.S_IMODE(rd_st.st_mode) != 0o700:
            raise ConfiguredReplacementPreconditionError("Rollback directory permissions invalid")
        expected_ch = {_ROLLBACK_BINDING_NAME, rbfile}
        try:
            actual_ch = {c.name for c in rb_dir.iterdir()}
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError("Could not list rollback directory") from exc
        if actual_ch != expected_ch:
            extra = actual_ch - expected_ch
            if extra:
                raise ConfiguredReplacementPreconditionError(f"Rollback directory foreign children: {extra}")
            raise ConfiguredReplacementPreconditionError("Rollback directory children mismatch")
        return _verify_rollback_binding(
            rb_dir, operation_id=operation_id, safety_backup_id=safety_snapshot.backup_id,
            safety_manifest_sha256=safety_snapshot.manifest_sha256, target_key=tkey,
            kind=entry.kind, index=index, rollback_filename=rbfile,
            size_bytes=entry.size_bytes, sha256=entry.sha256,
        )

    try:
        dest_parent_dev = os.lstat(str(dest_parent)).st_dev
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("Could not stat destination parent") from exc

    src_file = safety_snapshot.directory / entry.filename
    if has_symlink_component(src_file) or src_file.is_symlink() or not src_file.exists():
        raise ConfiguredReplacementPreconditionError("Safety backup source file missing or unsafe")

    try:
        rb_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("Could not create rollback directory") from exc
    if os.name != "nt":
        try: os.chmod(str(rb_dir), 0o700)
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError("Could not chmod rollback directory") from exc
    try:
        rb_dev = os.lstat(str(rb_dir)).st_dev
        if rb_dev != dest_parent_dev:
            raise ConfiguredReplacementPreconditionError("Rollback directory on different filesystem")
    except ConfiguredReplacementPreconditionError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("Rollback dir filesystem check failed") from exc

    _write_rollback_binding(
        rb_dir, operation_id=operation_id, safety_backup_id=safety_snapshot.backup_id,
        safety_manifest_sha256=safety_snapshot.manifest_sha256, target_key=tkey,
        kind=entry.kind, index=index, rollback_filename=rbfile,
        size_bytes=entry.size_bytes, sha256=entry.sha256,
    )
    _copy_rollback_file(src_file, rb_dir, rbfile, size=entry.size_bytes, sha256=entry.sha256)
    return _verify_rollback_binding(
        rb_dir, operation_id=operation_id, safety_backup_id=safety_snapshot.backup_id,
        safety_manifest_sha256=safety_snapshot.manifest_sha256, target_key=tkey,
        kind=entry.kind, index=index, rollback_filename=rbfile,
        size_bytes=entry.size_bytes, sha256=entry.sha256,
    )


# ---------------------------------------------------------------------------
# Sidecar handling
# ---------------------------------------------------------------------------

def _handle_configured_sidecars(
    destination: Path,
    journal: RestoreJournal,
    target_key: str,
    restore_root: Path,
) -> RestoreJournal:
    """Remove named WAL/SHM sidecars with durable journal recording. No wildcard."""
    op_id = journal.operation_id
    cur = journal

    for suffix in ("-wal", "-shm"):
        is_wal = suffix == "-wal"
        pres_attr = "wal_present" if is_wal else "shm_present"
        rem_attr = "wal_removed" if is_wal else "shm_removed"
        fact = next(f for f in cur.targets if f.target_key == target_key)
        was_pres = getattr(fact, pres_attr)
        was_rem = getattr(fact, rem_attr)
        sidecar = destination.parent / (destination.name + suffix)

        if was_rem:
            try:
                os.lstat(str(sidecar))
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} still present after recorded removal"
                )
            except FileNotFoundError:
                pass
            continue

        if was_pres:
            try:
                sc_st = os.lstat(str(sidecar))
                if stat.S_ISDIR(sc_st.st_mode):
                    raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is a directory")
                if stat.S_ISLNK(sc_st.st_mode):
                    raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is a symlink")
                if not stat.S_ISREG(sc_st.st_mode):
                    raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is not a regular file")
                os.unlink(str(sidecar))
            except FileNotFoundError:
                pass
            except ConfiguredReplacementPreconditionError:
                raise
            except OSError as exc:
                raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} removal failed") from exc
            _fsync_path(destination.parent, directory=True)
            cur = _journal_transition(op_id, restore_root, target_key=target_key, **{rem_attr: True})
            continue

        try:
            sc_st = os.lstat(str(sidecar))
        except FileNotFoundError:
            cur = _journal_transition(op_id, restore_root, target_key=target_key, **{pres_attr: False, rem_attr: False})
            continue

        if stat.S_ISDIR(sc_st.st_mode):
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is a directory")
        if stat.S_ISLNK(sc_st.st_mode):
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is a symlink")
        if not stat.S_ISREG(sc_st.st_mode):
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is not a regular file")

        before_id = (sc_st.st_dev, sc_st.st_ino, sc_st.st_size, getattr(sc_st, "st_mtime_ns", None))
        cur = _journal_transition(op_id, restore_root, target_key=target_key, **{pres_attr: True})

        try:
            after_st = os.lstat(str(sidecar))
            after_id = (after_st.st_dev, after_st.st_ino, after_st.st_size, getattr(after_st, "st_mtime_ns", None))
            if before_id != after_id:
                raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} changed after presence recording")
        except FileNotFoundError:
            pass
        except ConfiguredReplacementPreconditionError:
            raise
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} re-stat failed") from exc

        try:
            os.unlink(str(sidecar))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} removal failed") from exc

        _fsync_path(destination.parent, directory=True)
        cur = _journal_transition(op_id, restore_root, target_key=target_key, **{rem_attr: True})

    return cur


# ---------------------------------------------------------------------------
# Journal transition helper
# ---------------------------------------------------------------------------

def _journal_transition(
    operation_id: str,
    root: Path,
    stage: RestoreStage | None = None,
    target_key: str | None = None,
    target_state: TargetRestoreState | None = None,
    **kwargs,
) -> RestoreJournal:
    """Update journal, reread, and verify exact match."""
    try:
        updated = update_restore_journal(
            operation_id, root=root, stage=stage,
            target_key=target_key, target_state=target_state, **kwargs,
        )
        reread = load_restore_journal(operation_id, root=root)
        if reread != updated:
            raise RestoreJournalPersistenceError("Journal reread mismatch")
        return reread
    except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
        raise ConfiguredJournalUncertaintyError(
            "Journal transition could not be persisted or verified"
        ) from exc


# ---------------------------------------------------------------------------
# Pre-mutation proof barrier (REPLACEMENT_READY)
# ---------------------------------------------------------------------------

def _verify_barrier_pre_mutation(
    *,
    operation_id: str,
    expected_application_commit: str,
    selected_backup_id: str,
    selected_snapshot: ValidatedBackupSnapshot,
    safety_backup_id: str,
    safety_snapshot: ValidatedBackupSnapshot,
    targets: tuple,
    confirmed_target_set_hash: str,
    confirmed_restore_value: str,
    journal: RestoreJournal,
    restore_root: Path,
    backup_root: Path,
    evidence: DestinationBaselineEvidence,
) -> None:
    """Full proof barrier for REPLACEMENT_READY stage (before any mutation)."""
    _verify_project_root(expected_application_commit)

    try:
        from importlib.metadata import version as _ver
        if not _ver("garminconnect"):
            raise ConfiguredReplacementPreconditionError("garminconnect package version missing")
    except ConfiguredReplacementPreconditionError:
        raise
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError("garminconnect package verification failed") from exc

    if selected_snapshot.backup_id != selected_backup_id:
        raise ConfiguredReplacementPreconditionError("Selected backup ID mismatch")
    sel_dir = backup_root / f"backup-{selected_backup_id}"
    try:
        rsel = load_validated_backup_snapshot(sel_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredReplacementPreconditionError("Selected backup revalidation failed") from exc
    if rsel.manifest_sha256 != selected_snapshot.manifest_sha256:
        raise ConfiguredReplacementPreconditionError("Selected backup manifest SHA-256 drift")

    if safety_backup_id == selected_backup_id:
        raise ConfiguredReplacementPreconditionError("Safety backup ID matches selected backup ID")
    saf_dir = backup_root / f"backup-{safety_backup_id}"
    try:
        rsaf = load_validated_backup_snapshot(saf_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredReplacementPreconditionError("Safety backup revalidation failed") from exc
    if rsaf.manifest_sha256 != safety_snapshot.manifest_sha256:
        raise ConfiguredReplacementPreconditionError("Safety backup manifest SHA-256 drift")

    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    try:
        cur_tgts = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError("Target discovery failed") from exc
    if tuple(t.target_key for t in cur_tgts) != tuple(t.target_key for t in targets):
        raise ConfiguredReplacementPreconditionError("Runtime target keys mismatch")

    rh = target_set_hash(
        backup_id=selected_backup_id, manifest_sha256=selected_snapshot.manifest_sha256,
        runtime_mode=runtime_mode, target_keys=tuple(t.target_key for t in targets),
    )
    rc = confirmation_value(target_hash=rh, expected_application_commit=expected_application_commit)
    if confirmed_target_set_hash != rh or confirmed_restore_value != rc:
        raise ConfiguredReplacementPreconditionError("Target-set hash or confirmation value drift")

    if (
        journal.operation_id != operation_id
        or journal.selected_backup_id != selected_backup_id
        or journal.selected_backup_manifest_sha256 != selected_snapshot.manifest_sha256
        or journal.safety_backup_id != safety_backup_id
        or journal.expected_application_commit != expected_application_commit
        or journal.runtime_mode != runtime_mode
        or journal.target_keys != tuple(t.target_key for t in targets)
        or journal.target_set_hash != rh
        or journal.confirmation_value != rc
    ):
        raise ConfiguredReplacementPreconditionError("Journal immutable fields mismatch")
    if journal.destination_baseline_sha256 is None:
        raise ConfiguredReplacementPreconditionError("Journal missing destination baseline SHA-256")

    ev, sha_hex = load_destination_baseline_evidence(operation_id, restore_root=restore_root)
    if sha_hex != journal.destination_baseline_sha256:
        raise ConfiguredReplacementPreconditionError("Destination baseline SHA-256 mismatch")
    revalidate_destination_baseline_evidence(
        ev, targets, expected_application_commit, operation_id=operation_id,
        selected_backup_id=selected_backup_id,
        selected_backup_manifest_sha256=selected_snapshot.manifest_sha256,
        runtime_mode=runtime_mode, target_set_hash=rh, confirmation_value=rc,
    )

    if journal.stage is not RestoreStage.REPLACEMENT_READY:
        raise ConfiguredReplacementPreconditionError("Journal stage must be REPLACEMENT_READY at pre-mutation barrier")
    for fact in journal.targets:
        if fact.state is not TargetRestoreState.STAGED_VERIFIED:
            raise ConfiguredReplacementPreconditionError("Not all targets STAGED_VERIFIED at pre-mutation barrier")
        if any([fact.wal_removed, fact.shm_removed, fact.replacement_intent,
                fact.replacement_completed, fact.rollback_intent, fact.rollback_completed]):
            raise ConfiguredReplacementPreconditionError("Mutation flags present at pre-mutation barrier")

    sel_entries = {e.target_key: e for e in selected_snapshot.entries}
    for idx, tgt in enumerate(targets):
        entry = sel_entries[tgt.target_key]
        stage_dir = tgt.path.parent.resolve() / _staged_dir_name(operation_id)
        staged_p = stage_dir / _staged_artifact_name(idx, tgt.target_key)
        if not staged_p.exists() or staged_p.is_symlink():
            raise ConfiguredReplacementPreconditionError(f"Staged artifact missing for '{tgt.target_key}'")
        _verify_file_owned(staged_p, expected_size=entry.size_bytes, expected_sha256=entry.sha256)
        try:
            dest_dev = os.lstat(str(tgt.path.parent.resolve())).st_dev
            stage_dev = os.lstat(str(staged_p)).st_dev
            if dest_dev != stage_dev:
                raise ConfiguredReplacementPreconditionError(f"Staged artifact on different filesystem for '{tgt.target_key}'")
        except ConfiguredReplacementPreconditionError:
            raise
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError("Filesystem check failed") from exc


# ---------------------------------------------------------------------------
# Complete postcheck
# ---------------------------------------------------------------------------

def _run_complete_postcheck(
    *,
    operation_id: str,
    selected_snapshot: ValidatedBackupSnapshot,
    safety_snapshot: ValidatedBackupSnapshot,
    targets: tuple,
    backup_root: Path,
    restore_root: Path,
) -> None:
    """Complete postcheck per contract section 10."""
    journal = load_restore_journal(operation_id, root=restore_root)
    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    if journal.runtime_mode != runtime_mode:
        raise ConfiguredReplacementPostcheckError("Postcheck: runtime mode mismatch")

    try:
        cur_tgts = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredReplacementPostcheckError("Postcheck: target discovery failed") from exc
    if tuple(t.target_key for t in cur_tgts) != tuple(t.target_key for t in targets):
        raise ConfiguredReplacementPostcheckError("Postcheck: target set mismatch")
    if tuple(t.target_key for t in cur_tgts) != journal.target_keys:
        raise ConfiguredReplacementPostcheckError("Postcheck: target keys mismatch journal")

    for fact in journal.targets:
        if fact.state is not TargetRestoreState.REPLACED or not fact.replacement_completed:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: target '{fact.target_key}' not fully replaced")
        if fact.rollback_intent or fact.rollback_completed:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: rollback flags set for '{fact.target_key}'")

    sel_entries = {e.target_key: e for e in selected_snapshot.entries}
    for tgt in targets:
        entry = sel_entries.get(tgt.target_key)
        if entry is None:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: no backup entry for '{tgt.target_key}'")
        _verify_file_owned(tgt.path, expected_size=entry.size_bytes, expected_sha256=entry.sha256)
        check = inspect_sqlite(tgt.path, deep=True)
        if not check.readable or not check.quick_check_ok:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: quick_check failed for '{tgt.target_key}'")
        if not check.integrity_check_ok:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: integrity_check failed for '{tgt.target_key}'")
        if not check.foreign_keys_ok:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: foreign_key_check failed for '{tgt.target_key}'")
        fp = schema_fingerprint(tgt.path)
        if fp != entry.schema_fingerprint:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: schema fingerprint mismatch for '{tgt.target_key}'")
        markers = migration_markers(tgt.path, entry.kind)
        if markers != {"ledger": entry.migration_ledger, "keys": list(entry.migration_keys), "state": entry.migration_state}:
            raise ConfiguredReplacementPostcheckError(f"Postcheck: migration markers mismatch for '{tgt.target_key}'")
        if os.name != "nt":
            dst = os.stat(str(tgt.path), follow_symlinks=False)
            if stat.S_IMODE(dst.st_mode) not in {0o600, 0o400}:
                raise ConfiguredReplacementPostcheckError(f"Postcheck: mode not private for '{tgt.target_key}'")
        for sfx in ("-wal", "-shm"):
            if (tgt.path.parent / (tgt.path.name + sfx)).exists():
                raise ConfiguredReplacementPostcheckError(f"Postcheck: {sfx} sidecar present for '{tgt.target_key}'")

    try:
        rsel = load_validated_backup_snapshot(backup_root / f"backup-{selected_snapshot.backup_id}", against_current_config=True)
    except BackupError as exc:
        raise ConfiguredReplacementPostcheckError("Postcheck: selected backup drift") from exc
    if rsel.manifest_sha256 != selected_snapshot.manifest_sha256:
        raise ConfiguredReplacementPostcheckError("Postcheck: selected backup manifest drift")

    try:
        rsaf = load_validated_backup_snapshot(backup_root / f"backup-{safety_snapshot.backup_id}", against_current_config=True)
    except BackupError as exc:
        raise ConfiguredReplacementPostcheckError("Postcheck: safety backup drift") from exc
    if rsaf.manifest_sha256 != safety_snapshot.manifest_sha256:
        raise ConfiguredReplacementPostcheckError("Postcheck: safety backup manifest drift")


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def _run_rollback(
    *,
    operation_id: str,
    selected_snapshot: ValidatedBackupSnapshot,
    safety_snapshot: ValidatedBackupSnapshot,
    targets: tuple,
    restore_root: Path,
    baseline_evidence: "DestinationBaselineEvidence | None" = None,
) -> None:
    """Perform re-entrant rollback: control first, then data in reverse. Always raises."""
    try:
        journal = load_restore_journal(operation_id, root=restore_root)
        if journal.stage is not RestoreStage.ROLLBACK_REQUIRED:
            _journal_transition(operation_id, restore_root, stage=RestoreStage.ROLLBACK_REQUIRED)
    except ConfiguredJournalUncertaintyError:
        raise ConfiguredReplacementManualRecoveryRequiredError("Rollback: ROLLBACK_REQUIRED transition failed")
    except Exception as exc:
        raise ConfiguredReplacementManualRecoveryRequiredError("Rollback: journal load failed") from exc

    saf_entries = {e.target_key: e for e in safety_snapshot.entries}
    sel_entries = {e.target_key: e for e in selected_snapshot.entries}

    ctrl_idx = [i for i, t in enumerate(targets) if t.kind == "control"]
    data_idx = [i for i, t in enumerate(targets) if t.kind != "control"]
    rollback_order = ctrl_idx + list(reversed(data_idx))

    try:
        for index in rollback_order:
            tgt = targets[index]
            sentry = saf_entries.get(tgt.target_key)
            sentry_sel = sel_entries.get(tgt.target_key)
            if sentry is None or sentry_sel is None:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: missing entry for '{tgt.target_key}'"
                )
            journal = load_restore_journal(operation_id, root=restore_root)
            fact = next((f for f in journal.targets if f.target_key == tgt.target_key), None)
            if fact is None:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: no fact for '{tgt.target_key}'"
                )
            if fact.rollback_completed and fact.state is TargetRestoreState.ROLLED_BACK:
                try:
                    _verify_file_owned(tgt.path, expected_size=sentry.size_bytes, expected_sha256=sentry.sha256)
                except Exception as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: rolled-back target '{tgt.target_key}' does not match safety bytes"
                    ) from exc
                continue

            is_sel = is_saf = False
            if tgt.path.exists() and not tgt.path.is_symlink():
                try:
                    _verify_file_owned(tgt.path, expected_size=sentry.size_bytes, expected_sha256=sentry.sha256)
                    is_saf = True
                except Exception:
                    pass
                if not is_saf:
                    try:
                        _verify_file_owned(tgt.path, expected_size=sentry_sel.size_bytes, expected_sha256=sentry_sel.sha256)
                        is_sel = True
                    except Exception:
                        pass

            was_replaced = fact.state is TargetRestoreState.REPLACED or fact.replacement_completed or is_sel
            if not was_replaced:
                if is_saf:
                    continue
                # SQLite backup produces different bytes than the source.  If the
                # current destination still has original baseline bytes (never
                # replaced) it is also safe to skip rollback for this target.
                if baseline_evidence is not None:
                    base_rec = next(
                        (t for t in baseline_evidence.targets if t.target_key == tgt.target_key), None
                    )
                    if base_rec is not None:
                        try:
                            _verify_file_owned(
                                tgt.path,
                                expected_size=base_rec.size_bytes,
                                expected_sha256=base_rec.sha256,
                            )
                            continue  # Still original bytes — target was never replaced
                        except Exception:
                            pass
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: target '{tgt.target_key}' matches neither safety, selected, nor baseline bytes"
                )

            if fact.state is not TargetRestoreState.REPLACED or not fact.replacement_completed:
                try:
                    _journal_transition(operation_id, restore_root, target_key=tgt.target_key,
                                        target_state=TargetRestoreState.REPLACED, replacement_completed=True)
                except ConfiguredJournalUncertaintyError:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: replacement reconciliation failed for '{tgt.target_key}'"
                    )

            journal = load_restore_journal(operation_id, root=restore_root)
            fact = next(f for f in journal.targets if f.target_key == tgt.target_key)
            if not fact.rollback_intent:
                try:
                    _journal_transition(operation_id, restore_root, target_key=tgt.target_key, rollback_intent=True)
                except ConfiguredJournalUncertaintyError:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: intent journal failed for '{tgt.target_key}'"
                    )

            already_safe = False
            try:
                _verify_file_owned(tgt.path, expected_size=sentry.size_bytes, expected_sha256=sentry.sha256)
                already_safe = True
            except Exception:
                already_safe = False

            if not already_safe:
                rbfile = _rollback_artifact_name(index, tgt.target_key)
                rb_dir = tgt.path.parent.resolve() / _rollback_dir_name(operation_id, index)
                try:
                    rb_artifact = _verify_rollback_binding(
                        rb_dir, operation_id=operation_id, safety_backup_id=safety_snapshot.backup_id,
                        safety_manifest_sha256=safety_snapshot.manifest_sha256, target_key=tgt.target_key,
                        kind=sentry.kind, index=index, rollback_filename=rbfile,
                        size_bytes=sentry.size_bytes, sha256=sentry.sha256,
                    )
                except ConfiguredReplacementPreconditionError as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: artifact verification failed for '{tgt.target_key}'"
                    ) from exc

                journal = load_restore_journal(operation_id, root=restore_root)
                try:
                    journal = _handle_configured_sidecars(tgt.path, journal, tgt.target_key, restore_root)
                except Exception as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: sidecar handling failed for '{tgt.target_key}'"
                    ) from exc

                try:
                    dest_dev = os.lstat(str(tgt.path.parent.resolve())).st_dev
                    art_dev = os.lstat(str(rb_artifact)).st_dev
                    if dest_dev != art_dev:
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: artifact on different filesystem for '{tgt.target_key}'"
                        )
                except ConfiguredReplacementManualRecoveryRequiredError:
                    raise
                except OSError as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: filesystem check failed for '{tgt.target_key}'"
                    ) from exc

                try:
                    os.replace(str(rb_artifact), str(tgt.path))
                except OSError as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: os.replace failed for '{tgt.target_key}'"
                    ) from exc
                if os.name != "nt":
                    try: os.chmod(str(tgt.path), 0o600)
                    except OSError: pass
                try:
                    _verify_file_owned(tgt.path, expected_size=sentry.size_bytes, expected_sha256=sentry.sha256)
                except Exception as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: post-replace verification failed for '{tgt.target_key}'"
                    ) from exc
                chk = inspect_sqlite(tgt.path)
                if not chk.readable or not chk.quick_check_ok:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: SQLite check failed for '{tgt.target_key}'"
                    )
                _fsync_path(tgt.path)
                _fsync_path(tgt.path.parent.resolve(), directory=True)

            try:
                _journal_transition(operation_id, restore_root, target_key=tgt.target_key,
                                    target_state=TargetRestoreState.ROLLED_BACK, rollback_completed=True)
            except ConfiguredJournalUncertaintyError:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: completion journal failed for '{tgt.target_key}'"
                )

        _journal_transition(operation_id, restore_root, stage=RestoreStage.ROLLED_BACK)
        _journal_transition(operation_id, restore_root, stage=RestoreStage.FAILED_SAFE)

        try:
            _cleanup_evidence(operation_id, targets)
        except ConfiguredReplacementCleanupError:
            raise
        raise ConfiguredReplacementRollbackCompletedError(
            "Configured restore rolled back; all replaced targets restored to safety bytes"
        )

    except (ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementCleanupError):
        raise
    except ConfiguredReplacementManualRecoveryRequiredError:
        _settle_manual(operation_id, restore_root)
        raise
    except Exception as exc:
        _settle_manual(operation_id, restore_root)
        raise ConfiguredReplacementManualRecoveryRequiredError("Rollback failure") from exc


# ---------------------------------------------------------------------------
# Evidence cleanup
# ---------------------------------------------------------------------------

def _cleanup_evidence(operation_id: str, targets: tuple) -> None:
    """Strict owned-evidence cleanup: no recursive rmtree, no wildcard."""
    errors: list[str] = []
    seen_dirs: set[Path] = set()
    for idx, tgt in enumerate(targets):
        dest_parent = tgt.path.parent.resolve()
        for dirname in (_staged_dir_name(operation_id), _rollback_dir_name(operation_id, idx)):
            d = dest_parent / dirname
            if d in seen_dirs or not d.exists():
                continue
            try:
                _cleanup_single_dir(d, is_rollback=("rollback" in dirname))
                seen_dirs.add(d)
            except Exception as exc:
                errors.append(f"{dirname}: {exc}")
    if errors:
        raise ConfiguredReplacementCleanupError(f"Evidence cleanup incomplete: {'; '.join(errors)}")


def _cleanup_single_dir(directory: Path, *, is_rollback: bool) -> None:
    if directory.is_symlink() or has_symlink_component(directory):
        raise ConfiguredReplacementCleanupError("Directory path contains symlinks")
    try:
        dst = os.stat(str(directory), follow_symlinks=False)
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not stat directory") from exc
    if not stat.S_ISDIR(dst.st_mode):
        raise ConfiguredReplacementCleanupError("Path is not a directory")
    if os.name != "nt" and stat.S_IMODE(dst.st_mode) != 0o700:
        raise ConfiguredReplacementCleanupError("Directory permissions invalid")
    dir_id = (dst.st_dev, dst.st_ino)
    binding_name = _ROLLBACK_BINDING_NAME if is_rollback else ".staging-binding.json"
    binding_p = directory / binding_name
    if not binding_p.exists() or binding_p.is_symlink():
        raise ConfiguredReplacementCleanupError("Binding file missing or unsafe")
    try:
        raw = binding_p.read_bytes()
        if len(raw) > _MAX_BINDING_BYTES:
            raise ConfiguredReplacementCleanupError("Binding file too large")
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ConfiguredReplacementCleanupError("Could not parse binding file") from exc
    if is_rollback:
        artifact_names = {parsed.get("rollback_filename", "")}
    else:
        artifact_names = {a.get("staged_filename", "") for a in parsed.get("artifacts", [])}
    expected_names = {binding_name} | artifact_names
    try:
        actual = {c.name for c in directory.iterdir()}
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not list directory") from exc
    extra = actual - expected_names
    if extra:
        raise ConfiguredReplacementCleanupError(f"Unexpected children: {extra}")
    # Artifacts absent from 'actual' have been consumed by os.replace during replacement/rollback.
    for name in actual:
        child = directory / name
        try:
            cst = os.lstat(str(child))
        except OSError as exc:
            raise ConfiguredReplacementCleanupError(f"Could not stat '{name}'") from exc
        if stat.S_ISLNK(cst.st_mode) or not stat.S_ISREG(cst.st_mode):
            raise ConfiguredReplacementCleanupError(f"Child '{name}' is not a regular file")
    file_recs: dict[str, tuple] = {}
    bst = os.lstat(str(binding_p))
    bind_rec = (bst.st_dev, bst.st_ino, bst.st_size)
    for name in artifact_names:
        child = directory / name
        try:
            cst = os.lstat(str(child))
            file_recs[name] = (cst.st_dev, cst.st_ino, cst.st_size)
        except FileNotFoundError:
            pass  # Artifact consumed by os.replace - nothing to unlink
        except OSError as exc:
            raise ConfiguredReplacementCleanupError(f"Could not stat artifact '{name}'") from exc
    try:
        curr = os.stat(str(directory), follow_symlinks=False)
        if (curr.st_dev, curr.st_ino) != dir_id:
            raise ConfiguredReplacementCleanupError("Directory identity changed")
    except ConfiguredReplacementCleanupError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Directory re-stat failed") from exc
    for name, (dev, ino, sz) in file_recs.items():
        child = directory / name
        try:
            cst = os.lstat(str(child))
            if (cst.st_dev, cst.st_ino, cst.st_size) != (dev, ino, sz):
                raise ConfiguredReplacementCleanupError(f"Artifact '{name}' changed before unlink")
            os.unlink(str(child))
        except ConfiguredReplacementCleanupError:
            raise
        except OSError as exc:
            raise ConfiguredReplacementCleanupError(f"Could not unlink '{name}'") from exc
    try:
        cbst = os.lstat(str(binding_p))
        if (cbst.st_dev, cbst.st_ino, cbst.st_size) != bind_rec:
            raise ConfiguredReplacementCleanupError("Binding changed before unlink")
        os.unlink(str(binding_p))
    except ConfiguredReplacementCleanupError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not unlink binding") from exc
    try:
        rem = list(directory.iterdir())
        if rem:
            raise ConfiguredReplacementCleanupError("Directory not empty after cleanup")
    except ConfiguredReplacementCleanupError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not verify directory empty") from exc
    _fsync_path(directory, directory=True)
    try:
        directory.rmdir()
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not rmdir") from exc
    _fsync_path(directory.parent, directory=True)


# ---------------------------------------------------------------------------
# Journal settlement helpers
# ---------------------------------------------------------------------------

def _settle_safe(operation_id: str, restore_root: Path, journal: RestoreJournal) -> None:
    """Best-effort settle to FAILED_SAFE for pre-mutation failures."""
    try:
        if journal.stage not in {
            RestoreStage.COMPLETED, RestoreStage.FAILED_SAFE,
            RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
        } and RestoreStage.FAILED_SAFE in _GLOBAL_TRANSITIONS.get(journal.stage, set()):
            update_restore_journal(operation_id, root=restore_root, stage=RestoreStage.FAILED_SAFE)
    except Exception:
        pass


def _settle_manual(operation_id: str, restore_root: Path) -> None:
    """Best-effort settle to FAILED_MANUAL_RECOVERY_REQUIRED."""
    try:
        j = load_restore_journal(operation_id, root=restore_root)
        if j.stage not in {
            RestoreStage.COMPLETED, RestoreStage.FAILED_SAFE,
            RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
        } and j.final_result is None:
            update_restore_journal(operation_id, root=restore_root,
                                   stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def replace_and_verify_configured_restore(
    *,
    operation_id: str,
    selected_backup_id: str,
    expected_application_commit: str,
    confirmed_target_set_hash: str,
    confirmed_restore_value: str,
) -> ConfiguredReplacementResult:
    """Execute configured-runtime restore replacement, postcheck, and rollback.

    Accepts no arbitrary paths. Rediscovers canonical targets from config.
    Acquires ProcessLock -> RestoreLock -> BackupLock before any mutation.
    Releases all locks before returning a frozen result.
    Raises a bounded exception on every failure path.
    """
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ConfiguredReplacementPreconditionError("Operation ID format is invalid")
    if not _BACKUP_ID_RE.fullmatch(selected_backup_id):
        raise ConfiguredReplacementPreconditionError("Selected backup ID format is invalid")

    restore_root = validate_restore_root(config.OPERATOR_RESTORE_ROOT)
    backup_root = validate_backup_root(config.OPERATOR_BACKUP_ROOT)
    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"

    journal = load_restore_journal(operation_id, root=restore_root)
    if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Operation is in FAILED_MANUAL_RECOVERY_REQUIRED"
        )
    if journal.selected_backup_id != selected_backup_id:
        raise ConfiguredReplacementPreconditionError("selected_backup_id does not match journal")
    if journal.expected_application_commit != expected_application_commit:
        raise ConfiguredReplacementPreconditionError("expected_application_commit does not match journal")
    if journal.runtime_mode != runtime_mode:
        raise ConfiguredReplacementPreconditionError("runtime_mode does not match journal")

    try:
        configured_targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError("Target discovery failed") from exc
    if tuple(t.target_key for t in configured_targets) != journal.target_keys:
        raise ConfiguredReplacementPreconditionError("Discovered target keys mismatch journal")

    rh = target_set_hash(
        backup_id=selected_backup_id, manifest_sha256=journal.selected_backup_manifest_sha256,
        runtime_mode=runtime_mode, target_keys=journal.target_keys,
    )
    rc = confirmation_value(target_hash=rh, expected_application_commit=expected_application_commit)
    if confirmed_target_set_hash != rh:
        raise ConfiguredReplacementPreconditionError("confirmed_target_set_hash mismatch")
    if confirmed_restore_value != rc:
        raise ConfiguredReplacementPreconditionError("confirmed_restore_value mismatch")
    if journal.target_set_hash != rh or journal.confirmation_value != rc:
        raise ConfiguredReplacementPreconditionError("Journal target-set hash or confirmation mismatch")

    legal_stages = {
        RestoreStage.REPLACEMENT_READY, RestoreStage.REPLACING,
        RestoreStage.REPLACED, RestoreStage.POSTCHECK_PASSED, RestoreStage.COMPLETED,
        RestoreStage.ROLLBACK_REQUIRED, RestoreStage.ROLLED_BACK, RestoreStage.FAILED_SAFE,
    }
    if journal.stage not in legal_stages:
        raise ConfiguredReplacementPreconditionError(
            f"Journal stage '{journal.stage}' is not a legal entry stage"
        )

    proc_lock: ProcessLock | None = None
    rest_lock: RestoreLock | None = None
    bkup_lock: BackupLock | None = None

    try:
        project_root = safe_resolve(config.PROJECT_ROOT)
        try:
            proc_lock = acquire_process_lock(project_root / "garmincoach.lock")
        except Exception as exc:
            raise RestoreLockError("Could not acquire application process lock") from exc
        rest_lock = RestoreLock(restore_root)
        try:
            rest_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire dedicated restore lock") from exc
        try:
            bkup_lock = BackupLock(backup_root)
            bkup_lock.__enter__()
        except Exception as exc:
            raise RestoreLockError("Could not acquire BackupLock") from exc

        journal = load_restore_journal(operation_id, root=restore_root)

        if journal.stage is RestoreStage.COMPLETED:
            try:
                _cleanup_evidence(operation_id, configured_targets)
            except ConfiguredReplacementCleanupError:
                raise
            replaced_keys = tuple(f.target_key for f in journal.targets if f.replacement_completed)
            return ConfiguredReplacementResult(
                operation_id=operation_id, stage=RestoreStage.COMPLETED,
                selected_backup_id=selected_backup_id, safety_backup_id=journal.safety_backup_id or "",
                runtime_mode=runtime_mode, target_keys=journal.target_keys,
                replaced_target_keys=replaced_keys, rollback_occurred=False,
                configured_database_mutated=True, locks_released=True,
            )
        if journal.stage is RestoreStage.FAILED_SAFE:
            raise ConfiguredReplacementRollbackCompletedError("Operation already settled FAILED_SAFE")
        if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
            raise ConfiguredReplacementManualRecoveryRequiredError("Operation in FAILED_MANUAL_RECOVERY_REQUIRED")
        if journal.stage is RestoreStage.ROLLED_BACK:
            _journal_transition(operation_id, restore_root, stage=RestoreStage.FAILED_SAFE)
            raise ConfiguredReplacementRollbackCompletedError("Advanced ROLLED_BACK to FAILED_SAFE")

        safety_backup_id = journal.safety_backup_id
        if safety_backup_id is None:
            raise ConfiguredReplacementPreconditionError("Safety backup ID missing from journal")

        # When re-entering at REPLACING or later stages the configured databases may
        # already be in a partially replaced or otherwise modified state.  Checking
        # against_current_config=True would read those databases to validate schema
        # compatibility, which is inappropriate post-mutation.  The replacement loop
        # and postcheck perform their own state verification.  Only for
        # REPLACEMENT_READY (pre-mutation) do we need the current-config check, and
        # that is already covered by _verify_barrier_pre_mutation below.
        _post_mutation_stages = {
            RestoreStage.REPLACING, RestoreStage.REPLACED, RestoreStage.POSTCHECK_PASSED,
            RestoreStage.ROLLBACK_REQUIRED, RestoreStage.ROLLED_BACK,
        }
        _check_current = journal.stage not in _post_mutation_stages

        try:
            sel_snap = load_validated_backup_snapshot(
                backup_root / f"backup-{selected_backup_id}", against_current_config=_check_current
            )
        except BackupError as exc:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError("Selected backup validation failed") from exc
        try:
            saf_snap = load_validated_backup_snapshot(
                backup_root / f"backup-{safety_backup_id}", against_current_config=_check_current
            )
        except BackupError as exc:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError("Safety backup validation failed") from exc

        try:
            evidence, _ev_sha = load_destination_baseline_evidence(operation_id, restore_root=restore_root)
        except Exception as exc:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError("Baseline evidence load failed") from exc

        # ---- ROLLBACK_REQUIRED ----
        if journal.stage is RestoreStage.ROLLBACK_REQUIRED:
            _run_rollback(
                operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                targets=configured_targets, restore_root=restore_root,
                baseline_evidence=evidence,
            )
            raise ConfiguredReplacementManualRecoveryRequiredError("Rollback did not raise")

        # ---- REPLACED ----
        if journal.stage is RestoreStage.REPLACED:
            try:
                _run_complete_postcheck(
                    operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                    targets=configured_targets, backup_root=backup_root, restore_root=restore_root,
                )
                journal = _journal_transition(operation_id, restore_root, stage=RestoreStage.POSTCHECK_PASSED)
            except ConfiguredReplacementPostcheckError:
                _run_rollback(
                    operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                    targets=configured_targets, restore_root=restore_root,
                    baseline_evidence=evidence,
                )
                raise ConfiguredReplacementManualRecoveryRequiredError("Postcheck failed; rollback did not raise")

        # ---- POSTCHECK_PASSED ----
        if journal.stage is RestoreStage.POSTCHECK_PASSED:
            try:
                _run_complete_postcheck(
                    operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                    targets=configured_targets, backup_root=backup_root, restore_root=restore_root,
                )
            except ConfiguredReplacementPostcheckError:
                _run_rollback(
                    operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                    targets=configured_targets, restore_root=restore_root,
                    baseline_evidence=evidence,
                )
                raise ConfiguredReplacementManualRecoveryRequiredError("POSTCHECK_PASSED re-verify failed")
            journal = _journal_transition(operation_id, restore_root, stage=RestoreStage.COMPLETED)
            try:
                _cleanup_evidence(operation_id, configured_targets)
            except ConfiguredReplacementCleanupError:
                raise
            replaced_keys = tuple(f.target_key for f in journal.targets if f.replacement_completed)
            return ConfiguredReplacementResult(
                operation_id=operation_id, stage=RestoreStage.COMPLETED,
                selected_backup_id=selected_backup_id, safety_backup_id=safety_backup_id,
                runtime_mode=runtime_mode, target_keys=journal.target_keys,
                replaced_target_keys=replaced_keys, rollback_occurred=False,
                configured_database_mutated=True, locks_released=True,
            )

        # ---- REPLACEMENT_READY ----
        if journal.stage is RestoreStage.REPLACEMENT_READY:
            try:
                _verify_barrier_pre_mutation(
                    operation_id=operation_id, expected_application_commit=expected_application_commit,
                    selected_backup_id=selected_backup_id, selected_snapshot=sel_snap,
                    safety_backup_id=safety_backup_id, safety_snapshot=saf_snap,
                    targets=configured_targets, confirmed_target_set_hash=confirmed_target_set_hash,
                    confirmed_restore_value=confirmed_restore_value, journal=journal,
                    restore_root=restore_root, backup_root=backup_root, evidence=evidence,
                )
            except ConfiguredReplacementPreconditionError:
                _settle_safe(operation_id, restore_root, journal)
                raise

            for idx, tgt in enumerate(configured_targets):
                try:
                    _stage_rollback_artifact(
                        operation_id=operation_id, safety_snapshot=saf_snap, target=tgt, index=idx,
                    )
                except ConfiguredReplacementPreconditionError:
                    _settle_safe(operation_id, restore_root, journal)
                    raise

            try:
                _verify_barrier_pre_mutation(
                    operation_id=operation_id, expected_application_commit=expected_application_commit,
                    selected_backup_id=selected_backup_id, selected_snapshot=sel_snap,
                    safety_backup_id=safety_backup_id, safety_snapshot=saf_snap,
                    targets=configured_targets, confirmed_target_set_hash=confirmed_target_set_hash,
                    confirmed_restore_value=confirmed_restore_value, journal=journal,
                    restore_root=restore_root, backup_root=backup_root, evidence=evidence,
                )
            except ConfiguredReplacementPreconditionError:
                _settle_safe(operation_id, restore_root, journal)
                raise

            saf_entries = {e.target_key: e for e in saf_snap.entries}
            for idx, tgt in enumerate(configured_targets):
                sentry = saf_entries.get(tgt.target_key)
                if sentry is None:
                    _settle_safe(operation_id, restore_root, journal)
                    raise ConfiguredReplacementPreconditionError(f"No safety entry for '{tgt.target_key}'")
                rbfile = _rollback_artifact_name(idx, tgt.target_key)
                rb_dir = tgt.path.parent.resolve() / _rollback_dir_name(operation_id, idx)
                try:
                    _verify_rollback_binding(
                        rb_dir, operation_id=operation_id, safety_backup_id=safety_backup_id,
                        safety_manifest_sha256=saf_snap.manifest_sha256, target_key=tgt.target_key,
                        kind=sentry.kind, index=idx, rollback_filename=rbfile,
                        size_bytes=sentry.size_bytes, sha256=sentry.sha256,
                    )
                except ConfiguredReplacementPreconditionError:
                    _settle_safe(operation_id, restore_root, journal)
                    raise

            try:
                journal = _journal_transition(operation_id, restore_root, stage=RestoreStage.REPLACING)
            except ConfiguredJournalUncertaintyError:
                _settle_safe(operation_id, restore_root, journal)
                raise ConfiguredReplacementPreconditionError("REPLACING transition journal failed")

        # ---- REPLACING (fresh or re-entry) ----
        if journal.stage is not RestoreStage.REPLACING:
            raise ConfiguredReplacementManualRecoveryRequiredError(
                f"Unexpected journal stage '{journal.stage}' before replacement loop"
            )

        sel_entries = {e.target_key: e for e in sel_snap.entries}
        saf_entries = {e.target_key: e for e in saf_snap.entries}
        data_idx = [i for i, t in enumerate(configured_targets) if t.kind != "control"]
        ctrl_idx = [i for i, t in enumerate(configured_targets) if t.kind == "control"]
        replacement_order = data_idx + ctrl_idx

        try:
            for idx in replacement_order:
                tgt = configured_targets[idx]
                entry = sel_entries.get(tgt.target_key)
                sentry = saf_entries.get(tgt.target_key)
                if entry is None or sentry is None:
                    raise ConfiguredReplacementPreconditionError(f"Missing entry for '{tgt.target_key}'")

                journal = load_restore_journal(operation_id, root=restore_root)
                fact = next((f for f in journal.targets if f.target_key == tgt.target_key), None)
                if fact is None:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"No journal fact for '{tgt.target_key}'"
                    )

                # Already replaced and completed
                if fact.replacement_completed and fact.state is TargetRestoreState.REPLACED:
                    _verify_file_owned(tgt.path, expected_size=entry.size_bytes, expected_sha256=entry.sha256)
                    continue

                # Reconciliation: intent set but not completed
                if fact.replacement_intent:
                    is_sel = False
                    try:
                        _verify_file_owned(tgt.path, expected_size=entry.size_bytes, expected_sha256=entry.sha256)
                        is_sel = True
                    except Exception:
                        pass
                    if is_sel:
                        journal = _journal_transition(operation_id, restore_root, target_key=tgt.target_key,
                                                      target_state=TargetRestoreState.REPLACED, replacement_completed=True)
                        continue

                    is_base = False
                    base_rec = next((t for t in evidence.targets if t.target_key == tgt.target_key), None)
                    if base_rec is not None:
                        try:
                            _verify_file_owned(tgt.path, expected_size=base_rec.size_bytes, expected_sha256=base_rec.sha256)
                            is_base = True
                        except Exception:
                            pass
                    if not is_base:
                        is_saf = False
                        try:
                            _verify_file_owned(tgt.path, expected_size=sentry.size_bytes, expected_sha256=sentry.sha256)
                            is_saf = True
                        except Exception:
                            pass
                        if not is_saf:
                            raise ConfiguredReplacementManualRecoveryRequiredError(
                                f"REPLACING re-entry: '{tgt.target_key}' matches no known evidence"
                            )
                        continue

                dest_parent = tgt.path.parent.resolve()
                base_rec = next((t for t in evidence.targets if t.target_key == tgt.target_key), None)
                if base_rec is not None:
                    try:
                        _verify_durable_parent(
                            project_root=config.PROJECT_ROOT, current_parent_path=dest_parent,
                            persisted_relative_path=base_rec.parent_relative_path,
                            persisted_st_dev=base_rec.parent_st_dev,
                            persisted_st_ino=base_rec.parent_st_ino,
                            persisted_st_mode=base_rec.parent_st_mode,
                        )
                    except Exception as exc:
                        raise ConfiguredReplacementPreconditionError(
                            f"Destination parent validation failed for '{tgt.target_key}'"
                        ) from exc

                stage_dir = dest_parent / _staged_dir_name(operation_id)
                staged_p = stage_dir / _staged_artifact_name(idx, tgt.target_key)
                _verify_file_owned(staged_p, expected_size=entry.size_bytes, expected_sha256=entry.sha256)

                rbfile = _rollback_artifact_name(idx, tgt.target_key)
                rb_dir = dest_parent / _rollback_dir_name(operation_id, idx)
                _verify_rollback_binding(
                    rb_dir, operation_id=operation_id, safety_backup_id=safety_backup_id,
                    safety_manifest_sha256=saf_snap.manifest_sha256, target_key=tgt.target_key,
                    kind=sentry.kind, index=idx, rollback_filename=rbfile,
                    size_bytes=sentry.size_bytes, sha256=sentry.sha256,
                )

                try:
                    dest_dev = os.lstat(str(dest_parent)).st_dev
                    stage_dev = os.lstat(str(staged_p)).st_dev
                    if dest_dev != stage_dev:
                        raise ConfiguredReplacementPreconditionError(
                            f"Staged artifact on different filesystem for '{tgt.target_key}'"
                        )
                except ConfiguredReplacementPreconditionError:
                    raise
                except OSError as exc:
                    raise ConfiguredReplacementPreconditionError("Filesystem check failed") from exc

                journal = _journal_transition(operation_id, restore_root, target_key=tgt.target_key, replacement_intent=True)
                journal = _handle_configured_sidecars(tgt.path, journal, tgt.target_key, restore_root)

                try:
                    os.replace(str(staged_p), str(tgt.path))
                except OSError as exc:
                    raise ConfiguredReplacementPreconditionError(f"os.replace failed for '{tgt.target_key}'") from exc

                rep_fd = None
                try:
                    rep_fd = _open_nf(tgt.path)
                    rep_st = os.fstat(rep_fd)
                    pst = os.stat(str(tgt.path), follow_symlinks=False)
                    if (pst.st_dev, pst.st_ino) != (rep_st.st_dev, rep_st.st_ino):
                        raise ConfiguredReplacementPreconditionError(f"Replacement path/fd identity mismatch for '{tgt.target_key}'")
                    if not stat.S_ISREG(rep_st.st_mode):
                        raise ConfiguredReplacementPreconditionError(f"Replacement not a regular file for '{tgt.target_key}'")
                    if rep_st.st_size != entry.size_bytes:
                        raise ConfiguredReplacementPreconditionError(f"Replacement size mismatch for '{tgt.target_key}'")
                    if os.name != "nt" and rep_st.st_nlink != 1:
                        raise ConfiguredReplacementPreconditionError(f"Replacement unexpected link count for '{tgt.target_key}'")
                    actual_sha = _sha256_fd(rep_fd, entry.size_bytes)
                    if actual_sha != entry.sha256:
                        raise ConfiguredReplacementPreconditionError(f"Replacement SHA-256 mismatch for '{tgt.target_key}'")
                    if os.name != "nt":
                        os.fchmod(rep_fd, 0o600)
                    os.fsync(rep_fd)
                finally:
                    if rep_fd is not None:
                        try: os.close(rep_fd)
                        except OSError: pass

                chk = inspect_sqlite(tgt.path, deep=True)
                if not chk.readable or not chk.quick_check_ok:
                    raise ConfiguredReplacementPreconditionError(f"quick_check failed for '{tgt.target_key}'")
                if not chk.integrity_check_ok:
                    raise ConfiguredReplacementPreconditionError(f"integrity_check failed for '{tgt.target_key}'")
                if not chk.foreign_keys_ok:
                    raise ConfiguredReplacementPreconditionError(f"foreign_key_check failed for '{tgt.target_key}'")
                fp = schema_fingerprint(tgt.path)
                if fp != entry.schema_fingerprint:
                    raise ConfiguredReplacementPreconditionError(f"Schema fingerprint mismatch for '{tgt.target_key}'")
                markers = migration_markers(tgt.path, entry.kind)
                if markers != {"ledger": entry.migration_ledger, "keys": list(entry.migration_keys), "state": entry.migration_state}:
                    raise ConfiguredReplacementPreconditionError(f"Migration markers mismatch for '{tgt.target_key}'")

                _fsync_path(tgt.path.parent.resolve(), directory=True)
                journal = _journal_transition(operation_id, restore_root, target_key=tgt.target_key,
                                             target_state=TargetRestoreState.REPLACED, replacement_completed=True)

            journal = _journal_transition(operation_id, restore_root, stage=RestoreStage.REPLACED)
            _run_complete_postcheck(
                operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                targets=configured_targets, backup_root=backup_root, restore_root=restore_root,
            )
            journal = _journal_transition(operation_id, restore_root, stage=RestoreStage.POSTCHECK_PASSED)
            journal = _journal_transition(operation_id, restore_root, stage=RestoreStage.COMPLETED)
            try:
                _cleanup_evidence(operation_id, configured_targets)
            except ConfiguredReplacementCleanupError:
                raise
            replaced_keys = tuple(f.target_key for f in journal.targets if f.replacement_completed)
            return ConfiguredReplacementResult(
                operation_id=operation_id, stage=RestoreStage.COMPLETED,
                selected_backup_id=selected_backup_id, safety_backup_id=safety_backup_id,
                runtime_mode=runtime_mode, target_keys=journal.target_keys,
                replaced_target_keys=replaced_keys, rollback_occurred=False,
                configured_database_mutated=True, locks_released=True,
            )

        except ConfiguredReplacementCleanupError:
            raise
        except ConfiguredReplacementManualRecoveryRequiredError:
            _settle_manual(operation_id, restore_root)
            raise
        except ConfiguredReplacementRollbackCompletedError:
            raise
        except Exception as cause:
            try:
                _run_rollback(
                    operation_id=operation_id, selected_snapshot=sel_snap, safety_snapshot=saf_snap,
                    targets=configured_targets, restore_root=restore_root,
                    baseline_evidence=evidence,
                )
            except (ConfiguredReplacementRollbackCompletedError, ConfiguredReplacementCleanupError):
                raise
            except ConfiguredReplacementManualRecoveryRequiredError:
                raise
            except Exception as rb_exc:
                _settle_manual(operation_id, restore_root)
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    "Replacement and rollback both failed"
                ) from rb_exc
            raise ConfiguredReplacementManualRecoveryRequiredError(
                "Rollback succeeded but control flow was unexpected"
            ) from cause

    except (
        ConfiguredReplacementRollbackCompletedError,
        ConfiguredReplacementManualRecoveryRequiredError,
        ConfiguredReplacementCleanupError,
        ConfiguredRestoreLockReleaseError,
    ):
        raise
    except (RestoreLockError, ConfiguredJournalUncertaintyError, ConfiguredRestoreError,
            RestoreJournalError, RestoreJournalPersistenceError):
        raise
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError(
            "Configured restore replacement failed"
        ) from exc
    finally:
        errs = []
        if bkup_lock is not None:
            try: bkup_lock.__exit__(None, None, None)
            except Exception as e: errs.append(e)
            bkup_lock = None
        if rest_lock is not None:
            try: rest_lock.__exit__(None, None, None)
            except Exception as e: errs.append(e)
            rest_lock = None
        if proc_lock is not None:
            try: release_process_lock(proc_lock)
            except Exception as e: errs.append(e)
            proc_lock = None
        if errs:
            raise ConfiguredRestoreLockReleaseError("Failed to release locks cleanly") from errs[0]
