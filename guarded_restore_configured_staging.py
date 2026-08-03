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


BASELINE_FORMAT = "garmincoach-destination-baseline-v1"
_BASELINE_FILENAME = "destination-baseline.json"
MAX_BASELINE_BYTES = 256 * 1024


@dataclass(frozen=True)
class TargetBaselineRecord:
    target_key: str
    kind: str
    tenant_uuid: str | None
    target_order: int
    configured_relative_path: str
    resolved_relative_path: str
    is_regular_file: bool
    st_dev: int
    st_ino: int
    size_bytes: int
    mtime_ns: int
    st_mode: int
    sha256: str
    parent_relative_path: str
    parent_st_dev: int
    parent_st_ino: int
    parent_is_dir: bool
    parent_st_mode: int
    wal: dict[str, Any]
    shm: dict[str, Any]


@dataclass(frozen=True)
class DestinationBaselineEvidence:
    format_version: str
    operation_id: str
    selected_backup_id: str
    selected_backup_manifest_sha256: str
    expected_application_commit: str
    runtime_mode: str
    target_set_hash: str
    confirmation_value: str
    ordered_target_keys: tuple[str, ...]
    targets: tuple[TargetBaselineRecord, ...]
    active_control_user_mapping: dict[str, Any]
    garminconnect_version: str
    captured_at: str


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


def publish_noreplace(partial_path: Path, final_path: Path) -> None:
    """Safely publish partial_path to final_path without overwriting.

    Fails if final_path exists. Verifies descriptor identity before and after.
    Fsyncs file and containing directory.
    """
    if final_path.exists() or final_path.is_symlink():
        raise ConfiguredStagingOwnershipError(f"Publication destination '{final_path.name}' already exists or is unsafe")

    if not partial_path.exists() or partial_path.is_symlink():
        raise ConfiguredStagingOwnershipError("Partial file to publish is missing or unsafe")

    partial_stat = os.stat(partial_path, follow_symlinks=False)
    if not stat.S_ISREG(partial_stat.st_mode):
        raise ConfiguredStagingOwnershipError("Partial file must be a regular file")

    published = False
    try:
        os.link(str(partial_path), str(final_path))
        published = True
    except FileExistsError:
        raise ConfiguredStagingOwnershipError(f"Publication destination '{final_path.name}' already exists")
    except OSError as exc:
        if final_path.exists():
            raise ConfiguredStagingOwnershipError(f"Publication destination '{final_path.name}' already exists") from exc
        try:
            desc = os.open(str(final_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG, 0o600)
            published = True
            with os.fdopen(desc, "wb") as f_out:
                f_out.write(partial_path.read_bytes())
                f_out.flush()
                os.fsync(f_out.fileno())
        except FileExistsError:
            raise ConfiguredStagingOwnershipError(f"Publication destination '{final_path.name}' already exists") from exc
        except OSError as sub_exc:
            raise ConfiguredStagingOwnershipError("Failed to publish file without overwriting") from sub_exc

    if not published:
        raise ConfiguredStagingOwnershipError("Failed to publish file without overwriting")

    curr_partial_stat = os.stat(partial_path, follow_symlinks=False)
    if (curr_partial_stat.st_dev, curr_partial_stat.st_ino) != (partial_stat.st_dev, partial_stat.st_ino):
        raise ConfiguredStagingOwnershipError("Partial file descriptor identity changed before unlink")

    try:
        os.unlink(str(partial_path))
    except OSError as exc:
        raise ConfiguredStagingOwnershipError("Failed to unlink partial file after publication") from exc

    final_stat = os.stat(final_path, follow_symlinks=False)
    if not stat.S_ISREG(final_stat.st_mode) or final_stat.st_size != partial_stat.st_size:
        raise ConfiguredStagingOwnershipError("Published file verification failed")

    _private(final_path)

    try:
        f_fd = os.open(str(final_path), os.O_RDONLY | _BINARY_FLAG)
        try:
            os.fsync(f_fd)
        finally:
            os.close(f_fd)
    except OSError:
        pass

    parent_dir = final_path.parent
    if os.name != "nt":
        try:
            p_fd = os.open(str(parent_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(p_fd)
            finally:
                os.close(p_fd)
        except OSError:
            pass


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
    if len(data_bytes) > MAX_BASELINE_BYTES:
        raise ConfiguredStagingOwnershipError("JSON file exceeds maximum size limit")

    def _reject_duplicates(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ConfiguredStagingOwnershipError(f"Duplicate key '{k}' in JSON payload")
            d[k] = v
        return d

    try:
        text = data_bytes.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicates)
        if not isinstance(parsed, dict):
            raise ConfiguredStagingOwnershipError("JSON root must be an object")
        return parsed
    except Exception as exc:
        if isinstance(exc, ConfiguredStagingOwnershipError):
            raise
        raise ConfiguredStagingOwnershipError("Failed to parse JSON payload") from exc


def capture_destination_baseline_evidence(
    operation_id: str,
    selected_backup_id: str,
    selected_backup_manifest_sha256: str,
    expected_application_commit: str,
    runtime_mode: str,
    target_set_hash: str,
    confirmation_value: str,
    targets: tuple[DatabaseTarget, ...],
) -> DestinationBaselineEvidence:
    """Capture durable destination baseline evidence from runtime configuration."""
    import importlib.metadata
    from datetime import datetime, timezone
    from operator_storage import active_user_target_mapping

    control_t = next((t for t in targets if t.kind == "control"), None)
    if control_t is not None and control_t.path.exists():
        try:
            active_mapping = dict(active_user_target_mapping(control_t.path))
        except Exception:
            active_mapping = {}
    else:
        active_mapping = {}

    project_root = safe_resolve(config.PROJECT_ROOT)
    ordered_keys = tuple(t.target_key for t in targets)
    target_records: list[TargetBaselineRecord] = []

    for idx, t in enumerate(targets):
        raw_p = t.path
        if has_symlink_component(raw_p) or raw_p.is_symlink():
            raise ConfiguredStagingError("Configured database target path contains symlinks")
        if not raw_p.exists():
            if t.required:
                raise ConfiguredStagingError("Required configured database target missing")
            raise ConfiguredStagingError("Configured database target path does not exist")

        resolved_p = safe_resolve(raw_p)
        if resolved_p.is_symlink() or not stat.S_ISREG(resolved_p.stat().st_mode):
            raise ConfiguredStagingError("Configured database target must be a regular file")

        try:
            cfg_rel = str(raw_p.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            cfg_rel = str(raw_p).replace("\\", "/")

        try:
            res_rel = str(resolved_p.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            res_rel = str(resolved_p).replace("\\", "/")

        t_st = resolved_p.stat()
        sha = _sha256_file(resolved_p)

        parent_p = resolved_p.parent
        if has_symlink_component(parent_p) or parent_p.is_symlink():
            raise ConfiguredStagingError("Parent directory contains symlinks")
        try:
            p_rel = str(parent_p.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            p_rel = str(parent_p).replace("\\", "/")

        p_st = parent_p.stat()

        # WAL
        wal_path = raw_p.with_name(raw_p.name + "-wal")
        if has_symlink_component(wal_path) or wal_path.is_symlink():
            raise ConfiguredStagingError("WAL sidecar path contains symlinks")
        if wal_path.exists():
            wal_res = safe_resolve(wal_path)
            w_st = wal_res.stat()
            if not stat.S_ISREG(w_st.st_mode):
                raise ConfiguredStagingError("WAL sidecar must be a regular file")
            try:
                w_cfg_rel = str(wal_path.relative_to(project_root)).replace("\\", "/")
            except ValueError:
                w_cfg_rel = str(wal_path).replace("\\", "/")
            try:
                w_res_rel = str(wal_res.relative_to(project_root)).replace("\\", "/")
            except ValueError:
                w_res_rel = str(wal_res).replace("\\", "/")
            wal_info = {
                "present": True,
                "configured_relative_path": w_cfg_rel,
                "resolved_relative_path": w_res_rel,
                "is_regular_file": True,
                "st_dev": w_st.st_dev,
                "st_ino": w_st.st_ino,
                "size_bytes": w_st.st_size,
                "mtime_ns": w_st.st_mtime_ns,
                "st_mode": stat.S_IMODE(w_st.st_mode),
                "sha256": _sha256_file(wal_res),
            }
        else:
            wal_info = {"present": False}

        # SHM
        shm_path = raw_p.with_name(raw_p.name + "-shm")
        if has_symlink_component(shm_path) or shm_path.is_symlink():
            raise ConfiguredStagingError("SHM sidecar path contains symlinks")
        if shm_path.exists():
            shm_res = safe_resolve(shm_path)
            s_st = shm_res.stat()
            if not stat.S_ISREG(s_st.st_mode):
                raise ConfiguredStagingError("SHM sidecar must be a regular file")
            try:
                s_cfg_rel = str(shm_path.relative_to(project_root)).replace("\\", "/")
            except ValueError:
                s_cfg_rel = str(shm_path).replace("\\", "/")
            try:
                s_res_rel = str(shm_res.relative_to(project_root)).replace("\\", "/")
            except ValueError:
                s_res_rel = str(shm_res).replace("\\", "/")
            shm_info = {
                "present": True,
                "configured_relative_path": s_cfg_rel,
                "resolved_relative_path": s_res_rel,
                "is_regular_file": True,
                "st_dev": s_st.st_dev,
                "st_ino": s_st.st_ino,
                "size_bytes": s_st.st_size,
                "mtime_ns": s_st.st_mtime_ns,
                "st_mode": stat.S_IMODE(s_st.st_mode),
                "sha256": _sha256_file(shm_res),
            }
        else:
            shm_info = {"present": False}

        target_records.append(
            TargetBaselineRecord(
                target_key=t.target_key,
                kind=t.kind,
                tenant_uuid=t.tenant_id,
                target_order=idx,
                configured_relative_path=cfg_rel,
                resolved_relative_path=res_rel,
                is_regular_file=True,
                st_dev=t_st.st_dev,
                st_ino=t_st.st_ino,
                size_bytes=t_st.st_size,
                mtime_ns=t_st.st_mtime_ns,
                st_mode=stat.S_IMODE(t_st.st_mode),
                sha256=sha,
                parent_relative_path=p_rel,
                parent_st_dev=p_st.st_dev,
                parent_st_ino=p_st.st_ino,
                parent_is_dir=stat.S_ISDIR(p_st.st_mode),
                parent_st_mode=stat.S_IMODE(p_st.st_mode),
                wal=wal_info,
                shm=shm_info,
            )
        )

    return DestinationBaselineEvidence(
        format_version=BASELINE_FORMAT,
        operation_id=operation_id,
        selected_backup_id=selected_backup_id,
        selected_backup_manifest_sha256=selected_backup_manifest_sha256,
        expected_application_commit=expected_application_commit,
        runtime_mode=runtime_mode,
        target_set_hash=target_set_hash,
        confirmation_value=confirmation_value,
        ordered_target_keys=ordered_keys,
        targets=tuple(target_records),
        active_control_user_mapping=active_mapping,
        garminconnect_version=importlib.metadata.version("garminconnect"),
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def _destination_baseline_payload(ev: DestinationBaselineEvidence) -> dict[str, Any]:
    return {
        "format_version": ev.format_version,
        "operation_id": ev.operation_id,
        "selected_backup_id": ev.selected_backup_id,
        "selected_backup_manifest_sha256": ev.selected_backup_manifest_sha256,
        "expected_application_commit": ev.expected_application_commit,
        "runtime_mode": ev.runtime_mode,
        "target_set_hash": ev.target_set_hash,
        "confirmation_value": ev.confirmation_value,
        "ordered_target_keys": list(ev.ordered_target_keys),
        "targets": [
            {
                "target_key": t.target_key,
                "kind": t.kind,
                "tenant_uuid": t.tenant_uuid,
                "target_order": t.target_order,
                "configured_relative_path": t.configured_relative_path,
                "resolved_relative_path": t.resolved_relative_path,
                "is_regular_file": t.is_regular_file,
                "st_dev": t.st_dev,
                "st_ino": t.st_ino,
                "size_bytes": t.size_bytes,
                "mtime_ns": t.mtime_ns,
                "st_mode": t.st_mode,
                "sha256": t.sha256,
                "parent_relative_path": t.parent_relative_path,
                "parent_st_dev": t.parent_st_dev,
                "parent_st_ino": t.parent_st_ino,
                "parent_is_dir": t.parent_is_dir,
                "parent_st_mode": t.parent_st_mode,
                "wal": t.wal,
                "shm": t.shm,
            }
            for t in ev.targets
        ],
        "active_control_user_mapping": ev.active_control_user_mapping,
        "garminconnect_version": ev.garminconnect_version,
        "captured_at": ev.captured_at,
    }


def write_destination_baseline_evidence(
    operation_id: str,
    evidence: DestinationBaselineEvidence,
    *,
    restore_root: Path | str | None = None,
) -> str:
    root = validate_restore_root(restore_root)
    op_dir = root / f"operation-{operation_id}"
    if not op_dir.exists() or op_dir.is_symlink():
        raise ConfiguredStagingPersistenceError("Operation directory missing or unsafe")

    payload = _destination_baseline_payload(evidence)
    canonical_bytes = canonical_json(payload)
    sha256_hex = hashlib.sha256(canonical_bytes).hexdigest()

    final_file = op_dir / _BASELINE_FILENAME
    partial_file = op_dir / f".{_BASELINE_FILENAME}.partial"

    if final_file.exists() or final_file.is_symlink():
        raise ConfiguredStagingPersistenceError("Destination baseline file already exists")

    if partial_file.exists() or partial_file.is_symlink():
        raise ConfiguredStagingPersistenceError("Destination baseline partial file already exists")

    try:
        fd = os.open(str(partial_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG, 0o600)
        with os.fdopen(fd, "wb") as h:
            h.write(canonical_bytes)
            h.flush()
            os.fsync(h.fileno())
    except OSError as exc:
        raise ConfiguredStagingPersistenceError("Failed to write destination baseline partial file") from exc

    publish_noreplace(partial_file, final_file)

    reread_bytes = final_file.read_bytes()
    if reread_bytes != canonical_bytes or hashlib.sha256(reread_bytes).hexdigest() != sha256_hex:
        raise ConfiguredStagingPersistenceError("Destination baseline verification failed after write")

    return sha256_hex


def _destination_baseline_from_payload(payload: dict[str, Any]) -> DestinationBaselineEvidence:
    expected_keys = {
        "format_version", "operation_id", "selected_backup_id", "selected_backup_manifest_sha256",
        "expected_application_commit", "runtime_mode", "target_set_hash", "confirmation_value",
        "ordered_target_keys", "targets", "active_control_user_mapping", "garminconnect_version", "captured_at"
    }
    if set(payload.keys()) != expected_keys:
        raise ConfiguredStagingOwnershipError("Destination baseline JSON payload schema is invalid")

    if payload["format_version"] != BASELINE_FORMAT:
        raise ConfiguredStagingOwnershipError("Destination baseline format version invalid")

    targets: list[TargetBaselineRecord] = []
    for item in payload["targets"]:
        targets.append(
            TargetBaselineRecord(
                target_key=item["target_key"],
                kind=item["kind"],
                tenant_uuid=item["tenant_uuid"],
                target_order=item["target_order"],
                configured_relative_path=item["configured_relative_path"],
                resolved_relative_path=item["resolved_relative_path"],
                is_regular_file=item["is_regular_file"],
                st_dev=item["st_dev"],
                st_ino=item["st_ino"],
                size_bytes=item["size_bytes"],
                mtime_ns=item["mtime_ns"],
                st_mode=item["st_mode"],
                sha256=item["sha256"],
                parent_relative_path=item["parent_relative_path"],
                parent_st_dev=item["parent_st_dev"],
                parent_st_ino=item["parent_st_ino"],
                parent_is_dir=item["parent_is_dir"],
                parent_st_mode=item["parent_st_mode"],
                wal=dict(item["wal"]),
                shm=dict(item["shm"]),
            )
        )

    return DestinationBaselineEvidence(
        format_version=payload["format_version"],
        operation_id=payload["operation_id"],
        selected_backup_id=payload["selected_backup_id"],
        selected_backup_manifest_sha256=payload["selected_backup_manifest_sha256"],
        expected_application_commit=payload["expected_application_commit"],
        runtime_mode=payload["runtime_mode"],
        target_set_hash=payload["target_set_hash"],
        confirmation_value=payload["confirmation_value"],
        ordered_target_keys=tuple(payload["ordered_target_keys"]),
        targets=tuple(targets),
        active_control_user_mapping=dict(payload["active_control_user_mapping"]),
        garminconnect_version=payload["garminconnect_version"],
        captured_at=payload["captured_at"],
    )


def load_destination_baseline_evidence(
    operation_id: str,
    *,
    restore_root: Path | str | None = None,
) -> tuple[DestinationBaselineEvidence, str]:
    root = validate_restore_root(restore_root)
    op_dir = root / f"operation-{operation_id}"
    final_file = op_dir / _BASELINE_FILENAME

    if not final_file.exists() or final_file.is_symlink() or not stat.S_ISREG(final_file.stat().st_mode):
        raise ConfiguredStagingPersistenceError("Destination baseline file missing or unsafe")

    raw_bytes = final_file.read_bytes()
    if len(raw_bytes) > MAX_BASELINE_BYTES:
        raise ConfiguredStagingPersistenceError("Destination baseline file exceeds size limit")

    parsed = _strict_json_loads(raw_bytes)
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

    evidence = _destination_baseline_from_payload(parsed)
    if evidence.operation_id != operation_id:
        raise ConfiguredStagingPersistenceError("Destination baseline operation ID mismatch")

    return evidence, sha256_hex


def revalidate_destination_baseline_evidence(
    evidence: DestinationBaselineEvidence,
    targets: tuple[DatabaseTarget, ...],
    expected_application_commit: str,
) -> None:
    """Strictly revalidate current runtime environment against persisted baseline evidence."""
    import importlib.metadata
    from operator_storage import active_user_target_mapping

    if evidence.expected_application_commit != expected_application_commit:
        raise ConfiguredRestoreError("Application commit mismatch against persisted baseline")

    curr_gc_ver = importlib.metadata.version("garminconnect")
    if evidence.garminconnect_version != curr_gc_ver:
        raise ConfiguredRestoreError("garminconnect package version mismatch against persisted baseline")

    control_t = next((t for t in targets if t.kind == "control"), None)
    if control_t is not None and control_t.path.exists():
        try:
            curr_mapping = dict(active_user_target_mapping(control_t.path))
        except Exception:
            curr_mapping = {}
    else:
        curr_mapping = {}

    if evidence.active_control_user_mapping != curr_mapping:
        raise ConfiguredRestoreError("Active control-user mapping mismatch against persisted baseline")

    curr_keys = tuple(t.target_key for t in targets)
    if evidence.ordered_target_keys != curr_keys:
        raise ConfiguredRestoreError("Ordered target keys mismatch against persisted baseline")

    if len(evidence.targets) != len(targets):
        raise ConfiguredRestoreError("Target count mismatch against persisted baseline")

    fresh_ev = capture_destination_baseline_evidence(
        operation_id=evidence.operation_id,
        selected_backup_id=evidence.selected_backup_id,
        selected_backup_manifest_sha256=evidence.selected_backup_manifest_sha256,
        expected_application_commit=expected_application_commit,
        runtime_mode=evidence.runtime_mode,
        target_set_hash=evidence.target_set_hash,
        confirmation_value=evidence.confirmation_value,
        targets=targets,
    )

    for stored_t, fresh_t in zip(evidence.targets, fresh_ev.targets, strict=True):
        if stored_t != fresh_t:
            raise ConfiguredRestoreError(
                f"Destination baseline revalidation failed for target '{stored_t.target_key}': drift detected"
            )


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

        publish_noreplace(temp_path, binding_path)

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

    # Allowed children check inside stage_dir (EXACT match required, no partial files permitted)
    allowed_children = {_STAGING_BINDING_NAME} | expected_staged_names
    for child in stage_dir.iterdir():
        c_name = child.name
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

            if staged_path.exists():
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

                    publish_noreplace(partial_path, staged_path)
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
