"""Fixture-only guarded replacement and automatic rollback (Phase 6B2C).

This module is deliberately not a restore engine for GarminCoach's configured
databases.  Its public entry point accepts only paths below a private temporary
fixture root, and checks that boundary again immediately before each rename.
It composes the completed 6B2B staging proof; it never discovers application
targets, starts services, acquires application locks, or accepts arbitrary
SQLite paths.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import config
from guarded_restore import (
    RestoreJournalError, RestoreJournalPersistenceError, RestoreStage,
    TargetRestoreState, load_restore_journal, update_restore_journal,
)
from guarded_restore_staging import (
    FinalReadinessError, StagedArtifact, StagingResult, SyntheticRestoreTarget,
    _BINARY_FLAG, _NOFOLLOW_FLAG, _entry_tuple, _inside, _private,
    _ready_file_record, _staged_filename, _validate_fixture_root, _verify,
)
from operator_storage import has_symlink_component, permission_health
from verified_backup import ValidatedBackupSnapshot, load_validated_backup_snapshot


class SyntheticReplacementError(RuntimeError):
    """Base class whose public text is intentionally bounded."""


class ReplacementPreconditionError(SyntheticReplacementError): pass
class ReplacementPersistenceError(SyntheticReplacementError): pass
class ReplacementPostcheckError(SyntheticReplacementError): pass
class RollbackCompletedError(SyntheticReplacementError): pass
class ManualRecoveryRequiredError(SyntheticReplacementError): pass
class EvidenceCleanupError(SyntheticReplacementError): pass


@dataclass(frozen=True)
class SyntheticReplacementResult:
    operation_id: str
    final_stage: RestoreStage
    replaced_target_keys: tuple[str, ...]
    rollback_occurred: bool
    final_result: str


_BINDING = ".rollback-binding.json"
_MAX_BINDING = 64 * 1024


def _safe_fail(operation_id: str, journal_root: Path) -> None:
    """Best-effort safe terminalization before the first possible rename."""
    try:
        journal = load_restore_journal(operation_id, root=journal_root)
        if journal.stage in {RestoreStage.REPLACEMENT_READY, RestoreStage.RESTORE_STAGED,
                             RestoreStage.STAGED_VERIFIED, RestoreStage.CURRENT_SNAPSHOT_CREATED,
                             RestoreStage.VERIFIED, RestoreStage.PRECHECK}:
            update_restore_journal(operation_id, root=journal_root, stage=RestoreStage.FAILED_SAFE)
    except (RestoreJournalError, RestoreJournalPersistenceError):
        pass


def _snapshot(value: ValidatedBackupSnapshot, *, label: str) -> ValidatedBackupSnapshot:
    if type(value) is not ValidatedBackupSnapshot:
        raise ReplacementPreconditionError(f"{label} backup is invalid")
    try:
        fresh = load_validated_backup_snapshot(value.directory)
    except Exception as exc:
        raise ReplacementPreconditionError(f"{label} backup is invalid") from exc
    if fresh != value:
        raise ReplacementPreconditionError(f"{label} backup changed")
    return fresh


def _configured(path: Path) -> bool:
    try:
        candidate = path.resolve(strict=False)
        protected = (Path(config.CONTROL_DB_PATH).resolve(), Path(config.DB_PATH).resolve(),
                     Path(config.MULTI_USER_DATA_ROOT).resolve(), Path(config.OPERATOR_BACKUP_ROOT).resolve(),
                     Path(config.OPERATOR_RESTORE_ROOT).resolve())
        return any(candidate == item or candidate in item.parents or item in candidate.parents for item in protected)
    except OSError:
        return True


def _read_record(path: Path, *, size: int | None = None, digest: str | None = None):
    try:
        return _ready_file_record(path, expected_size=size, expected_sha256=digest)
    except OSError as exc:
        raise ReplacementPreconditionError("Synthetic replacement file is unsafe") from exc


def _same_device(left: Path, right: Path) -> bool:
    try:
        return os.lstat(left).st_dev == os.lstat(right).st_dev
    except OSError:
        return False


def _rollback_directory(target: SyntheticRestoreTarget, operation_id: str, index: int) -> Path:
    return target.path.parent / f".garmincoach-restore-rollback-{operation_id}-{index:03d}"


def _rollback_name(entry: tuple, index: int) -> str:
    return _staged_filename(entry, index).replace(".sqlite.staged", ".sqlite.rollback")


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _copy_private(source: Path, destination: Path, *, size: int, digest: str) -> None:
    """Copy exact bytes through no-follow descriptors, then atomically publish."""
    partial = destination.with_name("." + destination.name + ".partial")
    source_fd = output_fd = None
    try:
        source_fd = os.open(str(source), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise OSError("source")
        output_fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG, 0o600)
        h = hashlib.sha256(); copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            h.update(chunk); copied += len(chunk); offset = 0
            while offset < len(chunk):
                written = os.write(output_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("write")
                offset += written
        os.fsync(output_fd)
        after = os.fstat(source_fd)
        if (before.st_dev, before.st_ino, before.st_size, getattr(before, "st_mtime_ns", None)) != (after.st_dev, after.st_ino, after.st_size, getattr(after, "st_mtime_ns", None)) or copied != size or h.hexdigest() != digest:
            raise OSError("source changed")
        os.close(source_fd); source_fd = None; os.close(output_fd); output_fd = None
        if destination.exists() or destination.is_symlink() or partial.is_symlink():
            raise OSError("publication")
        os.replace(partial, destination); _private(destination)
        _read_record(destination, size=size, digest=digest)
        _fsync(destination); _fsync(destination.parent, directory=True)
    except (OSError, ReplacementPreconditionError) as exc:
        raise ReplacementPersistenceError("Rollback staging could not be persisted") from exc
    finally:
        for descriptor in (source_fd, output_fd):
            if descriptor is not None:
                try: os.close(descriptor)
                except OSError: pass
        try:
            if partial.exists(): partial.unlink()
        except OSError:
            pass


def _fsync(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | _NOFOLLOW_FLAG | (getattr(os, "O_DIRECTORY", 0) if directory else _BINARY_FLAG)
    try:
        fd = os.open(str(path), flags)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError as exc:
        raise ReplacementPersistenceError("Synthetic replacement could not be persisted") from exc


def _write_binding(directory: Path, *, operation_id: str, safety: ValidatedBackupSnapshot, entry: tuple, index: int) -> None:
    payload = {"format_version": "garmincoach-rollback-binding-v1", "operation_id": operation_id,
               "safety_backup_id": safety.backup_id, "safety_backup_manifest_sha256": safety.manifest_sha256,
               "target_key": entry[0], "kind": entry[1], "target_order": index,
               "rollback_filename": _rollback_name(entry, index), "size_bytes": entry[4], "sha256": entry[5]}
    data = _canonical(payload); final = directory / _BINDING; partial = directory / ("." + _BINDING + ".partial")
    if len(data) > _MAX_BINDING or final.exists() or partial.exists():
        raise ReplacementPersistenceError("Rollback staging binding is unsafe")
    fd = None
    try:
        fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG, 0o600)
        os.write(fd, data); os.fsync(fd); os.close(fd); fd = None
        os.replace(partial, final); _private(final); _read_record(final, size=len(data), digest=hashlib.sha256(data).hexdigest()); _fsync(directory, directory=True)
    except (OSError, ReplacementPreconditionError) as exc:
        raise ReplacementPersistenceError("Rollback staging binding could not be persisted") from exc
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass


def _verify_binding(directory: Path, *, operation_id: str, safety: ValidatedBackupSnapshot, entry: tuple, index: int) -> Path:
    binding = directory / _BINDING
    try:
        record = _read_record(binding)
        if record.size > _MAX_BINDING:
            raise OSError("size")
        data = binding.read_bytes(); parsed = json.loads(data.decode("utf-8"))
        expected = {"format_version": "garmincoach-rollback-binding-v1", "operation_id": operation_id,
                    "safety_backup_id": safety.backup_id, "safety_backup_manifest_sha256": safety.manifest_sha256,
                    "target_key": entry[0], "kind": entry[1], "target_order": index,
                    "rollback_filename": _rollback_name(entry, index), "size_bytes": entry[4], "sha256": entry[5]}
        if _canonical(parsed) != data or parsed != expected:
            raise OSError("binding")
        artifact = directory / _rollback_name(entry, index)
        if not _same_device(directory, artifact):
            raise OSError("filesystem")
        _read_record(artifact, size=entry[4], digest=entry[5]); _verify(entry, artifact)
        return artifact
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, FinalReadinessError) as exc:
        raise ReplacementPreconditionError("Rollback staging evidence is invalid") from exc


def _prepare_rollbacks(*, operation_id: str, safety: ValidatedBackupSnapshot, destinations: tuple[SyntheticRestoreTarget, ...], fixture_root: Path) -> tuple[Path, ...]:
    entries = tuple(_entry_tuple(entry) for entry in safety.entries)
    result = []
    for index, (entry, target) in enumerate(zip(entries, destinations)):
        directory = _rollback_directory(target, operation_id, index)
        try:
            # The rollback directory is constructed directly below the already
            # preflighted destination parent, so it is necessarily on the
            # destination filesystem.  Comparing a Windows file's ``st_dev``
            # to a directory's is not a portable additional proof.
            directory.mkdir(mode=0o700); _private(directory, directory=True)
            _write_binding(directory, operation_id=operation_id, safety=safety, entry=entry, index=index)
            source = safety.directory / entry[3]
            _copy_private(source, directory / _rollback_name(entry, index), size=entry[4], digest=entry[5])
            result.append(_verify_binding(directory, operation_id=operation_id, safety=safety, entry=entry, index=index))
        except SyntheticReplacementError:
            raise
        except OSError as exc:
            raise ReplacementPersistenceError("Rollback staging could not be persisted") from exc
    return tuple(result)


def _preflight(*, operation_id: str, selected: ValidatedBackupSnapshot, safety: ValidatedBackupSnapshot,
               destinations: tuple[SyntheticRestoreTarget, ...], staging_result: StagingResult,
               fixture_root: Path, journal_root: Path):
    try:
        journal = load_restore_journal(operation_id, root=journal_root)
        if journal.stage is not RestoreStage.REPLACEMENT_READY or journal.operation_id != operation_id or journal.safety_backup_id != safety.backup_id:
            raise ValueError("journal")
        if (journal.selected_backup_id, journal.selected_backup_manifest_sha256, journal.runtime_mode, journal.target_keys) != (selected.backup_id, selected.manifest_sha256, selected.runtime_mode, selected.target_keys):
            raise ValueError("selected")
        if selected.backup_id == safety.backup_id or selected.runtime_mode != safety.runtime_mode or selected.target_keys != safety.target_keys:
            raise ValueError("backup set")
        root = _validate_fixture_root(Path(fixture_root), selected.directory)
        if _configured(root) or len(destinations) != len(journal.target_keys) or tuple(target.target_key for target in destinations) != journal.target_keys:
            raise ValueError("targets")
        if len({target.path.resolve(strict=False) for target in destinations}) != len(destinations):
            raise ValueError("duplicates")
        selected_entries, safety_entries = tuple(_entry_tuple(x) for x in selected.entries), tuple(_entry_tuple(x) for x in safety.entries)
        if tuple((x[0], x[1]) for x in selected_entries) != tuple((x[0], x[1]) for x in safety_entries):
            raise ValueError("mapping")
        if type(staging_result) is not StagingResult or staging_result.operation_id != operation_id or len(staging_result.artifacts) != len(destinations):
            raise ValueError("staging")
        for index, (entry, target, artifact) in enumerate(zip(selected_entries, destinations, staging_result.artifacts)):
            if (target.target_key, target.kind) != entry[:2] or _configured(target.path) or not _inside(target.path, root) or has_symlink_component(target.path) or not target.path.is_file():
                raise ValueError("destination")
            if os.name != "nt" and permission_health(target.path.parent, directory=True) != "private":
                raise ValueError("parent")
            expected = target.path.parent / f".garmincoach-restore-stage-{operation_id}" / _staged_filename(entry, index)
            if not isinstance(artifact, StagedArtifact) or (artifact.operation_id, artifact.target_key, artifact.kind, artifact.target_order, artifact.path, artifact.size_bytes, artifact.sha256) != (operation_id, entry[0], entry[1], index, expected, entry[4], entry[5]) or artifact.destination_size_bytes < 0 or len(artifact.destination_sha256) != 64:
                raise ValueError("artifact")
            _read_record(target.path, size=artifact.destination_size_bytes, digest=artifact.destination_sha256); _read_record(expected, size=entry[4], digest=entry[5]); _verify(entry, expected)
            if not _same_device(target.path.parent, expected):
                raise ValueError("filesystem")
        return journal, root, selected_entries, safety_entries
    except SyntheticReplacementError:
        raise
    except Exception as exc:
        raise ReplacementPreconditionError("Synthetic replacement preflight failed") from exc


def _sidecars(destination: Path) -> None:
    for suffix in ("-wal", "-shm"):
        item = Path(str(destination) + suffix)
        try:
            state = os.lstat(item)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReplacementPersistenceError("Synthetic sidecar handling failed") from exc
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
            raise ReplacementPreconditionError("Synthetic sidecar is unsafe")
        try:
            os.unlink(item)
        except OSError as exc:
            raise ReplacementPersistenceError("Synthetic sidecar handling failed") from exc


def _transition(operation_id: str, journal_root: Path, stage: RestoreStage | None = None,
                target_key: str | None = None, target_state: TargetRestoreState | None = None):
    try:
        updated = update_restore_journal(operation_id, root=journal_root, stage=stage, target_key=target_key, target_state=target_state)
        reread = load_restore_journal(operation_id, root=journal_root)
        if reread != updated:
            raise RestoreJournalPersistenceError("reread")
        return reread
    except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
        raise ReplacementPersistenceError("Synthetic replacement journal could not be persisted") from exc


def _rollback(*, operation_id: str, journal_root: Path, safety: ValidatedBackupSnapshot,
              destinations: tuple[SyntheticRestoreTarget, ...], entries: tuple, replaced: list[str]) -> None:
    try:
        journal = load_restore_journal(operation_id, root=journal_root)
        if journal.stage is not RestoreStage.ROLLBACK_REQUIRED:
            _transition(operation_id, journal_root, stage=RestoreStage.ROLLBACK_REQUIRED)
    except SyntheticReplacementError:
        raise
    except Exception as exc:
        raise ManualRecoveryRequiredError("Manual recovery is required") from exc
    try:
        for index in reversed(range(len(destinations))):
            target, entry = destinations[index], entries[index]
            if target.target_key not in replaced:
                continue
            artifact = _verify_binding(_rollback_directory(target, operation_id, index), operation_id=operation_id, safety=safety, entry=entry, index=index)
            _sidecars(target.path)
            if not _same_device(target.path.parent, artifact):
                raise ReplacementPreconditionError("Rollback filesystem is unsafe")
            os.replace(artifact, target.path); _private(target.path)
            _read_record(target.path, size=entry[4], digest=entry[5]); _verify(entry, target.path); _fsync(target.path); _fsync(target.path.parent, directory=True)
            _transition(operation_id, journal_root, target_key=target.target_key, target_state=TargetRestoreState.ROLLED_BACK)
        _transition(operation_id, journal_root, stage=RestoreStage.ROLLED_BACK)
        _transition(operation_id, journal_root, stage=RestoreStage.FAILED_SAFE)
    except Exception as exc:
        try:
            journal = load_restore_journal(operation_id, root=journal_root)
            if journal.stage is RestoreStage.ROLLBACK_REQUIRED:
                _transition(operation_id, journal_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED)
        except Exception:
            pass
        raise ManualRecoveryRequiredError("Manual recovery is required") from exc


def _cleanup(operation_id: str, destinations: tuple[SyntheticRestoreTarget, ...]) -> None:
    try:
        for index, target in enumerate(destinations):
            for name in (f".garmincoach-restore-stage-{operation_id}", f".garmincoach-restore-rollback-{operation_id}-{index:03d}"):
                directory = target.path.parent / name
                if directory.exists() and directory.is_dir() and not directory.is_symlink():
                    shutil.rmtree(directory)
    except OSError as exc:
        raise EvidenceCleanupError("Operation evidence requires cleanup") from exc


def replace_and_verify_synthetic_restore(*, operation_id: str, selected_backup: ValidatedBackupSnapshot,
                                         safety_backup: ValidatedBackupSnapshot,
                                         destinations: tuple[SyntheticRestoreTarget, ...], staging_result: StagingResult,
                                         fixture_root: Path, journal_root: Path) -> SyntheticReplacementResult:
    """Replace only prepared fixture DBs; automatically roll back a partial run.

    Exceptions deliberately provide no path, SQLite, token, or operating-system
    detail.  A completed rollback raises :class:`RollbackCompletedError`, so a
    caller cannot mistake a recovered failure for a successful restore.
    """
    try:
        selected, safety = _snapshot(selected_backup, label="Selected"), _snapshot(safety_backup, label="Safety")
        journal, root, selected_entries, safety_entries = _preflight(operation_id=operation_id, selected=selected, safety=safety, destinations=destinations, staging_result=staging_result, fixture_root=fixture_root, journal_root=journal_root)
        rollback_artifacts = _prepare_rollbacks(operation_id=operation_id, safety=safety, destinations=destinations, fixture_root=root)
        # Whole-operation final read-only barrier: every source, staged, rollback, and destination is rebound.
        for index, target in enumerate(destinations):
            _read_record(target.path); _read_record(staging_result.artifacts[index].path, size=selected_entries[index][4], digest=selected_entries[index][5]); _verify(selected_entries[index], staging_result.artifacts[index].path)
            _verify_binding(_rollback_directory(target, operation_id, index), operation_id=operation_id, safety=safety, entry=safety_entries[index], index=index)
        if load_restore_journal(operation_id, root=journal_root) != journal:
            raise ReplacementPreconditionError("Synthetic replacement journal changed")
        _transition(operation_id, journal_root, stage=RestoreStage.REPLACING)
    except SyntheticReplacementError:
        _safe_fail(operation_id, journal_root)
        raise
    except Exception as exc:
        _safe_fail(operation_id, journal_root)
        raise ReplacementPreconditionError("Synthetic replacement preflight failed") from exc

    replaced: list[str] = []
    ordered = tuple(index for index, target in enumerate(destinations) if target.kind in {"tenant", "single_user"}) + tuple(index for index, target in enumerate(destinations) if target.kind == "control")
    try:
        if len([target for target in destinations if target.kind == "control"]) != 1:
            raise ReplacementPreconditionError("Synthetic replacement target set is invalid")
        for index in ordered:
            target, entry, artifact = destinations[index], selected_entries[index], staging_result.artifacts[index]
            journal = load_restore_journal(operation_id, root=journal_root)
            if journal.stage is not RestoreStage.REPLACING or journal.targets[index].state is not TargetRestoreState.STAGED_VERIFIED:
                raise ReplacementPreconditionError("Synthetic replacement journal is invalid")
            _read_record(target.path); _read_record(artifact.path, size=entry[4], digest=entry[5]); _verify(entry, artifact.path); _sidecars(target.path)
            if not _same_device(target.path.parent, artifact.path):
                raise ReplacementPreconditionError("Synthetic replacement filesystem is unsafe")
            os.replace(artifact.path, target.path); _private(target.path)
            replaced.append(target.target_key)
            _read_record(target.path, size=entry[4], digest=entry[5]); _verify(entry, target.path); _fsync(target.path); _fsync(target.path.parent, directory=True)
            _transition(operation_id, journal_root, target_key=target.target_key, target_state=TargetRestoreState.REPLACED)
        _transition(operation_id, journal_root, stage=RestoreStage.REPLACED)
        for target, entry in zip(destinations, selected_entries):
            _read_record(target.path, size=entry[4], digest=entry[5]); _verify(entry, target.path)
            if Path(str(target.path) + "-wal").exists() or Path(str(target.path) + "-shm").exists():
                raise ReplacementPostcheckError("Synthetic replacement postcheck failed")
        _snapshot(selected, label="Selected"); _snapshot(safety, label="Safety")
        _transition(operation_id, journal_root, stage=RestoreStage.POSTCHECK_PASSED)
        completed = _transition(operation_id, journal_root, stage=RestoreStage.COMPLETED)
        _cleanup(operation_id, destinations)
        return SyntheticReplacementResult(operation_id, completed.stage, tuple(replaced), False, "COMPLETED")
    except Exception as cause:
        try:
            _rollback(operation_id=operation_id, journal_root=journal_root, safety=safety, destinations=destinations, entries=safety_entries, replaced=replaced)
        except ManualRecoveryRequiredError:
            raise
        raise RollbackCompletedError("Synthetic replacement was rolled back") from cause
