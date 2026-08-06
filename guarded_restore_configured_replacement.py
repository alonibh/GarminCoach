"""Configured-runtime restore replacement, postcheck, rollback, and re-entry (Phase 6B3B2).

Security-hardened implementation with:
1. Keyword-only API: no caller-supplied paths, backup directories, or staging paths.
2. Rediscovers canonical targets from config; loads journal by validated operation ID.
3. Explicit service-stopped proof (injectable for tests) before and after lock acquisition.
4. Acquires ProcessLock -> RestoreLock -> BackupLock non-blockingly; holds all throughout.
5. Race-complete file ownership: pre-hash and post-hash fstat+pathname re-stat; no unsafe window.
6. Descriptor-bound permissions only: fchmod via owned fd; no pathname chmod after publication.
7. Per-target immediate baseline revalidation before each replacement_intent persistence.
8. Stages verified rollback artifacts with exact 0700 directory, 0600 files, nlink=1.
9. Replacement order: data/tenant targets first, control database last.
10. Atomic os.replace; durable replacement_intent before and replacement_completed after.
11. Named WAL/SHM sidecars handled with baseline-identity proof on re-entry.
12. Complete rollback verification: fchmod, integrity_check, foreign_key_check, schema, markers.
13. Pre-FAILED_SAFE safety state verification for complete rollback.
14. ROLLED_BACK re-entry: full state verification before FAILED_SAFE.
15. COMPLETED re-entry: full postcheck before idempotent result.
16. Verified settlement: raises ConfiguredJournalUncertaintyError if persistence uncertain.
17. Lock-release outcome precedence: ManualRecovery and RollbackCompleted preserved.
18. Strict descriptor-bound evidence cleanup: no pathname read_bytes for ownership proof.
19. Post-mutation snapshot validation without current-config DB comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

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
    DestinationBaselineRecord,
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
    """Open with no-follow; verify regular file; return fd.

    The descriptor is closed at most once.  On non-regular-file detection the
    descriptor is closed exactly once (tracked via _fd_closed) before raising;
    the except handler only closes if the earlier close did not happen.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError("Could not open file no-follow") from exc
    _fd_closed = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            try:
                os.close(fd)
            except OSError:
                pass
            _fd_closed = True
            raise ConfiguredReplacementPreconditionError("Opened descriptor is not a regular file")
    except ConfiguredReplacementPreconditionError:
        if not _fd_closed:
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


def _verify_file_owned(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    require_mode_0600: bool = False,
    require_single_link: bool = False,
) -> tuple:
    """Race-complete file ownership verification.

    Records all seven mutable facts (device, inode, file type, size, mode,
    link count, mtime_ns) from BOTH fstat AND no-follow pathname stat BEFORE
    hashing.  Requires descriptor/pathname agreement on all seven facts before
    reading.  Hashes through the descriptor.  Re-runs both fstat and no-follow
    pathname stat AFTER hashing and requires all seven facts to be unchanged.
    Mode-0600 and single-link requirements are enforced before AND after hash.
    Surfaces descriptor-close failure as a bounded ownership error.

    Returns (dev, ino, mode, nlink, mtime_ns).
    """
    fd = _open_nf(path)
    try:
        pre_fst = os.fstat(fd)
        pre_pst = os.stat(str(path), follow_symlinks=False)

        # Both must be regular files.
        if not stat.S_ISREG(pre_fst.st_mode):
            raise ConfiguredReplacementPreconditionError("Descriptor is not a regular file")
        if not stat.S_ISREG(pre_pst.st_mode):
            raise ConfiguredReplacementPreconditionError("Pathname is not a regular file before hash")

        pre_fst_mtime = getattr(pre_fst, "st_mtime_ns", None)
        pre_pst_mtime = getattr(pre_pst, "st_mtime_ns", None)

        # Descriptor and pathname must agree on all seven facts before hashing.
        if (pre_fst.st_dev, pre_fst.st_ino, pre_fst.st_size, pre_fst.st_mode,
                pre_fst.st_nlink, pre_fst_mtime) != (
            pre_pst.st_dev, pre_pst.st_ino, pre_pst.st_size, pre_pst.st_mode,
                pre_pst.st_nlink, pre_pst_mtime):
            raise ConfiguredReplacementPreconditionError(
                "Descriptor/pathname disagreement on file facts before hash"
            )

        if pre_fst.st_size != expected_size:
            raise ConfiguredReplacementPreconditionError("File size mismatch before hash")
        if require_mode_0600 and os.name != "nt":
            if stat.S_IMODE(pre_fst.st_mode) != 0o600:
                raise ConfiguredReplacementPreconditionError("File mode not 0600 before hash")
        if require_single_link and os.name != "nt":
            if pre_fst.st_nlink != 1:
                raise ConfiguredReplacementPreconditionError("File link count not 1 before hash")

        # Hash through descriptor; size mismatch raises from _sha256_fd.
        actual_sha = _sha256_fd(fd, expected_size)
        if actual_sha != expected_sha256:
            raise ConfiguredReplacementPreconditionError("File SHA-256 mismatch")

        # Re-run both fstat and no-follow pathname stat after hashing.
        post_fst = os.fstat(fd)
        post_pst = os.stat(str(path), follow_symlinks=False)
        post_fst_mtime = getattr(post_fst, "st_mtime_ns", None)
        post_pst_mtime = getattr(post_pst, "st_mtime_ns", None)

        # All seven facts must be unchanged in fstat.
        if (post_fst.st_dev, post_fst.st_ino, post_fst.st_size, post_fst.st_mode,
                post_fst.st_nlink, post_fst_mtime) != (
            pre_fst.st_dev, pre_fst.st_ino, pre_fst.st_size, pre_fst.st_mode,
                pre_fst.st_nlink, pre_fst_mtime):
            raise ConfiguredReplacementPreconditionError(
                "File facts changed during hash (fstat)"
            )

        # All seven facts must be unchanged in pathname stat; inode must still name descriptor.
        if (post_pst.st_dev, post_pst.st_ino, post_pst.st_size, post_pst.st_mode,
                post_pst.st_nlink, post_pst_mtime) != (
            pre_pst.st_dev, pre_pst.st_ino, pre_pst.st_size, pre_pst.st_mode,
                pre_pst.st_nlink, pre_pst_mtime):
            raise ConfiguredReplacementPreconditionError(
                "File facts changed during hash (pathname)"
            )
        if (post_pst.st_dev, post_pst.st_ino) != (pre_fst.st_dev, pre_fst.st_ino):
            raise ConfiguredReplacementPreconditionError(
                "Pathname no longer names the descriptor's inode after hash"
            )

        # Mode and link-count requirements must hold after hashing.
        if require_mode_0600 and os.name != "nt":
            if stat.S_IMODE(post_fst.st_mode) != 0o600:
                raise ConfiguredReplacementPreconditionError("File mode not 0600 after hash")
        if require_single_link and os.name != "nt":
            if post_fst.st_nlink != 1:
                raise ConfiguredReplacementPreconditionError("File link count not 1 after hash")

        result = (pre_fst.st_dev, pre_fst.st_ino, stat.S_IMODE(pre_fst.st_mode), pre_fst.st_nlink, pre_fst_mtime)
    except BaseException as _orig_exc:
        _close_exc: OSError | None = None
        try:
            os.close(fd)
        except OSError as _ce:
            _close_exc = _ce
        if _close_exc is not None:
            # Both verification failed and close failed: surface close uncertainty.
            # close exception is the direct cause; original verification failure is context.
            raise ConfiguredReplacementPreconditionError(
                "Descriptor ownership uncertain: verification failed and descriptor close failed"
            ) from _close_exc
        raise

    # Verification succeeded; surface close failure as a bounded ownership error.
    try:
        os.close(fd)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Descriptor close failed during ownership verification"
        ) from exc
    return result


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
    """Write rollback binding with descriptor-bound permissions. No pathname chmod."""
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
        fd = os.open(
            str(partial_p),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG,
            0o600,
        )
        offset = 0
        while offset < len(data):
            n = os.write(fd, data[offset:])
            if n <= 0:
                raise OSError("write failed")
            offset += n
        # Descriptor-bound permission finalization: fchmod before fsync and close.
        # No pathname chmod is ever applied.
        if os.name != "nt":
            os.fchmod(fd, 0o600)
            pst = os.fstat(fd)
            if stat.S_IMODE(pst.st_mode) != 0o600:
                raise ConfiguredReplacementPreconditionError(
                    "Rollback binding fchmod did not take effect"
                )
        if os.name != "nt":
            os.fsync(fd)
        if os.fstat(fd).st_size != len(data):
            raise ConfiguredReplacementPreconditionError("Rollback binding write size mismatch")
        os.close(fd)
        fd = None
    except (OSError, ConfiguredReplacementPreconditionError):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
        try:
            partial_p.unlink()
        except OSError:
            pass
        raise
    try:
        os.lstat(str(final_p))
        try:
            partial_p.unlink()
        except OSError:
            pass
        raise ConfiguredReplacementPreconditionError(
            "Rollback binding destination appeared before replace"
        )
    except FileNotFoundError:
        pass
    try:
        os.replace(str(partial_p), str(final_p))
    except OSError as exc:
        try:
            partial_p.unlink()
        except OSError:
            pass
        raise ConfiguredReplacementPreconditionError("Rollback binding replace failed") from exc
    # No pathname chmod. Verify mode and content via descriptor after publication.
    vfd = None
    try:
        vfd = os.open(str(final_p), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        fst = os.fstat(vfd)
        pst = os.lstat(str(final_p))
        if (fst.st_dev, fst.st_ino, fst.st_size) != (pst.st_dev, pst.st_ino, pst.st_size):
            raise ConfiguredReplacementPreconditionError(
                "Rollback binding path/fd identity mismatch"
            )
        if not stat.S_ISREG(fst.st_mode):
            raise ConfiguredReplacementPreconditionError("Rollback binding is not a regular file")
        if os.name != "nt" and stat.S_IMODE(fst.st_mode) != 0o600:
            raise ConfiguredReplacementPreconditionError(
                f"Rollback binding mode {oct(stat.S_IMODE(fst.st_mode))} != 0600"
            )
        rd = b""
        while True:
            chunk = os.read(vfd, 65536)
            if not chunk:
                break
            rd += chunk
        if rd != data:
            raise ConfiguredReplacementPreconditionError(
                "Rollback binding verification data mismatch"
            )
        # Post-read re-stat
        post_fst = os.fstat(vfd)
        post_pst = os.lstat(str(final_p))
        if (post_fst.st_dev, post_fst.st_ino, post_fst.st_size) != (
            fst.st_dev, fst.st_ino, fst.st_size
        ):
            raise ConfiguredReplacementPreconditionError(
                "Rollback binding changed during verification read"
            )
        if (post_pst.st_dev, post_pst.st_ino) != (fst.st_dev, fst.st_ino):
            raise ConfiguredReplacementPreconditionError(
                "Rollback binding path identity changed during verification"
            )
        if os.name != "nt":
            os.fsync(vfd)
    finally:
        if vfd is not None:
            try:
                os.close(vfd)
            except OSError:
                pass
    _fsync_path(rollback_dir, directory=True)


def _verify_rollback_binding(
    rollback_dir: Path, *, operation_id: str, safety_backup_id: str,
    safety_manifest_sha256: str, target_key: str, kind: str, index: int,
    rollback_filename: str, size_bytes: int, sha256: str,
    require_deep_sqlite: bool = False,
    schema_fp: str | None = None,
    migration_ledger: str | None = None,
    migration_keys: tuple | None = None,
    migration_state: str | None = None,
    kind_for_markers: str | None = None,
) -> Path:
    """Verify rollback directory, binding and artifact with race-complete ownership checks."""
    # 1. Directory identity and mode
    if rollback_dir.is_symlink() or has_symlink_component(rollback_dir):
        raise ConfiguredReplacementPreconditionError(
            "Rollback directory path contains symlinks"
        )
    try:
        d_st = os.stat(str(rollback_dir), follow_symlinks=False)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Rollback directory not accessible"
        ) from exc
    if not stat.S_ISDIR(d_st.st_mode):
        raise ConfiguredReplacementPreconditionError("Rollback directory is not a directory")
    if os.name != "nt" and stat.S_IMODE(d_st.st_mode) != 0o700:
        raise ConfiguredReplacementPreconditionError(
            f"Rollback directory mode {oct(stat.S_IMODE(d_st.st_mode))} != 0700"
        )
    dir_id = (d_st.st_dev, d_st.st_ino)
    dir_mtime = getattr(d_st, "st_mtime_ns", None)
    dir_nlink = d_st.st_nlink

    # 2. First child-set enumeration
    binding_p = rollback_dir / _ROLLBACK_BINDING_NAME
    if binding_p.is_symlink() or has_symlink_component(binding_p):
        raise ConfiguredReplacementPreconditionError(
            "Rollback binding path contains symlinks"
        )
    expected = _rollback_binding_bytes(
        operation_id, safety_backup_id, safety_manifest_sha256,
        target_key, kind, index, rollback_filename, size_bytes, sha256,
    )
    try:
        first_children = {c.name for c in rollback_dir.iterdir()}
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Could not enumerate rollback directory"
        ) from exc
    expected_children = {_ROLLBACK_BINDING_NAME, rollback_filename}
    if first_children != expected_children:
        raise ConfiguredReplacementPreconditionError(
            f"Rollback directory children mismatch: got {first_children}, expected {expected_children}"
        )

    # 3. Binding: open no-follow, verify identity, mode, nlink, content, post-read re-stat
    fd = None
    try:
        fd = os.open(str(binding_p), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        bst = os.fstat(fd)
        pst = os.lstat(str(binding_p))
        if (bst.st_dev, bst.st_ino, bst.st_size) != (pst.st_dev, pst.st_ino, pst.st_size):
            raise ConfiguredReplacementPreconditionError("Rollback binding identity mismatch")
        if not stat.S_ISREG(bst.st_mode) or bst.st_size > _MAX_BINDING_BYTES:
            raise ConfiguredReplacementPreconditionError("Rollback binding invalid")
        if os.name != "nt" and stat.S_IMODE(bst.st_mode) != 0o600:
            raise ConfiguredReplacementPreconditionError(
                f"Rollback binding mode {oct(stat.S_IMODE(bst.st_mode))} != 0600"
            )
        if os.name != "nt" and bst.st_nlink != 1:
            raise ConfiguredReplacementPreconditionError(
                f"Rollback binding link count {bst.st_nlink} != 1"
            )
        rd = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            rd += chunk
        # Post-read re-stat
        post_bst = os.fstat(fd)
        post_pst = os.lstat(str(binding_p))
        if (post_bst.st_dev, post_bst.st_ino, post_bst.st_size) != (
            bst.st_dev, bst.st_ino, bst.st_size
        ):
            raise ConfiguredReplacementPreconditionError(
                "Rollback binding changed during read"
            )
        if (post_pst.st_dev, post_pst.st_ino) != (bst.st_dev, bst.st_ino):
            raise ConfiguredReplacementPreconditionError(
                "Rollback binding path identity mismatch after read"
            )
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if rd != expected:
        raise ConfiguredReplacementPreconditionError("Rollback binding bytes mismatch")

    # 4. Artifact: verify ownership with mode 0600 and nlink=1
    artifact_p = rollback_dir / rollback_filename
    if has_symlink_component(artifact_p) or artifact_p.is_symlink():
        raise ConfiguredReplacementPreconditionError(
            "Rollback artifact path contains symlinks"
        )
    try:
        rdir_dev = os.lstat(str(rollback_dir)).st_dev
        art_dev = os.lstat(str(artifact_p)).st_dev
        if rdir_dev != art_dev:
            raise ConfiguredReplacementPreconditionError(
                "Rollback artifact on different filesystem"
            )
    except ConfiguredReplacementPreconditionError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Rollback artifact filesystem check failed"
        ) from exc
    _verify_file_owned(
        artifact_p,
        expected_size=size_bytes,
        expected_sha256=sha256,
        require_mode_0600=True,
        require_single_link=True,
    )

    # 5. Deep SQLite verification (optional, at key decision points)
    if require_deep_sqlite:
        chk = inspect_sqlite(artifact_p, deep=True)
        if not chk.readable or not chk.quick_check_ok:
            raise ConfiguredReplacementPreconditionError(
                "Rollback artifact quick_check failed"
            )
        if not chk.integrity_check_ok:
            raise ConfiguredReplacementPreconditionError(
                "Rollback artifact integrity_check failed"
            )
        if not chk.foreign_keys_ok:
            raise ConfiguredReplacementPreconditionError(
                "Rollback artifact foreign_key_check failed"
            )
        if schema_fp is not None:
            actual_fp = schema_fingerprint(artifact_p)
            if actual_fp != schema_fp:
                raise ConfiguredReplacementPreconditionError(
                    "Rollback artifact schema fingerprint mismatch"
                )
        if migration_ledger is not None and kind_for_markers is not None:
            expected_markers = {
                "ledger": migration_ledger,
                "keys": list(migration_keys or []),
                "state": migration_state or "",
            }
            actual_markers = migration_markers(artifact_p, kind_for_markers)
            if actual_markers != expected_markers:
                raise ConfiguredReplacementPreconditionError(
                    "Rollback artifact migration markers mismatch"
                )

    # 6. Second child-set enumeration: reject any changes since first enumeration
    try:
        second_children = {c.name for c in rollback_dir.iterdir()}
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Could not re-enumerate rollback directory"
        ) from exc
    if second_children != expected_children:
        raise ConfiguredReplacementPreconditionError(
            f"Rollback directory children changed during verification: {second_children}"
        )

    # 7. Directory identity must be unchanged
    try:
        d_st2 = os.stat(str(rollback_dir), follow_symlinks=False)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Rollback directory re-stat failed"
        ) from exc
    if (d_st2.st_dev, d_st2.st_ino) != dir_id:
        raise ConfiguredReplacementPreconditionError(
            "Rollback directory identity changed during verification"
        )

    return artifact_p


def _copy_rollback_file(
    source: Path, dest_dir: Path, filename: str, *, size: int, sha256: str
) -> None:
    """Copy safety backup file to rollback dir with descriptor-bound 0600 permissions."""
    dest = dest_dir / filename
    partial = dest_dir / ("." + filename + ".partial")
    if dest.exists() or dest.is_symlink():
        raise ConfiguredReplacementPreconditionError(
            "Rollback artifact destination already exists"
        )
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
        out_fd = os.open(
            str(partial),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG,
            0o600,
        )
        h = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(src_fd, 1 << 20)
            if not chunk:
                break
            h.update(chunk)
            copied += len(chunk)
            off = 0
            while off < len(chunk):
                n = os.write(out_fd, chunk[off:])
                if n <= 0:
                    raise OSError("write failed")
                off += n
        # Descriptor-bound permission finalization before fsync. No pathname chmod.
        if os.name != "nt":
            os.fchmod(out_fd, 0o600)
            pst = os.fstat(out_fd)
            if stat.S_IMODE(pst.st_mode) != 0o600:
                raise ConfiguredReplacementPreconditionError(
                    "Rollback artifact fchmod did not take effect"
                )
        if os.name != "nt":
            os.fsync(out_fd)
        ast = os.fstat(src_fd)
        if (
            (bst.st_dev, bst.st_ino, bst.st_size) != (ast.st_dev, ast.st_ino, ast.st_size)
            or copied != size
            or h.hexdigest() != sha256
        ):
            raise ConfiguredReplacementPreconditionError(
                "Rollback source changed during copy"
            )
        os.close(src_fd)
        src_fd = None
        os.close(out_fd)
        out_fd = None
        if dest.exists() or dest.is_symlink():
            raise ConfiguredReplacementPreconditionError(
                "Rollback artifact destination appeared during copy"
            )
        os.replace(str(partial), str(dest))
        # No pathname chmod. Verify via race-complete descriptor-based check.
        _verify_file_owned(
            dest,
            expected_size=size,
            expected_sha256=sha256,
            require_mode_0600=True,
            require_single_link=True,
        )
        _fsync_path(dest)
        _fsync_path(dest_dir, directory=True)
    except (OSError, ConfiguredReplacementPreconditionError):
        for fd in (src_fd, out_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    finally:
        for fd in (src_fd, out_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


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
            raise ConfiguredReplacementPreconditionError(
                "Rollback directory path contains symlinks"
            )
        rd_st = os.stat(str(rb_dir), follow_symlinks=False)
        if not stat.S_ISDIR(rd_st.st_mode):
            raise ConfiguredReplacementPreconditionError(
                "Rollback directory is not a directory"
            )
        if os.name != "nt" and stat.S_IMODE(rd_st.st_mode) != 0o700:
            raise ConfiguredReplacementPreconditionError(
                "Rollback directory permissions invalid"
            )
        expected_ch = {_ROLLBACK_BINDING_NAME, rbfile}
        try:
            actual_ch = {c.name for c in rb_dir.iterdir()}
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError(
                "Could not list rollback directory"
            ) from exc
        if actual_ch != expected_ch:
            extra = actual_ch - expected_ch
            if extra:
                raise ConfiguredReplacementPreconditionError(
                    f"Rollback directory foreign children: {extra}"
                )
            raise ConfiguredReplacementPreconditionError(
                "Rollback directory children mismatch"
            )
        return _verify_rollback_binding(
            rb_dir,
            operation_id=operation_id,
            safety_backup_id=safety_snapshot.backup_id,
            safety_manifest_sha256=safety_snapshot.manifest_sha256,
            target_key=tkey,
            kind=entry.kind,
            index=index,
            rollback_filename=rbfile,
            size_bytes=entry.size_bytes,
            sha256=entry.sha256,
            require_deep_sqlite=True,
            schema_fp=entry.schema_fingerprint,
            migration_ledger=entry.migration_ledger,
            migration_keys=entry.migration_keys,
            migration_state=entry.migration_state,
            kind_for_markers=entry.kind,
        )

    try:
        dest_parent_dev = os.lstat(str(dest_parent)).st_dev
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Could not stat destination parent"
        ) from exc

    src_file = safety_snapshot.directory / entry.filename
    if has_symlink_component(src_file) or src_file.is_symlink() or not src_file.exists():
        raise ConfiguredReplacementPreconditionError(
            "Safety backup source file missing or unsafe"
        )

    try:
        rb_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Could not create rollback directory"
        ) from exc
    # No pathname chmod on directory – it was created with mode 0700.
    try:
        rb_dev = os.lstat(str(rb_dir)).st_dev
        if rb_dev != dest_parent_dev:
            raise ConfiguredReplacementPreconditionError(
                "Rollback directory on different filesystem"
            )
    except ConfiguredReplacementPreconditionError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Rollback dir filesystem check failed"
        ) from exc

    _write_rollback_binding(
        rb_dir,
        operation_id=operation_id,
        safety_backup_id=safety_snapshot.backup_id,
        safety_manifest_sha256=safety_snapshot.manifest_sha256,
        target_key=tkey,
        kind=entry.kind,
        index=index,
        rollback_filename=rbfile,
        size_bytes=entry.size_bytes,
        sha256=entry.sha256,
    )
    _copy_rollback_file(src_file, rb_dir, rbfile, size=entry.size_bytes, sha256=entry.sha256)
    return _verify_rollback_binding(
        rb_dir,
        operation_id=operation_id,
        safety_backup_id=safety_snapshot.backup_id,
        safety_manifest_sha256=safety_snapshot.manifest_sha256,
        target_key=tkey,
        kind=entry.kind,
        index=index,
        rollback_filename=rbfile,
        size_bytes=entry.size_bytes,
        sha256=entry.sha256,
        require_deep_sqlite=True,
        schema_fp=entry.schema_fingerprint,
        migration_ledger=entry.migration_ledger,
        migration_keys=entry.migration_keys,
        migration_state=entry.migration_state,
        kind_for_markers=entry.kind,
    )


# ---------------------------------------------------------------------------
# Service-stopped proof
# ---------------------------------------------------------------------------

def _require_service_stopped(checker=None) -> None:
    """Require a positive, bounded proof that the application service is stopped.

    The checker must be a callable that raises on any service-running or
    uncertain outcome.  In production the default is require_service_stopped
    from database_reset.  In tests the caller should inject a no-op or a
    controlled stub.
    """
    if checker is not None:
        try:
            checker()
        except ConfiguredReplacementPreconditionError:
            raise
        except Exception as exc:
            raise ConfiguredReplacementPreconditionError(
                "Service-stopped proof failed"
            ) from exc
        return

    # Production default: use the already-reviewed local service-state mechanism.
    try:
        from database_reset import DatabaseResetError, require_service_stopped
    except ImportError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Service-stopped proof mechanism unavailable"
        ) from exc
    try:
        require_service_stopped()
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError(
            "Service-stopped proof failed"
        ) from exc


# ---------------------------------------------------------------------------
# Sidecar handling
# ---------------------------------------------------------------------------

def _handle_configured_sidecars(
    destination: Path,
    journal: RestoreJournal,
    target_key: str,
    restore_root: Path,
    baseline_evidence: DestinationBaselineEvidence | None = None,
) -> RestoreJournal:
    """Remove named WAL/SHM sidecars with durable journal recording.

    On re-entry when presence was recorded but removal was not, verifies
    sidecar identity against baseline evidence before unlinking.  A sidecar
    that cannot be proven to be the same object transitions to manual recovery.
    """
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
            # Re-entry: presence recorded but removal not yet recorded.
            # Verify current sidecar identity against baseline before unlinking.
            base_rec = None
            if baseline_evidence is not None:
                base_rec = next(
                    (t for t in baseline_evidence.targets if t.target_key == target_key),
                    None,
                )
            try:
                sc_st = os.lstat(str(sidecar))
            except FileNotFoundError:
                # Sidecar is already gone; record removal.
                cur = _journal_transition(
                    op_id, restore_root, target_key=target_key, **{rem_attr: True}
                )
                continue
            # Sidecar still exists; verify type.
            if stat.S_ISDIR(sc_st.st_mode):
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} is a directory"
                )
            if stat.S_ISLNK(sc_st.st_mode):
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} is a symlink"
                )
            if not stat.S_ISREG(sc_st.st_mode):
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} is not a regular file"
                )
            # Verify identity against baseline evidence.
            if base_rec is not None and os.name != "nt":
                sc_dict = base_rec.wal if is_wal else base_rec.shm
                bl_dev = sc_dict.get("st_dev")
                bl_ino = sc_dict.get("st_ino")
                bl_size = sc_dict.get("size_bytes")
                bl_mtime = sc_dict.get("mtime_ns")
                if bl_dev is None or bl_ino is None:
                    raise ConfiguredReplacementPreconditionError(
                        f"Sidecar {suffix} identity cannot be proven from baseline evidence"
                    )
                curr_id = (
                    sc_st.st_dev, sc_st.st_ino, sc_st.st_size,
                    getattr(sc_st, "st_mtime_ns", None),
                )
                baseline_id = (bl_dev, bl_ino, bl_size, bl_mtime)
                if curr_id != baseline_id:
                    raise ConfiguredReplacementPreconditionError(
                        f"Sidecar {suffix} identity does not match baseline evidence"
                    )
            elif os.name != "nt":
                raise ConfiguredReplacementPreconditionError(
                    f"No baseline evidence for sidecar {suffix} re-entry identity proof"
                )
            # Identity proven; unlink.
            try:
                os.unlink(str(sidecar))
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} removal failed"
                ) from exc
            _fsync_path(destination.parent, directory=True)
            cur = _journal_transition(
                op_id, restore_root, target_key=target_key, **{rem_attr: True}
            )
            continue

        # First handling: sidecar not yet recorded.
        try:
            sc_st = os.lstat(str(sidecar))
        except FileNotFoundError:
            cur = _journal_transition(
                op_id, restore_root, target_key=target_key,
                **{pres_attr: False, rem_attr: False},
            )
            continue

        if stat.S_ISDIR(sc_st.st_mode):
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is a directory")
        if stat.S_ISLNK(sc_st.st_mode):
            raise ConfiguredReplacementPreconditionError(f"Sidecar {suffix} is a symlink")
        if not stat.S_ISREG(sc_st.st_mode):
            raise ConfiguredReplacementPreconditionError(
                f"Sidecar {suffix} is not a regular file"
            )

        before_id = (
            sc_st.st_dev, sc_st.st_ino, sc_st.st_size,
            getattr(sc_st, "st_mtime_ns", None),
        )
        cur = _journal_transition(
            op_id, restore_root, target_key=target_key, **{pres_attr: True}
        )

        try:
            after_st = os.lstat(str(sidecar))
            after_id = (
                after_st.st_dev, after_st.st_ino, after_st.st_size,
                getattr(after_st, "st_mtime_ns", None),
            )
            if before_id != after_id:
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} changed after presence recording"
                )
        except FileNotFoundError:
            pass
        except ConfiguredReplacementPreconditionError:
            raise
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError(
                f"Sidecar {suffix} re-stat failed"
            ) from exc

        try:
            os.unlink(str(sidecar))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError(
                f"Sidecar {suffix} removal failed"
            ) from exc

        _fsync_path(destination.parent, directory=True)
        cur = _journal_transition(
            op_id, restore_root, target_key=target_key, **{rem_attr: True}
        )

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
    """Update journal, reread, and verify exact match. Raises ConfiguredJournalUncertaintyError."""
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
# Verified settlement helpers
# ---------------------------------------------------------------------------

def _settle_safe(operation_id: str, restore_root: Path, journal: RestoreJournal) -> None:
    """Settle to FAILED_SAFE.  Raises ConfiguredJournalUncertaintyError if uncertain."""
    if journal.stage in {
        RestoreStage.COMPLETED,
        RestoreStage.FAILED_SAFE,
        RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
    }:
        return
    if RestoreStage.FAILED_SAFE not in _GLOBAL_TRANSITIONS.get(journal.stage, set()):
        return
    _journal_transition(operation_id, restore_root, stage=RestoreStage.FAILED_SAFE)


def _settle_manual(operation_id: str, restore_root: Path) -> None:
    """Settle to FAILED_MANUAL_RECOVERY_REQUIRED via legal transitions.

    Raises ConfiguredJournalUncertaintyError if persistence cannot be verified.
    Never rewrites FAILED_SAFE or COMPLETED to FAILED_MANUAL_RECOVERY_REQUIRED.
    """
    try:
        j = load_restore_journal(operation_id, root=restore_root)
    except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
        raise ConfiguredJournalUncertaintyError(
            "Cannot load journal during manual settlement"
        ) from exc

    if j.stage in {
        RestoreStage.COMPLETED,
        RestoreStage.FAILED_SAFE,
        RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
    }:
        return

    # If ROLLBACK_REQUIRED → FAILED_MANUAL_RECOVERY_REQUIRED is legal, do it directly.
    if j.stage is RestoreStage.ROLLBACK_REQUIRED:
        _journal_transition(
            operation_id, restore_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED
        )
        return

    # For other stages: attempt ROLLBACK_REQUIRED first then FAILED_MANUAL_RECOVERY_REQUIRED.
    if RestoreStage.ROLLBACK_REQUIRED in _GLOBAL_TRANSITIONS.get(j.stage, set()):
        try:
            _journal_transition(
                operation_id, restore_root, stage=RestoreStage.ROLLBACK_REQUIRED
            )
        except ConfiguredJournalUncertaintyError:
            raise
        try:
            _journal_transition(
                operation_id, restore_root,
                stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED,
            )
        except ConfiguredJournalUncertaintyError:
            raise
        return

    # If direct FAILED_MANUAL_RECOVERY_REQUIRED transition is available.
    if RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED in _GLOBAL_TRANSITIONS.get(
        j.stage, set()
    ):
        _journal_transition(
            operation_id, restore_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED
        )
        return

    # No legal path to FAILED_MANUAL_RECOVERY_REQUIRED from current stage.
    raise ConfiguredJournalUncertaintyError(
        f"No legal transition from {j.stage} to FAILED_MANUAL_RECOVERY_REQUIRED"
    )


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
            raise ConfiguredReplacementPreconditionError(
                "garminconnect package version missing"
            )
    except ConfiguredReplacementPreconditionError:
        raise
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError(
            "garminconnect package verification failed"
        ) from exc

    if selected_snapshot.backup_id != selected_backup_id:
        raise ConfiguredReplacementPreconditionError("Selected backup ID mismatch")
    sel_dir = backup_root / f"backup-{selected_backup_id}"
    try:
        rsel = load_validated_backup_snapshot(sel_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Selected backup revalidation failed"
        ) from exc
    if rsel.manifest_sha256 != selected_snapshot.manifest_sha256:
        raise ConfiguredReplacementPreconditionError("Selected backup manifest SHA-256 drift")
    if rsel.manifest_sha256 != journal.selected_backup_manifest_sha256:
        raise ConfiguredReplacementPreconditionError(
            "Reloaded selected backup manifest SHA-256 does not match journal"
        )

    if safety_backup_id == selected_backup_id:
        raise ConfiguredReplacementPreconditionError(
            "Safety backup ID matches selected backup ID"
        )
    saf_dir = backup_root / f"backup-{safety_backup_id}"
    try:
        rsaf = load_validated_backup_snapshot(saf_dir, against_current_config=True)
    except BackupError as exc:
        raise ConfiguredReplacementPreconditionError(
            "Safety backup revalidation failed"
        ) from exc
    if rsaf.manifest_sha256 != safety_snapshot.manifest_sha256:
        raise ConfiguredReplacementPreconditionError("Safety backup manifest SHA-256 drift")
    if journal.safety_backup_manifest_sha256 is not None and rsaf.manifest_sha256 != journal.safety_backup_manifest_sha256:
        raise ConfiguredReplacementPreconditionError(
            "Reloaded safety backup manifest SHA-256 does not match journal"
        )

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
    rc = confirmation_value(
        target_hash=rh, expected_application_commit=expected_application_commit
    )
    if confirmed_target_set_hash != rh or confirmed_restore_value != rc:
        raise ConfiguredReplacementPreconditionError(
            "Target-set hash or confirmation value drift"
        )

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
        raise ConfiguredReplacementPreconditionError(
            "Journal missing destination baseline SHA-256"
        )

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
        raise ConfiguredReplacementPreconditionError(
            "Journal stage must be REPLACEMENT_READY at pre-mutation barrier"
        )
    for fact in journal.targets:
        if fact.state is not TargetRestoreState.STAGED_VERIFIED:
            raise ConfiguredReplacementPreconditionError(
                "Not all targets STAGED_VERIFIED at pre-mutation barrier"
            )
        if any([
            fact.wal_removed, fact.shm_removed, fact.replacement_intent,
            fact.replacement_completed, fact.rollback_intent, fact.rollback_completed,
        ]):
            raise ConfiguredReplacementPreconditionError(
                "Mutation flags present at pre-mutation barrier"
            )

    sel_entries = {e.target_key: e for e in selected_snapshot.entries}
    for idx, tgt in enumerate(targets):
        entry = sel_entries[tgt.target_key]
        stage_dir = tgt.path.parent.resolve() / _staged_dir_name(operation_id)
        staged_p = stage_dir / _staged_artifact_name(idx, tgt.target_key)
        if not staged_p.exists() or staged_p.is_symlink():
            raise ConfiguredReplacementPreconditionError(
                f"Staged artifact missing for '{tgt.target_key}'"
            )
        _verify_file_owned(
            staged_p,
            expected_size=entry.size_bytes,
            expected_sha256=entry.sha256,
            require_mode_0600=True,
            require_single_link=True,
        )
        try:
            dest_dev = os.lstat(str(tgt.path.parent.resolve())).st_dev
            stage_dev = os.lstat(str(staged_p)).st_dev
            if dest_dev != stage_dev:
                raise ConfiguredReplacementPreconditionError(
                    f"Staged artifact on different filesystem for '{tgt.target_key}'"
                )
        except ConfiguredReplacementPreconditionError:
            raise
        except OSError as exc:
            raise ConfiguredReplacementPreconditionError("Filesystem check failed") from exc


# ---------------------------------------------------------------------------
# Post-mutation backup snapshot validation
# ---------------------------------------------------------------------------

def _validate_snapshot_post_mutation(
    snap: ValidatedBackupSnapshot,
    *,
    expected_backup_id: str,
    expected_manifest_sha256: str,
    runtime_mode: str,
    expected_target_keys: tuple,
    backup_root: Path,
) -> None:
    """Validate backup snapshot without comparing against current (transitional) databases.

    Verifies backup ID, manifest SHA, runtime mode, target keys, and every backup
    file size and SHA-256 via descriptor-based race-complete check.
    """
    if snap.backup_id != expected_backup_id:
        raise ConfiguredReplacementPreconditionError("Backup ID mismatch in snapshot")
    if snap.manifest_sha256 != expected_manifest_sha256:
        raise ConfiguredReplacementPreconditionError("Backup manifest SHA-256 mismatch")
    if snap.runtime_mode != runtime_mode:
        raise ConfiguredReplacementPreconditionError("Backup runtime mode mismatch in snapshot")
    if snap.target_keys != expected_target_keys:
        raise ConfiguredReplacementPreconditionError("Backup target keys mismatch in snapshot")
    # Verify each backup file via descriptor-based race-complete ownership check.
    for entry in snap.entries:
        src = snap.directory / entry.filename
        if has_symlink_component(src) or src.is_symlink():
            raise ConfiguredReplacementPreconditionError(
                f"Backup file path unsafe for '{entry.target_key}'"
            )
        _verify_file_owned(src, expected_size=entry.size_bytes, expected_sha256=entry.sha256)


# ---------------------------------------------------------------------------
# Per-target baseline revalidation (immediate, before replacement_intent)
# ---------------------------------------------------------------------------

def _revalidate_target_pre_intent(
    *,
    tgt: DatabaseTarget,
    idx: int,
    operation_id: str,
    restore_root: Path,
    evidence: DestinationBaselineEvidence,
    sel_entry,
    saf_entry,
    safety_backup_id: str,
    saf_snap: ValidatedBackupSnapshot,
    staged_p: Path,
    rb_dir: Path,
    rbfile: str,
    dest_parent: Path,
) -> None:
    """Immediate per-target verification immediately before replacement_intent persistence.

    1. Reload and verify journal (no conflicting flags).
    2. Revalidate durable parent.
    3. Revalidate destination against persisted baseline evidence.
    4. Revalidate named sidecar baseline state.
    5. Revalidate selected staged artifact.
    6. Revalidate rollback artifact.
    7. Revalidate both same-filesystem relationships.
    """
    # 1. Reload journal and verify no conflicting flags for this target.
    try:
        j = load_restore_journal(operation_id, root=restore_root)
    except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
        raise ConfiguredReplacementPreconditionError(
            f"Journal reload failed for '{tgt.target_key}'"
        ) from exc
    fact = next((f for f in j.targets if f.target_key == tgt.target_key), None)
    if fact is None:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            f"No journal fact for '{tgt.target_key}' at pre-intent check"
        )
    if fact.replacement_intent or fact.replacement_completed:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            f"Conflicting replacement flags for '{tgt.target_key}' at pre-intent check"
        )

    # 2. Revalidate durable parent.
    base_rec = next(
        (t for t in evidence.targets if t.target_key == tgt.target_key), None
    )
    if base_rec is None:
        raise ConfiguredReplacementPreconditionError(
            f"No baseline record for '{tgt.target_key}'"
        )
    try:
        _verify_durable_parent(
            project_root=config.PROJECT_ROOT,
            current_parent_path=dest_parent,
            persisted_relative_path=base_rec.parent_relative_path,
            persisted_st_dev=base_rec.parent_st_dev,
            persisted_st_ino=base_rec.parent_st_ino,
            persisted_st_mode=base_rec.parent_st_mode,
        )
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError(
            f"Destination parent revalidation failed for '{tgt.target_key}'"
        ) from exc

    # 3. Revalidate destination against baseline.
    _verify_file_owned(
        tgt.path, expected_size=base_rec.size_bytes, expected_sha256=base_rec.sha256
    )

    # 4. Revalidate named sidecar baseline state.
    for suffix, sc_dict in [("-wal", base_rec.wal), ("-shm", base_rec.shm)]:
        bl_exists = sc_dict.get("present", False)
        bl_dev = sc_dict.get("st_dev")
        bl_ino = sc_dict.get("st_ino")
        bl_size = sc_dict.get("size_bytes")
        bl_mtime = sc_dict.get("mtime_ns")
        sidecar = tgt.path.parent / (tgt.path.name + suffix)
        try:
            sc_st = os.lstat(str(sidecar))
        except FileNotFoundError:
            if bl_exists:
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} disappeared since baseline for '{tgt.target_key}'"
                )
            continue
        if not bl_exists:
            raise ConfiguredReplacementPreconditionError(
                f"Sidecar {suffix} appeared since baseline for '{tgt.target_key}'"
            )
        if os.name != "nt" and bl_dev is not None and bl_ino is not None:
            curr_id = (
                sc_st.st_dev,
                sc_st.st_ino,
                sc_st.st_size,
                getattr(sc_st, "st_mtime_ns", None),
            )
            if curr_id != (bl_dev, bl_ino, bl_size, bl_mtime):
                raise ConfiguredReplacementPreconditionError(
                    f"Sidecar {suffix} identity drift for '{tgt.target_key}'"
                )

    # 5. Revalidate selected staged artifact.
    _verify_file_owned(
        staged_p,
        expected_size=sel_entry.size_bytes,
        expected_sha256=sel_entry.sha256,
        require_mode_0600=True,
        require_single_link=True,
    )

    # 6. Revalidate rollback artifact.
    _verify_rollback_binding(
        rb_dir,
        operation_id=operation_id,
        safety_backup_id=safety_backup_id,
        safety_manifest_sha256=saf_snap.manifest_sha256,
        target_key=tgt.target_key,
        kind=saf_entry.kind,
        index=idx,
        rollback_filename=rbfile,
        size_bytes=saf_entry.size_bytes,
        sha256=saf_entry.sha256,
    )

    # 7. Revalidate both same-filesystem relationships.
    try:
        dest_dev = os.lstat(str(dest_parent)).st_dev
        stage_dev = os.lstat(str(staged_p)).st_dev
        if dest_dev != stage_dev:
            raise ConfiguredReplacementPreconditionError(
                f"Staged artifact on different filesystem for '{tgt.target_key}'"
            )
        rb_dev = os.lstat(str(rb_dir)).st_dev
        if rb_dev != dest_dev:
            raise ConfiguredReplacementPreconditionError(
                f"Rollback artifact on different filesystem for '{tgt.target_key}'"
            )
    except ConfiguredReplacementPreconditionError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementPreconditionError(
            f"Filesystem relationship check failed for '{tgt.target_key}'"
        ) from exc


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
        raise ConfiguredReplacementPostcheckError(
            "Postcheck: target discovery failed"
        ) from exc
    if tuple(t.target_key for t in cur_tgts) != tuple(t.target_key for t in targets):
        raise ConfiguredReplacementPostcheckError("Postcheck: target set mismatch")
    if tuple(t.target_key for t in cur_tgts) != journal.target_keys:
        raise ConfiguredReplacementPostcheckError("Postcheck: target keys mismatch journal")

    for fact in journal.targets:
        if fact.state is not TargetRestoreState.REPLACED or not fact.replacement_completed:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: target '{fact.target_key}' not fully replaced"
            )
        if fact.rollback_intent or fact.rollback_completed:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: rollback flags set for '{fact.target_key}'"
            )

    sel_entries = {e.target_key: e for e in selected_snapshot.entries}
    for tgt in targets:
        entry = sel_entries.get(tgt.target_key)
        if entry is None:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: no backup entry for '{tgt.target_key}'"
            )
        try:
            _verify_file_owned(
                tgt.path, expected_size=entry.size_bytes, expected_sha256=entry.sha256
            )
        except ConfiguredReplacementPreconditionError as exc:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: database SHA mismatch for '{tgt.target_key}'"
            ) from exc
        check = inspect_sqlite(tgt.path, deep=True)
        if not check.readable or not check.quick_check_ok:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: quick_check failed for '{tgt.target_key}'"
            )
        if not check.integrity_check_ok:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: integrity_check failed for '{tgt.target_key}'"
            )
        if not check.foreign_keys_ok:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: foreign_key_check failed for '{tgt.target_key}'"
            )
        fp = schema_fingerprint(tgt.path)
        if fp != entry.schema_fingerprint:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: schema fingerprint mismatch for '{tgt.target_key}'"
            )
        markers = migration_markers(tgt.path, entry.kind)
        if markers != {
            "ledger": entry.migration_ledger,
            "keys": list(entry.migration_keys),
            "state": entry.migration_state,
        }:
            raise ConfiguredReplacementPostcheckError(
                f"Postcheck: migration markers mismatch for '{tgt.target_key}'"
            )
        if os.name != "nt":
            dst = os.stat(str(tgt.path), follow_symlinks=False)
            if stat.S_IMODE(dst.st_mode) not in {0o600, 0o400}:
                raise ConfiguredReplacementPostcheckError(
                    f"Postcheck: mode not private for '{tgt.target_key}'"
                )
        for sfx in ("-wal", "-shm"):
            if (tgt.path.parent / (tgt.path.name + sfx)).exists():
                raise ConfiguredReplacementPostcheckError(
                    f"Postcheck: {sfx} sidecar present for '{tgt.target_key}'"
                )

    # Backup integrity: validate against immutable journal-bound manifest SHAs,
    # not against the snapshots' own manifest_sha256 (which would be circular).
    _validate_snapshot_post_mutation(
        selected_snapshot,
        expected_backup_id=journal.selected_backup_id,
        expected_manifest_sha256=journal.selected_backup_manifest_sha256,
        runtime_mode=runtime_mode,
        expected_target_keys=journal.target_keys,
        backup_root=backup_root,
    )
    if journal.safety_backup_manifest_sha256 is not None:
        _validate_snapshot_post_mutation(
            safety_snapshot,
            expected_backup_id=journal.safety_backup_id,
            expected_manifest_sha256=journal.safety_backup_manifest_sha256,
            runtime_mode=runtime_mode,
            expected_target_keys=journal.target_keys,
            backup_root=backup_root,
        )


# ---------------------------------------------------------------------------
# Complete rollback safety state verification
# ---------------------------------------------------------------------------

def _verify_complete_rollback_state(
    *,
    operation_id: str,
    targets: tuple,
    safety_snapshot: ValidatedBackupSnapshot,
    selected_snapshot: ValidatedBackupSnapshot,
    restore_root: Path,
    backup_root: Path,
    evidence: DestinationBaselineEvidence | None,
) -> None:
    """Full safety-state verification before ROLLED_BACK → FAILED_SAFE transition.

    For each target with rollback_intent: verifies exact safety bytes, private mode,
    no sidecars, and full SQLite checks.  For never-replaced targets: verifies exact
    original destination-baseline bytes, durable parent identity, original mode, and
    original named sidecar presence/absence/identity.  Confirms both backups are
    unchanged via immutable journal-bound manifest SHAs.
    """
    journal = load_restore_journal(operation_id, root=restore_root)
    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"
    if journal.runtime_mode != runtime_mode:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Rollback safety check: runtime mode mismatch"
        )
    try:
        cur_tgts = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Rollback safety check: target discovery failed"
        ) from exc
    if tuple(t.target_key for t in cur_tgts) != tuple(t.target_key for t in targets):
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Rollback safety check: target set mismatch"
        )

    saf_entries = {e.target_key: e for e in safety_snapshot.entries}
    for tgt in targets:
        sentry = saf_entries.get(tgt.target_key)
        if sentry is None:
            raise ConfiguredReplacementManualRecoveryRequiredError(
                f"Rollback safety check: no safety entry for '{tgt.target_key}'"
            )
        fact = next((f for f in journal.targets if f.target_key == tgt.target_key), None)
        if fact is None:
            raise ConfiguredReplacementManualRecoveryRequiredError(
                f"Rollback safety check: no journal fact for '{tgt.target_key}'"
            )

        if fact.rollback_intent:
            # Target was actively rolled back; require full rollback completion and exact safety bytes.
            if not fact.rollback_completed or fact.state is not TargetRestoreState.ROLLED_BACK:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: '{tgt.target_key}' not fully rolled back"
                )
            try:
                _verify_file_owned(
                    tgt.path,
                    expected_size=sentry.size_bytes,
                    expected_sha256=sentry.sha256,
                    require_mode_0600=True,
                    require_single_link=True,
                )
            except ConfiguredReplacementPreconditionError as exc:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: '{tgt.target_key}' does not match safety bytes"
                ) from exc
            if os.name != "nt":
                pst = os.stat(str(tgt.path), follow_symlinks=False)
                if stat.S_IMODE(pst.st_mode) not in {0o600, 0o400}:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback safety check: mode not private for '{tgt.target_key}'"
                    )
            for sfx in ("-wal", "-shm"):
                if (tgt.path.parent / (tgt.path.name + sfx)).exists():
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback safety check: stale {sfx} sidecar for '{tgt.target_key}'"
                    )
        else:
            # Target was never replaced.  An inconsistent mixture of flags is manual recovery.
            if fact.replacement_completed or fact.rollback_completed:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: inconsistent flags for never-replaced '{tgt.target_key}'"
                )
            # Freshly prove the target against original destination baseline.
            if evidence is None:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: no baseline evidence for never-replaced '{tgt.target_key}'"
                )
            base_rec = next(
                (t for t in evidence.targets if t.target_key == tgt.target_key), None
            )
            if base_rec is None:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: no baseline record for '{tgt.target_key}'"
                )
            try:
                _verify_file_owned(
                    tgt.path,
                    expected_size=base_rec.size_bytes,
                    expected_sha256=base_rec.sha256,
                )
            except ConfiguredReplacementPreconditionError as exc:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: '{tgt.target_key}' does not match original baseline bytes"
                ) from exc
            # Verify durable parent identity.
            try:
                _verify_durable_parent(
                    project_root=config.PROJECT_ROOT,
                    current_parent_path=tgt.path.parent.resolve(),
                    persisted_relative_path=base_rec.parent_relative_path,
                    persisted_st_dev=base_rec.parent_st_dev,
                    persisted_st_ino=base_rec.parent_st_ino,
                    persisted_st_mode=base_rec.parent_st_mode,
                )
            except Exception as exc:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback safety check: parent identity changed for '{tgt.target_key}'"
                ) from exc
            # Verify original mode has not changed.
            if os.name != "nt":
                cur_st = os.stat(str(tgt.path), follow_symlinks=False)
                if stat.S_IMODE(cur_st.st_mode) != stat.S_IMODE(base_rec.st_mode):
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback safety check: mode changed for never-replaced '{tgt.target_key}'"
                    )
            # Verify original named sidecar presence/absence and identity.
            for sfx, sc_key in (("-wal", "wal"), ("-shm", "shm")):
                sc_path = tgt.path.parent / (tgt.path.name + sfx)
                sc_dict: dict = getattr(base_rec, sc_key, {})
                sc_was_present = sc_dict.get("present", False)
                if sc_was_present:
                    if not sc_path.exists():
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback safety check: sidecar {sfx} disappeared for '{tgt.target_key}'"
                        )
                    try:
                        cur_sc = os.lstat(str(sc_path))
                    except OSError as exc:
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback safety check: cannot stat sidecar {sfx} for '{tgt.target_key}'"
                        ) from exc
                    if (cur_sc.st_dev, cur_sc.st_ino) != (sc_dict.get("st_dev"), sc_dict.get("st_ino")):
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback safety check: sidecar {sfx} identity changed for '{tgt.target_key}'"
                        )
                else:
                    if sc_path.exists():
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback safety check: unexpected sidecar {sfx} for '{tgt.target_key}'"
                        )

    # Backups unchanged: validate against immutable journal-bound manifest SHAs.
    _validate_snapshot_post_mutation(
        selected_snapshot,
        expected_backup_id=journal.selected_backup_id,
        expected_manifest_sha256=journal.selected_backup_manifest_sha256,
        runtime_mode=runtime_mode,
        expected_target_keys=journal.target_keys,
        backup_root=backup_root,
    )
    if journal.safety_backup_manifest_sha256 is not None:
        _validate_snapshot_post_mutation(
            safety_snapshot,
            expected_backup_id=journal.safety_backup_id,
            expected_manifest_sha256=journal.safety_backup_manifest_sha256,
            runtime_mode=runtime_mode,
            expected_target_keys=journal.target_keys,
            backup_root=backup_root,
        )


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
    backup_root: Path,
    baseline_evidence: DestinationBaselineEvidence | None = None,
) -> None:
    """Re-entrant rollback: control first, then data in reverse. Always raises."""
    try:
        journal = load_restore_journal(operation_id, root=restore_root)
        if journal.stage is not RestoreStage.ROLLBACK_REQUIRED:
            _journal_transition(
                operation_id, restore_root, stage=RestoreStage.ROLLBACK_REQUIRED
            )
    except ConfiguredJournalUncertaintyError:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Rollback: ROLLBACK_REQUIRED transition failed"
        )
    except Exception as exc:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Rollback: journal load failed"
        ) from exc

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
            fact = next(
                (f for f in journal.targets if f.target_key == tgt.target_key), None
            )
            if fact is None:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: no fact for '{tgt.target_key}'"
                )
            if fact.rollback_completed and fact.state is TargetRestoreState.ROLLED_BACK:
                try:
                    _verify_file_owned(
                        tgt.path,
                        expected_size=sentry.size_bytes,
                        expected_sha256=sentry.sha256,
                    )
                except Exception as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: rolled-back target '{tgt.target_key}' does not match "
                        f"safety bytes"
                    ) from exc
                continue

            is_sel = is_saf = False
            if tgt.path.exists() and not tgt.path.is_symlink():
                try:
                    _verify_file_owned(
                        tgt.path,
                        expected_size=sentry.size_bytes,
                        expected_sha256=sentry.sha256,
                    )
                    is_saf = True
                except Exception:
                    pass
                if not is_saf:
                    try:
                        _verify_file_owned(
                            tgt.path,
                            expected_size=sentry_sel.size_bytes,
                            expected_sha256=sentry_sel.sha256,
                        )
                        is_sel = True
                    except Exception:
                        pass

            was_replaced = (
                fact.state is TargetRestoreState.REPLACED
                or fact.replacement_completed
                or is_sel
            )
            if not was_replaced:
                if is_saf:
                    continue
                # SQLite backup produces different bytes than the source.  If the
                # current destination still has original baseline bytes (never replaced)
                # it is also safe to skip rollback for this target.
                if baseline_evidence is not None:
                    base_rec = next(
                        (
                            t for t in baseline_evidence.targets
                            if t.target_key == tgt.target_key
                        ),
                        None,
                    )
                    if base_rec is not None:
                        try:
                            _verify_file_owned(
                                tgt.path,
                                expected_size=base_rec.size_bytes,
                                expected_sha256=base_rec.sha256,
                            )
                            continue
                        except Exception:
                            pass
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: target '{tgt.target_key}' matches neither safety, "
                    f"selected, nor baseline bytes"
                )

            if fact.state is not TargetRestoreState.REPLACED or not fact.replacement_completed:
                try:
                    _journal_transition(
                        operation_id, restore_root,
                        target_key=tgt.target_key,
                        target_state=TargetRestoreState.REPLACED,
                        replacement_completed=True,
                    )
                except ConfiguredJournalUncertaintyError:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: replacement reconciliation failed for '{tgt.target_key}'"
                    )

            journal = load_restore_journal(operation_id, root=restore_root)
            fact = next(f for f in journal.targets if f.target_key == tgt.target_key)
            if not fact.rollback_intent:
                try:
                    _journal_transition(
                        operation_id, restore_root,
                        target_key=tgt.target_key,
                        rollback_intent=True,
                    )
                except ConfiguredJournalUncertaintyError:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: intent journal failed for '{tgt.target_key}'"
                    )

            already_safe = False
            try:
                _verify_file_owned(
                    tgt.path,
                    expected_size=sentry.size_bytes,
                    expected_sha256=sentry.sha256,
                )
                already_safe = True
            except Exception:
                already_safe = False

            if not already_safe:
                rbfile = _rollback_artifact_name(index, tgt.target_key)
                rb_dir = tgt.path.parent.resolve() / _rollback_dir_name(
                    operation_id, index
                )
                try:
                    rb_artifact = _verify_rollback_binding(
                        rb_dir,
                        operation_id=operation_id,
                        safety_backup_id=safety_snapshot.backup_id,
                        safety_manifest_sha256=safety_snapshot.manifest_sha256,
                        target_key=tgt.target_key,
                        kind=sentry.kind,
                        index=index,
                        rollback_filename=rbfile,
                        size_bytes=sentry.size_bytes,
                        sha256=sentry.sha256,
                    )
                except ConfiguredReplacementPreconditionError as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: artifact verification failed for '{tgt.target_key}'"
                    ) from exc

                journal = load_restore_journal(operation_id, root=restore_root)
                try:
                    journal = _handle_configured_sidecars(
                        tgt.path, journal, tgt.target_key, restore_root,
                        baseline_evidence=baseline_evidence,
                    )
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

                # Descriptor-bound post-rollback verification.
                # No pathname chmod. Apply fchmod via owned descriptor.
                rep_fd = None
                try:
                    rep_fd = _open_nf(tgt.path)
                    rep_st = os.fstat(rep_fd)
                    pst = os.stat(str(tgt.path), follow_symlinks=False)
                    if (pst.st_dev, pst.st_ino) != (rep_st.st_dev, rep_st.st_ino):
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: path/fd identity mismatch for '{tgt.target_key}'"
                        )
                    if not stat.S_ISREG(rep_st.st_mode):
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: not a regular file for '{tgt.target_key}'"
                        )
                    if os.name != "nt" and rep_st.st_nlink != 1:
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: unexpected link count for '{tgt.target_key}'"
                        )
                    if rep_st.st_size != sentry.size_bytes:
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: size mismatch for '{tgt.target_key}'"
                        )
                    actual_sha = _sha256_fd(rep_fd, sentry.size_bytes)
                    if actual_sha != sentry.sha256:
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: SHA-256 mismatch for '{tgt.target_key}'"
                        )
                    # Descriptor-bound permission finalization. No pathname chmod.
                    if os.name != "nt":
                        os.fchmod(rep_fd, 0o600)
                        pst2 = os.fstat(rep_fd)
                        if stat.S_IMODE(pst2.st_mode) != 0o600:
                            raise ConfiguredReplacementManualRecoveryRequiredError(
                                f"Rollback: fchmod did not take effect for '{tgt.target_key}'"
                            )
                    os.fsync(rep_fd)
                    # Re-stat pathname after fchmod and fsync.
                    post_pst = os.stat(str(tgt.path), follow_symlinks=False)
                    if (post_pst.st_dev, post_pst.st_ino) != (rep_st.st_dev, rep_st.st_ino):
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Rollback: pathname changed after fchmod for '{tgt.target_key}'"
                        )
                except ConfiguredReplacementManualRecoveryRequiredError:
                    raise
                except OSError as exc:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: descriptor verification failed for '{tgt.target_key}'"
                    ) from exc
                finally:
                    if rep_fd is not None:
                        try:
                            os.close(rep_fd)
                        except OSError:
                            pass

                # Complete SQLite and schema/marker verification.
                chk = inspect_sqlite(tgt.path, deep=True)
                if not chk.readable or not chk.quick_check_ok:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: quick_check failed for '{tgt.target_key}'"
                    )
                if not chk.integrity_check_ok:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: integrity_check failed for '{tgt.target_key}'"
                    )
                if not chk.foreign_keys_ok:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: foreign_key_check failed for '{tgt.target_key}'"
                    )
                fp = schema_fingerprint(tgt.path)
                if fp != sentry.schema_fingerprint:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: schema fingerprint mismatch for '{tgt.target_key}'"
                    )
                markers = migration_markers(tgt.path, sentry.kind)
                if markers != {
                    "ledger": sentry.migration_ledger,
                    "keys": list(sentry.migration_keys),
                    "state": sentry.migration_state,
                }:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"Rollback: migration markers mismatch for '{tgt.target_key}'"
                    )
                _fsync_path(tgt.path)
                _fsync_path(tgt.path.parent.resolve(), directory=True)

            try:
                _journal_transition(
                    operation_id, restore_root,
                    target_key=tgt.target_key,
                    target_state=TargetRestoreState.ROLLED_BACK,
                    rollback_completed=True,
                )
            except ConfiguredJournalUncertaintyError:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    f"Rollback: completion journal failed for '{tgt.target_key}'"
                )

        # Complete safety state verification before ROLLED_BACK → FAILED_SAFE.
        _verify_complete_rollback_state(
            operation_id=operation_id,
            targets=targets,
            safety_snapshot=safety_snapshot,
            selected_snapshot=selected_snapshot,
            restore_root=restore_root,
            backup_root=backup_root,
            evidence=baseline_evidence,
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
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Rollback failure"
        ) from exc


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
        raise ConfiguredReplacementCleanupError(
            f"Evidence cleanup incomplete: {'; '.join(errors)}"
        )


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

    # Descriptor-bound binding read: no pathname read_bytes for ownership proof.
    fd = None
    bst_rec = None
    try:
        fd = os.open(str(binding_p), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        bst = os.fstat(fd)
        pre_pst = os.lstat(str(binding_p))
        if (bst.st_dev, bst.st_ino) != (pre_pst.st_dev, pre_pst.st_ino):
            raise ConfiguredReplacementCleanupError(
                "Binding path/fd identity mismatch"
            )
        if not stat.S_ISREG(bst.st_mode):
            raise ConfiguredReplacementCleanupError("Binding is not a regular file")
        if bst.st_size > _MAX_BINDING_BYTES:
            raise ConfiguredReplacementCleanupError("Binding file too large")
        raw = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
        post_bst = os.fstat(fd)
        post_pst = os.lstat(str(binding_p))
        if (post_bst.st_dev, post_bst.st_ino, post_bst.st_size) != (
            bst.st_dev, bst.st_ino, bst.st_size
        ):
            raise ConfiguredReplacementCleanupError("Binding changed during descriptor read")
        if (post_pst.st_dev, post_pst.st_ino) != (bst.st_dev, bst.st_ino):
            raise ConfiguredReplacementCleanupError(
                "Binding path identity changed during read"
            )
        bst_rec = (bst.st_dev, bst.st_ino, bst.st_size)
    except ConfiguredReplacementCleanupError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not open or read binding") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    try:
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
    for name in actual:
        child = directory / name
        try:
            cst = os.lstat(str(child))
        except OSError as exc:
            raise ConfiguredReplacementCleanupError(f"Could not stat '{name}'") from exc
        if stat.S_ISLNK(cst.st_mode) or not stat.S_ISREG(cst.st_mode):
            raise ConfiguredReplacementCleanupError(f"Child '{name}' is not a regular file")

    # Record complete identity of each artifact (dev, ino, size, mtime_ns, nlink).
    file_recs: dict[str, tuple] = {}
    for name in artifact_names:
        child = directory / name
        try:
            cst = os.lstat(str(child))
            file_recs[name] = (
                cst.st_dev, cst.st_ino, cst.st_size,
                getattr(cst, "st_mtime_ns", None), cst.st_nlink,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfiguredReplacementCleanupError(
                f"Could not stat artifact '{name}'"
            ) from exc

    # Re-verify directory identity before unlink.
    try:
        curr = os.stat(str(directory), follow_symlinks=False)
        if (curr.st_dev, curr.st_ino) != dir_id:
            raise ConfiguredReplacementCleanupError("Directory identity changed")
    except ConfiguredReplacementCleanupError:
        raise
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Directory re-stat failed") from exc

    # Unlink artifacts with immediate pre-unlink identity revalidation.
    for name, (dev, ino, sz, mt, nl) in file_recs.items():
        child = directory / name
        try:
            cst = os.lstat(str(child))
            curr_id = (
                cst.st_dev, cst.st_ino, cst.st_size,
                getattr(cst, "st_mtime_ns", None), cst.st_nlink,
            )
            if curr_id != (dev, ino, sz, mt, nl):
                raise ConfiguredReplacementCleanupError(
                    f"Artifact '{name}' changed before unlink"
                )
            os.unlink(str(child))
        except ConfiguredReplacementCleanupError:
            raise
        except OSError as exc:
            raise ConfiguredReplacementCleanupError(
                f"Could not unlink '{name}'"
            ) from exc

    # Unlink binding with identity revalidation.
    try:
        cbst = os.lstat(str(binding_p))
        if (cbst.st_dev, cbst.st_ino, cbst.st_size) != (
            bst_rec[0], bst_rec[1], bst_rec[2]
        ):
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
        raise ConfiguredReplacementCleanupError(
            "Could not verify directory empty"
        ) from exc
    _fsync_path(directory, directory=True)
    try:
        directory.rmdir()
    except OSError as exc:
        raise ConfiguredReplacementCleanupError("Could not rmdir") from exc
    _fsync_path(directory.parent, directory=True)


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
    _service_checker=None,
) -> ConfiguredReplacementResult:
    """Execute configured-runtime restore replacement, postcheck, and rollback.

    Accepts no arbitrary paths. Rediscovers canonical targets from config.
    Requires explicit service-stopped proof (injectable via _service_checker).
    Acquires ProcessLock -> RestoreLock -> BackupLock before any mutation.
    Releases all locks before returning a frozen result.
    Raises a bounded exception on every failure path.
    """
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ConfiguredReplacementPreconditionError("Operation ID format is invalid")
    if not _BACKUP_ID_RE.fullmatch(selected_backup_id):
        raise ConfiguredReplacementPreconditionError("Selected backup ID format is invalid")

    # Service-stopped proof #1 (before lock acquisition).
    _require_service_stopped(_service_checker)

    restore_root = validate_restore_root(config.OPERATOR_RESTORE_ROOT)
    backup_root = validate_backup_root(config.OPERATOR_BACKUP_ROOT)
    runtime_mode = "multi_user" if config.MULTI_USER_ENABLED else "single_user"

    journal = load_restore_journal(operation_id, root=restore_root)
    if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        raise ConfiguredReplacementManualRecoveryRequiredError(
            "Operation is in FAILED_MANUAL_RECOVERY_REQUIRED"
        )
    if journal.selected_backup_id != selected_backup_id:
        raise ConfiguredReplacementPreconditionError(
            "selected_backup_id does not match journal"
        )
    if journal.expected_application_commit != expected_application_commit:
        raise ConfiguredReplacementPreconditionError(
            "expected_application_commit does not match journal"
        )
    if journal.runtime_mode != runtime_mode:
        raise ConfiguredReplacementPreconditionError("runtime_mode does not match journal")

    try:
        configured_targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError("Target discovery failed") from exc
    if tuple(t.target_key for t in configured_targets) != journal.target_keys:
        raise ConfiguredReplacementPreconditionError(
            "Discovered target keys mismatch journal"
        )

    rh = target_set_hash(
        backup_id=selected_backup_id,
        manifest_sha256=journal.selected_backup_manifest_sha256,
        runtime_mode=runtime_mode,
        target_keys=journal.target_keys,
    )
    rc = confirmation_value(
        target_hash=rh, expected_application_commit=expected_application_commit
    )
    if confirmed_target_set_hash != rh:
        raise ConfiguredReplacementPreconditionError("confirmed_target_set_hash mismatch")
    if confirmed_restore_value != rc:
        raise ConfiguredReplacementPreconditionError("confirmed_restore_value mismatch")
    if journal.target_set_hash != rh or journal.confirmation_value != rc:
        raise ConfiguredReplacementPreconditionError(
            "Journal target-set hash or confirmation mismatch"
        )

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

    # Track the primary exception type for lock-release precedence.
    _primary_exc: BaseException | None = None

    try:
        project_root = safe_resolve(config.PROJECT_ROOT)
        try:
            proc_lock = acquire_process_lock(project_root / "garmincoach.lock")
        except Exception as exc:
            raise RestoreLockError("Could not acquire application process lock") from exc

        # Service-stopped proof #2 (after process lock acquisition).
        try:
            _require_service_stopped(_service_checker)
        except ConfiguredReplacementPreconditionError:
            raise

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

        # Handle terminal stages early, before snapshot loading.
        if journal.stage is RestoreStage.FAILED_SAFE:
            _primary_exc = ConfiguredReplacementRollbackCompletedError(
                "Operation already settled FAILED_SAFE"
            )
            raise _primary_exc
        if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
            _primary_exc = ConfiguredReplacementManualRecoveryRequiredError(
                "Operation in FAILED_MANUAL_RECOVERY_REQUIRED"
            )
            raise _primary_exc

        safety_backup_id = journal.safety_backup_id
        if safety_backup_id is None:
            raise ConfiguredReplacementPreconditionError(
                "Safety backup ID missing from journal"
            )

        # Post-mutation stages must not check against (possibly transitional) current DBs.
        _post_mutation_stages = {
            RestoreStage.REPLACING, RestoreStage.REPLACED, RestoreStage.POSTCHECK_PASSED,
            RestoreStage.COMPLETED,
            RestoreStage.ROLLBACK_REQUIRED, RestoreStage.ROLLED_BACK,
        }
        _check_current = journal.stage not in _post_mutation_stages

        try:
            sel_snap = load_validated_backup_snapshot(
                backup_root / f"backup-{selected_backup_id}",
                against_current_config=_check_current,
            )
        except BackupError as exc:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError(
                "Selected backup validation failed"
            ) from exc
        try:
            saf_snap = load_validated_backup_snapshot(
                backup_root / f"backup-{safety_backup_id}",
                against_current_config=_check_current,
            )
        except BackupError as exc:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError(
                "Safety backup validation failed"
            ) from exc

        # Backward-compat: refuse operations on journals missing the immutable safety
        # manifest SHA.  Pre-mutation journals (up to REPLACEMENT_READY) require a new
        # prepare operation; post-mutation journals signal manual recovery.
        if journal.safety_backup_id is not None and journal.safety_backup_manifest_sha256 is None:
            if journal.stage in {RestoreStage.REPLACEMENT_READY}:
                raise ConfiguredReplacementPreconditionError(
                    "Safety backup manifest SHA-256 missing from journal; prepare a new operation"
                )
            _primary_exc = ConfiguredReplacementManualRecoveryRequiredError(
                "Safety backup manifest SHA-256 missing from journal; cannot verify backup identity"
            )
            raise _primary_exc

        # Bind loaded snapshots to immutable journal evidence.
        if sel_snap.manifest_sha256 != journal.selected_backup_manifest_sha256:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError(
                "Selected backup manifest SHA-256 does not match journal"
            )
        if journal.safety_backup_manifest_sha256 is not None:
            if saf_snap.manifest_sha256 != journal.safety_backup_manifest_sha256:
                _settle_safe(operation_id, restore_root, journal)
                raise ConfiguredReplacementPreconditionError(
                    "Safety backup manifest SHA-256 does not match journal"
                )

        # Explicitly validate post-mutation snapshot fields without current-config check.
        if not _check_current:
            try:
                _validate_snapshot_post_mutation(
                    sel_snap,
                    expected_backup_id=selected_backup_id,
                    expected_manifest_sha256=journal.selected_backup_manifest_sha256,
                    runtime_mode=runtime_mode,
                    expected_target_keys=journal.target_keys,
                    backup_root=backup_root,
                )
                _validate_snapshot_post_mutation(
                    saf_snap,
                    expected_backup_id=safety_backup_id,
                    expected_manifest_sha256=journal.safety_backup_manifest_sha256,
                    runtime_mode=runtime_mode,
                    expected_target_keys=journal.target_keys,
                    backup_root=backup_root,
                )
            except ConfiguredReplacementPreconditionError:
                _settle_safe(operation_id, restore_root, journal)
                raise

        try:
            evidence, _ev_sha = load_destination_baseline_evidence(
                operation_id, restore_root=restore_root
            )
        except Exception as exc:
            _settle_safe(operation_id, restore_root, journal)
            raise ConfiguredReplacementPreconditionError(
                "Baseline evidence load failed"
            ) from exc

        # ---- ROLLED_BACK re-entry ----
        if journal.stage is RestoreStage.ROLLED_BACK:
            # Full safety state verification before FAILED_SAFE (freshly proves every target).
            try:
                _verify_complete_rollback_state(
                    operation_id=operation_id,
                    targets=configured_targets,
                    safety_snapshot=saf_snap,
                    selected_snapshot=sel_snap,
                    restore_root=restore_root,
                    backup_root=backup_root,
                    evidence=evidence,
                )
            except ConfiguredReplacementManualRecoveryRequiredError as exc:
                _primary_exc = exc
                _settle_manual(operation_id, restore_root)
                raise
            _journal_transition(operation_id, restore_root, stage=RestoreStage.FAILED_SAFE)
            _primary_exc = ConfiguredReplacementRollbackCompletedError(
                "Advanced ROLLED_BACK to FAILED_SAFE"
            )
            raise _primary_exc

        # ---- COMPLETED re-entry ----
        if journal.stage is RestoreStage.COMPLETED:
            # Full postcheck before returning idempotent success.
            try:
                _run_complete_postcheck(
                    operation_id=operation_id,
                    selected_snapshot=sel_snap,
                    safety_snapshot=saf_snap,
                    targets=configured_targets,
                    backup_root=backup_root,
                    restore_root=restore_root,
                )
            except ConfiguredReplacementPostcheckError as exc:
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    "COMPLETED re-entry: postcheck failed; databases may have drifted"
                ) from exc
            try:
                _cleanup_evidence(operation_id, configured_targets)
            except ConfiguredReplacementCleanupError:
                raise
            replaced_keys = tuple(
                f.target_key for f in journal.targets if f.replacement_completed
            )
            return ConfiguredReplacementResult(
                operation_id=operation_id, stage=RestoreStage.COMPLETED,
                selected_backup_id=selected_backup_id,
                safety_backup_id=journal.safety_backup_id or "",
                runtime_mode=runtime_mode, target_keys=journal.target_keys,
                replaced_target_keys=replaced_keys, rollback_occurred=False,
                configured_database_mutated=True, locks_released=True,
            )

        # ---- ROLLBACK_REQUIRED ----
        if journal.stage is RestoreStage.ROLLBACK_REQUIRED:
            _run_rollback(
                operation_id=operation_id, selected_snapshot=sel_snap,
                safety_snapshot=saf_snap, targets=configured_targets,
                restore_root=restore_root, backup_root=backup_root,
                baseline_evidence=evidence,
            )
            raise ConfiguredReplacementManualRecoveryRequiredError(
                "Rollback did not raise"
            )

        # ---- REPLACED ----
        if journal.stage is RestoreStage.REPLACED:
            try:
                _run_complete_postcheck(
                    operation_id=operation_id, selected_snapshot=sel_snap,
                    safety_snapshot=saf_snap, targets=configured_targets,
                    backup_root=backup_root, restore_root=restore_root,
                )
                journal = _journal_transition(
                    operation_id, restore_root, stage=RestoreStage.POSTCHECK_PASSED
                )
            except ConfiguredReplacementPostcheckError:
                _run_rollback(
                    operation_id=operation_id, selected_snapshot=sel_snap,
                    safety_snapshot=saf_snap, targets=configured_targets,
                    restore_root=restore_root, backup_root=backup_root,
                    baseline_evidence=evidence,
                )
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    "Postcheck failed; rollback did not raise"
                )

        # ---- POSTCHECK_PASSED ----
        if journal.stage is RestoreStage.POSTCHECK_PASSED:
            try:
                _run_complete_postcheck(
                    operation_id=operation_id, selected_snapshot=sel_snap,
                    safety_snapshot=saf_snap, targets=configured_targets,
                    backup_root=backup_root, restore_root=restore_root,
                )
            except ConfiguredReplacementPostcheckError:
                _run_rollback(
                    operation_id=operation_id, selected_snapshot=sel_snap,
                    safety_snapshot=saf_snap, targets=configured_targets,
                    restore_root=restore_root, backup_root=backup_root,
                    baseline_evidence=evidence,
                )
                raise ConfiguredReplacementManualRecoveryRequiredError(
                    "POSTCHECK_PASSED re-verify failed"
                )
            journal = _journal_transition(
                operation_id, restore_root, stage=RestoreStage.COMPLETED
            )
            try:
                _cleanup_evidence(operation_id, configured_targets)
            except ConfiguredReplacementCleanupError:
                raise
            replaced_keys = tuple(
                f.target_key for f in journal.targets if f.replacement_completed
            )
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
                    operation_id=operation_id,
                    expected_application_commit=expected_application_commit,
                    selected_backup_id=selected_backup_id,
                    selected_snapshot=sel_snap,
                    safety_backup_id=safety_backup_id,
                    safety_snapshot=saf_snap,
                    targets=configured_targets,
                    confirmed_target_set_hash=confirmed_target_set_hash,
                    confirmed_restore_value=confirmed_restore_value,
                    journal=journal,
                    restore_root=restore_root,
                    backup_root=backup_root,
                    evidence=evidence,
                )
            except ConfiguredReplacementPreconditionError:
                _settle_safe(operation_id, restore_root, journal)
                raise

            for idx, tgt in enumerate(configured_targets):
                try:
                    _stage_rollback_artifact(
                        operation_id=operation_id, safety_snapshot=saf_snap,
                        target=tgt, index=idx,
                    )
                except ConfiguredReplacementPreconditionError:
                    _settle_safe(operation_id, restore_root, journal)
                    raise

            try:
                _verify_barrier_pre_mutation(
                    operation_id=operation_id,
                    expected_application_commit=expected_application_commit,
                    selected_backup_id=selected_backup_id,
                    selected_snapshot=sel_snap,
                    safety_backup_id=safety_backup_id,
                    safety_snapshot=saf_snap,
                    targets=configured_targets,
                    confirmed_target_set_hash=confirmed_target_set_hash,
                    confirmed_restore_value=confirmed_restore_value,
                    journal=journal,
                    restore_root=restore_root,
                    backup_root=backup_root,
                    evidence=evidence,
                )
            except ConfiguredReplacementPreconditionError:
                _settle_safe(operation_id, restore_root, journal)
                raise

            saf_entries = {e.target_key: e for e in saf_snap.entries}
            for idx, tgt in enumerate(configured_targets):
                sentry = saf_entries.get(tgt.target_key)
                if sentry is None:
                    _settle_safe(operation_id, restore_root, journal)
                    raise ConfiguredReplacementPreconditionError(
                        f"No safety entry for '{tgt.target_key}'"
                    )
                rbfile = _rollback_artifact_name(idx, tgt.target_key)
                rb_dir = tgt.path.parent.resolve() / _rollback_dir_name(
                    operation_id, idx
                )
                try:
                    _verify_rollback_binding(
                        rb_dir,
                        operation_id=operation_id,
                        safety_backup_id=safety_backup_id,
                        safety_manifest_sha256=saf_snap.manifest_sha256,
                        target_key=tgt.target_key,
                        kind=sentry.kind,
                        index=idx,
                        rollback_filename=rbfile,
                        size_bytes=sentry.size_bytes,
                        sha256=sentry.sha256,
                    )
                except ConfiguredReplacementPreconditionError:
                    _settle_safe(operation_id, restore_root, journal)
                    raise

            try:
                journal = _journal_transition(
                    operation_id, restore_root, stage=RestoreStage.REPLACING
                )
            except ConfiguredJournalUncertaintyError:
                _settle_safe(operation_id, restore_root, journal)
                raise ConfiguredReplacementPreconditionError(
                    "REPLACING transition journal failed"
                )

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
                    raise ConfiguredReplacementPreconditionError(
                        f"Missing entry for '{tgt.target_key}'"
                    )

                journal = load_restore_journal(operation_id, root=restore_root)
                fact = next(
                    (f for f in journal.targets if f.target_key == tgt.target_key), None
                )
                if fact is None:
                    raise ConfiguredReplacementManualRecoveryRequiredError(
                        f"No journal fact for '{tgt.target_key}'"
                    )

                # Already replaced and completed.
                if fact.replacement_completed and fact.state is TargetRestoreState.REPLACED:
                    _verify_file_owned(
                        tgt.path,
                        expected_size=entry.size_bytes,
                        expected_sha256=entry.sha256,
                    )
                    continue

                # Reconciliation: intent set but not completed.
                if fact.replacement_intent:
                    is_sel = False
                    try:
                        _verify_file_owned(
                            tgt.path,
                            expected_size=entry.size_bytes,
                            expected_sha256=entry.sha256,
                        )
                        is_sel = True
                    except Exception:
                        pass
                    if is_sel:
                        journal = _journal_transition(
                            operation_id, restore_root,
                            target_key=tgt.target_key,
                            target_state=TargetRestoreState.REPLACED,
                            replacement_completed=True,
                        )
                        continue

                    is_base = False
                    base_rec = next(
                        (t for t in evidence.targets if t.target_key == tgt.target_key), None
                    )
                    if base_rec is not None:
                        try:
                            _verify_file_owned(
                                tgt.path,
                                expected_size=base_rec.size_bytes,
                                expected_sha256=base_rec.sha256,
                            )
                            is_base = True
                        except Exception:
                            pass
                    if not is_base:
                        is_saf = False
                        try:
                            _verify_file_owned(
                                tgt.path,
                                expected_size=sentry.size_bytes,
                                expected_sha256=sentry.sha256,
                            )
                            is_saf = True
                        except Exception:
                            pass
                        if not is_saf:
                            raise ConfiguredReplacementManualRecoveryRequiredError(
                                f"REPLACING re-entry: '{tgt.target_key}' matches no known evidence"
                            )
                        continue

                dest_parent = tgt.path.parent.resolve()
                stage_dir = dest_parent / _staged_dir_name(operation_id)
                staged_p = stage_dir / _staged_artifact_name(idx, tgt.target_key)
                rbfile = _rollback_artifact_name(idx, tgt.target_key)
                rb_dir = dest_parent / _rollback_dir_name(operation_id, idx)

                # Per-target immediate baseline revalidation before replacement_intent.
                _revalidate_target_pre_intent(
                    tgt=tgt, idx=idx, operation_id=operation_id,
                    restore_root=restore_root, evidence=evidence,
                    sel_entry=entry, saf_entry=sentry,
                    safety_backup_id=safety_backup_id, saf_snap=saf_snap,
                    staged_p=staged_p, rb_dir=rb_dir, rbfile=rbfile,
                    dest_parent=dest_parent,
                )

                journal = _journal_transition(
                    operation_id, restore_root,
                    target_key=tgt.target_key,
                    replacement_intent=True,
                )

                # Immediately re-verify destination classification after intent persistence.
                base_rec = next(
                    (t for t in evidence.targets if t.target_key == tgt.target_key), None
                )
                if base_rec is not None:
                    try:
                        _verify_file_owned(
                            tgt.path,
                            expected_size=base_rec.size_bytes,
                            expected_sha256=base_rec.sha256,
                        )
                    except ConfiguredReplacementPreconditionError as exc:
                        raise ConfiguredReplacementManualRecoveryRequiredError(
                            f"Destination changed after intent persistence for "
                            f"'{tgt.target_key}'"
                        ) from exc

                journal = _handle_configured_sidecars(
                    tgt.path, journal, tgt.target_key, restore_root,
                    baseline_evidence=evidence,
                )

                try:
                    os.replace(str(staged_p), str(tgt.path))
                except OSError as exc:
                    raise ConfiguredReplacementPreconditionError(
                        f"os.replace failed for '{tgt.target_key}'"
                    ) from exc

                # Descriptor-bound post-replacement verification with fchmod.
                rep_fd = None
                try:
                    rep_fd = _open_nf(tgt.path)
                    rep_st = os.fstat(rep_fd)
                    pst = os.stat(str(tgt.path), follow_symlinks=False)
                    if (pst.st_dev, pst.st_ino) != (rep_st.st_dev, rep_st.st_ino):
                        raise ConfiguredReplacementPreconditionError(
                            f"Replacement path/fd identity mismatch for '{tgt.target_key}'"
                        )
                    if not stat.S_ISREG(rep_st.st_mode):
                        raise ConfiguredReplacementPreconditionError(
                            f"Replacement not a regular file for '{tgt.target_key}'"
                        )
                    if rep_st.st_size != entry.size_bytes:
                        raise ConfiguredReplacementPreconditionError(
                            f"Replacement size mismatch for '{tgt.target_key}'"
                        )
                    if os.name != "nt" and rep_st.st_nlink != 1:
                        raise ConfiguredReplacementPreconditionError(
                            f"Replacement unexpected link count for '{tgt.target_key}'"
                        )
                    actual_sha = _sha256_fd(rep_fd, entry.size_bytes)
                    if actual_sha != entry.sha256:
                        raise ConfiguredReplacementPreconditionError(
                            f"Replacement SHA-256 mismatch for '{tgt.target_key}'"
                        )
                    # Descriptor-bound permission finalization. No pathname chmod.
                    if os.name != "nt":
                        os.fchmod(rep_fd, 0o600)
                        pst2 = os.fstat(rep_fd)
                        if stat.S_IMODE(pst2.st_mode) != 0o600:
                            raise ConfiguredReplacementPreconditionError(
                                f"Replacement fchmod failed for '{tgt.target_key}'"
                            )
                    os.fsync(rep_fd)
                    # Re-stat pathname after fchmod and fsync.
                    post_pst = os.stat(str(tgt.path), follow_symlinks=False)
                    if (post_pst.st_dev, post_pst.st_ino) != (rep_st.st_dev, rep_st.st_ino):
                        raise ConfiguredReplacementPreconditionError(
                            f"Pathname changed after fchmod for '{tgt.target_key}'"
                        )
                finally:
                    if rep_fd is not None:
                        try:
                            os.close(rep_fd)
                        except OSError:
                            pass

                chk = inspect_sqlite(tgt.path, deep=True)
                if not chk.readable or not chk.quick_check_ok:
                    raise ConfiguredReplacementPreconditionError(
                        f"quick_check failed for '{tgt.target_key}'"
                    )
                if not chk.integrity_check_ok:
                    raise ConfiguredReplacementPreconditionError(
                        f"integrity_check failed for '{tgt.target_key}'"
                    )
                if not chk.foreign_keys_ok:
                    raise ConfiguredReplacementPreconditionError(
                        f"foreign_key_check failed for '{tgt.target_key}'"
                    )
                fp = schema_fingerprint(tgt.path)
                if fp != entry.schema_fingerprint:
                    raise ConfiguredReplacementPreconditionError(
                        f"Schema fingerprint mismatch for '{tgt.target_key}'"
                    )
                markers = migration_markers(tgt.path, entry.kind)
                if markers != {
                    "ledger": entry.migration_ledger,
                    "keys": list(entry.migration_keys),
                    "state": entry.migration_state,
                }:
                    raise ConfiguredReplacementPreconditionError(
                        f"Migration markers mismatch for '{tgt.target_key}'"
                    )

                _fsync_path(tgt.path.parent.resolve(), directory=True)
                journal = _journal_transition(
                    operation_id, restore_root,
                    target_key=tgt.target_key,
                    target_state=TargetRestoreState.REPLACED,
                    replacement_completed=True,
                )

            journal = _journal_transition(
                operation_id, restore_root, stage=RestoreStage.REPLACED
            )
            _run_complete_postcheck(
                operation_id=operation_id, selected_snapshot=sel_snap,
                safety_snapshot=saf_snap, targets=configured_targets,
                backup_root=backup_root, restore_root=restore_root,
            )
            journal = _journal_transition(
                operation_id, restore_root, stage=RestoreStage.POSTCHECK_PASSED
            )
            journal = _journal_transition(
                operation_id, restore_root, stage=RestoreStage.COMPLETED
            )
            try:
                _cleanup_evidence(operation_id, configured_targets)
            except ConfiguredReplacementCleanupError:
                raise
            replaced_keys = tuple(
                f.target_key for f in journal.targets if f.replacement_completed
            )
            return ConfiguredReplacementResult(
                operation_id=operation_id, stage=RestoreStage.COMPLETED,
                selected_backup_id=selected_backup_id, safety_backup_id=safety_backup_id,
                runtime_mode=runtime_mode, target_keys=journal.target_keys,
                replaced_target_keys=replaced_keys, rollback_occurred=False,
                configured_database_mutated=True, locks_released=True,
            )

        except ConfiguredReplacementCleanupError:
            _primary_exc = sys.exc_info()[1]
            raise
        except ConfiguredReplacementManualRecoveryRequiredError:
            _primary_exc = sys.exc_info()[1]
            _settle_manual(operation_id, restore_root)
            raise
        except ConfiguredReplacementRollbackCompletedError:
            _primary_exc = sys.exc_info()[1]
            raise
        except Exception as cause:
            try:
                _run_rollback(
                    operation_id=operation_id, selected_snapshot=sel_snap,
                    safety_snapshot=saf_snap, targets=configured_targets,
                    restore_root=restore_root, backup_root=backup_root,
                    baseline_evidence=evidence,
                )
            except (ConfiguredReplacementRollbackCompletedError,
                    ConfiguredReplacementCleanupError):
                _primary_exc = sys.exc_info()[1]
                raise
            except ConfiguredReplacementManualRecoveryRequiredError:
                _primary_exc = sys.exc_info()[1]
                raise
            except Exception as rb_exc:
                _settle_manual(operation_id, restore_root)
                exc = ConfiguredReplacementManualRecoveryRequiredError(
                    "Replacement and rollback both failed"
                )
                exc.__cause__ = rb_exc
                _primary_exc = exc
                raise exc from rb_exc
            exc = ConfiguredReplacementManualRecoveryRequiredError(
                "Rollback succeeded but control flow was unexpected"
            )
            exc.__cause__ = cause
            _primary_exc = exc
            raise exc from cause

    except (
        ConfiguredReplacementRollbackCompletedError,
        ConfiguredReplacementManualRecoveryRequiredError,
        ConfiguredReplacementCleanupError,
        ConfiguredRestoreLockReleaseError,
    ):
        raise
    except (
        RestoreLockError, ConfiguredJournalUncertaintyError, ConfiguredRestoreError,
        RestoreJournalError, RestoreJournalPersistenceError,
    ):
        raise
    except Exception as exc:
        raise ConfiguredReplacementPreconditionError(
            "Configured restore replacement failed"
        ) from exc
    finally:
        lock_errs: list[Exception] = []
        if bkup_lock is not None:
            try:
                bkup_lock.__exit__(None, None, None)
            except Exception as e:
                lock_errs.append(e)
            bkup_lock = None
        if rest_lock is not None:
            try:
                rest_lock.__exit__(None, None, None)
            except Exception as e:
                lock_errs.append(e)
            rest_lock = None
        if proc_lock is not None:
            try:
                release_process_lock(proc_lock)
            except Exception as e:
                lock_errs.append(e)
            proc_lock = None

        if lock_errs:
            lock_exc = ConfiguredRestoreLockReleaseError(
                "Failed to release locks cleanly"
            )
            lock_exc.__cause__ = lock_errs[0]
            # Determine outcome precedence.
            # Use sys.exc_info() to detect the active propagating exception.
            active = sys.exc_info()[1]
            if active is None:
                # No primary failure: lock release error IS the failure.
                raise lock_exc
            elif isinstance(
                active,
                (
                    ConfiguredReplacementManualRecoveryRequiredError,
                    ConfiguredReplacementRollbackCompletedError,
                    ConfiguredReplacementCleanupError,
                ),
            ):
                # Protected outcome: preserve primary; chain lock error as context.
                active.__context__ = lock_exc
                # Primary continues to propagate — no re-raise needed.
            else:
                # Non-protected exception: lock release error takes precedence.
                raise lock_exc
