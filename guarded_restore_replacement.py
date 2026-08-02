"""Fixture-only guarded replacement and automatic rollback (Phase 6B2C).

This module is deliberately not a restore engine for GarminCoach's configured
databases. Its public entry point accepts only paths below a private temporary
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
import stat

import config
from guarded_restore import (
    RestoreJournal, RestoreJournalError, RestoreJournalPersistenceError, RestoreStage,
    TargetJournalFact, TargetRestoreState, load_restore_journal, update_restore_journal,
)
from guarded_restore_staging import (
    FinalReadinessError, StagedArtifact, StagingResult, SyntheticRestoreTarget,
    _CleanupBindingRecord, _DestinationBaseline, _ReadyDirectoryRecord,
    _BINARY_FLAG, _MAX_STAGING_BINDING_BYTES, _NOFOLLOW_FLAG,
    _STAGING_BINDING_NAME, _canonical_json, _cleanup_directory_record,
    _cleanup_file_record, _directory_identity, _entry_tuple, _inside,
    _open_verified_directory, _private, _read_exact, _ready_file_record,
    _revalidate_cleanup_directory, _revalidate_destination_baselines,
    _same_directory_identity, _same_ready_file, _staged_filename,
    _validate_fixture_root, _verify,
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
        raise ReplacementPreconditionError("Synthetic replacement preflight failed")
    try:
        fresh = load_validated_backup_snapshot(value.directory)
    except Exception as exc:
        raise ReplacementPreconditionError("Synthetic replacement preflight failed") from exc
    if fresh != value:
        raise ReplacementPreconditionError("Synthetic replacement preflight failed")
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


def _fsync(path: Path, *, directory: bool = False) -> None:
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
        raise ReplacementPersistenceError("Synthetic replacement could not be persisted") from exc


def _transition(
    operation_id: str,
    journal_root: Path,
    stage: RestoreStage | None = None,
    target_key: str | None = None,
    target_state: TargetRestoreState | None = None,
    wal_present: bool | None = None,
    shm_present: bool | None = None,
    wal_removed: bool | None = None,
    shm_removed: bool | None = None,
    replacement_intent: bool | None = None,
    replacement_completed: bool | None = None,
    rollback_intent: bool | None = None,
    rollback_completed: bool | None = None,
) -> RestoreJournal:
    try:
        updated = update_restore_journal(
            operation_id,
            root=journal_root,
            stage=stage,
            target_key=target_key,
            target_state=target_state,
            wal_present=wal_present,
            shm_present=shm_present,
            wal_removed=wal_removed,
            shm_removed=shm_removed,
            replacement_intent=replacement_intent,
            replacement_completed=replacement_completed,
            rollback_intent=rollback_intent,
            rollback_completed=rollback_completed,
        )
        reread = load_restore_journal(operation_id, root=journal_root)
        if reread != updated:
            raise RestoreJournalPersistenceError("Journal reread mismatch")
        return reread
    except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
        raise ReplacementPersistenceError("Synthetic replacement journal could not be persisted") from exc


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
        h = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            copied += len(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(output_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("write")
                offset += written
        os.fsync(output_fd)
        after = os.fstat(source_fd)
        if (
            (before.st_dev, before.st_ino, before.st_size, getattr(before, "st_mtime_ns", None))
            != (after.st_dev, after.st_ino, after.st_size, getattr(after, "st_mtime_ns", None))
            or copied != size
            or h.hexdigest() != digest
        ):
            raise OSError("source changed")
        os.close(source_fd)
        source_fd = None
        os.close(output_fd)
        output_fd = None
        if destination.exists() or destination.is_symlink() or partial.is_symlink():
            raise OSError("publication")
        os.replace(partial, destination)
        _private(destination)
        _read_record(destination, size=size, digest=digest)
        _fsync(destination)
        _fsync(destination.parent, directory=True)
    except (OSError, ReplacementPreconditionError) as exc:
        raise ReplacementPersistenceError("Rollback staging could not be persisted") from exc
    finally:
        for descriptor in (source_fd, output_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass


def _write_binding(directory: Path, *, operation_id: str, safety: ValidatedBackupSnapshot, entry: tuple, index: int) -> None:
    """Write rollback binding using a complete write loop and descriptor verification."""
    payload = {
        "format_version": "garmincoach-rollback-binding-v1",
        "operation_id": operation_id,
        "safety_backup_id": safety.backup_id,
        "safety_backup_manifest_sha256": safety.manifest_sha256,
        "target_key": entry[0],
        "kind": entry[1],
        "target_order": index,
        "rollback_filename": _rollback_name(entry, index),
        "size_bytes": entry[4],
        "sha256": entry[5],
    }
    data = _canonical_json(payload)
    final = directory / _BINDING
    partial = directory / ("." + _BINDING + ".partial")

    try:
        if len(data) > _MAX_BINDING:
            raise OSError("binding too large")
        try:
            if os.lstat(final):
                raise OSError("final binding exists")
        except FileNotFoundError:
            pass
        try:
            if os.lstat(partial):
                raise OSError("partial binding exists")
        except FileNotFoundError:
            pass

        fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _BINARY_FLAG, 0o600)
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("write failed")
                offset += written
            os.fsync(fd)
            before_stat = os.fstat(fd)
            if before_stat.st_size != len(data):
                raise OSError("stat size mismatch")
        finally:
            os.close(fd)

        try:
            if os.lstat(final):
                raise OSError("final binding exists before replace")
        except FileNotFoundError:
            pass

        os.replace(partial, final)
        _private(final)

        read_fd = os.open(str(final), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        try:
            read_stat = os.fstat(read_fd)
            path_stat = os.lstat(final)
            if (
                (read_stat.st_dev, read_stat.st_ino, read_stat.st_size)
                != (path_stat.st_dev, path_stat.st_ino, path_stat.st_size)
            ):
                raise OSError("descriptor path mismatch")
            data_read = _read_exact(read_fd, len(data))
            if data_read != data:
                raise OSError("verification data mismatch")
            if os.name != "nt":
                os.fsync(read_fd)
        finally:
            os.close(read_fd)

        _fsync(directory, directory=True)
    except (OSError, ReplacementPreconditionError) as exc:
        raise ReplacementPersistenceError("Rollback staging binding could not be persisted") from exc


def _verify_binding(directory: Path, *, operation_id: str, safety: ValidatedBackupSnapshot, entry: tuple, index: int) -> Path:
    """Read and verify rollback binding using strict descriptor bounds."""
    binding = directory / _BINDING
    try:
        if binding.is_symlink() or has_symlink_component(binding):
            raise OSError("symlink binding")
        fd = os.open(str(binding), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        try:
            before = os.fstat(fd)
            path_before = os.lstat(binding)
            if (before.st_dev, before.st_ino, before.st_size) != (path_before.st_dev, path_before.st_ino, path_before.st_size):
                raise OSError("identity mismatch")
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BINDING:
                raise OSError("invalid binding size")
            data = _read_exact(fd, before.st_size)
            after = os.fstat(fd)
            path_after = os.lstat(binding)
            if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or (before.st_dev, before.st_ino, before.st_size) != (path_after.st_dev, path_after.st_ino, path_after.st_size):
                raise OSError("modified during read")
        finally:
            os.close(fd)

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            res: dict[str, object] = {}
            for k, v in pairs:
                if k in res:
                    raise ValueError
                res[k] = v
            return res

        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
        expected = {
            "format_version": "garmincoach-rollback-binding-v1",
            "operation_id": operation_id,
            "safety_backup_id": safety.backup_id,
            "safety_backup_manifest_sha256": safety.manifest_sha256,
            "target_key": entry[0],
            "kind": entry[1],
            "target_order": index,
            "rollback_filename": _rollback_name(entry, index),
            "size_bytes": entry[4],
            "sha256": entry[5],
        }
        if _canonical_json(parsed) != data or parsed != expected:
            raise OSError("binding content mismatch")

        artifact = directory / _rollback_name(entry, index)
        if not _same_device(directory, artifact):
            raise OSError("device mismatch")
        _read_record(artifact, size=entry[4], digest=entry[5])
        _verify(entry, artifact)
        return artifact
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, FinalReadinessError) as exc:
        raise ReplacementPreconditionError("Rollback staging evidence is invalid") from exc


def _prepare_rollbacks(*, operation_id: str, safety: ValidatedBackupSnapshot, destinations: tuple[SyntheticRestoreTarget, ...], fixture_root: Path) -> tuple[Path, ...]:
    entries = tuple(_entry_tuple(entry) for entry in safety.entries)
    result = []
    for index, (entry, target) in enumerate(zip(entries, destinations)):
        directory = _rollback_directory(target, operation_id, index)
        try:
            directory.mkdir(mode=0o700)
            _private(directory, directory=True)
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
            _read_record(target.path, size=artifact.destination_size_bytes, digest=artifact.destination_sha256)
            _read_record(expected, size=entry[4], digest=entry[5])
            _verify(entry, expected)
            if not _same_device(target.path.parent, expected):
                raise ValueError("filesystem")
        return journal, root, selected_entries, safety_entries
    except SyntheticReplacementError:
        raise
    except Exception as exc:
        raise ReplacementPreconditionError("Synthetic replacement preflight failed") from exc


def _handle_durable_sidecars(destination: Path, journal: RestoreJournal, target_key: str, journal_root: Path) -> RestoreJournal:
    """Perform sidecar handling according to the durable sidecar protocol."""
    fact = next((f for f in journal.targets if f.target_key == target_key), None)
    if fact is None:
        raise ReplacementPreconditionError("Target fact missing")

    current_journal = journal
    for suffix in ("-wal", "-shm"):
        is_wal = (suffix == "-wal")
        pres_attr = "wal_present" if is_wal else "shm_present"
        rem_attr = "wal_removed" if is_wal else "shm_removed"

        was_pres = getattr(fact, pres_attr)
        was_rem = getattr(fact, rem_attr)

        sidecar = Path(str(destination) + suffix)

        if was_rem:
            try:
                state = os.lstat(sidecar)
                raise ReplacementPreconditionError("Synthetic sidecar handling failed")
            except FileNotFoundError:
                pass
            continue

        if was_pres:
            try:
                state = os.lstat(sidecar)
                if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
                    raise ReplacementPreconditionError("Synthetic sidecar is unsafe")
                os.unlink(sidecar)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ReplacementPersistenceError("Synthetic sidecar handling failed") from exc

            _fsync(destination.parent, directory=True)
            kw = {rem_attr: True}
            current_journal = _transition(journal.operation_id, journal_root, target_key=target_key, **kw)
            fact = next(f for f in current_journal.targets if f.target_key == target_key)
            continue

        try:
            state = os.lstat(sidecar)
            if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
                raise ReplacementPreconditionError("Synthetic sidecar is unsafe")
            before_id = (state.st_dev, state.st_ino, stat.S_IFMT(state.st_mode), state.st_size, getattr(state, "st_mtime_ns", None))
            kw_pres = {pres_attr: True}
            current_journal = _transition(journal.operation_id, journal_root, target_key=target_key, **kw_pres)
            fact = next(f for f in current_journal.targets if f.target_key == target_key)

            after_state = os.lstat(sidecar)
            after_id = (after_state.st_dev, after_state.st_ino, stat.S_IFMT(after_state.st_mode), after_state.st_size, getattr(after_state, "st_mtime_ns", None))
            if before_id != after_id:
                raise ReplacementPreconditionError("Synthetic sidecar changed")

            os.unlink(sidecar)
            _fsync(destination.parent, directory=True)
            kw_rem = {rem_attr: True}
            current_journal = _transition(journal.operation_id, journal_root, target_key=target_key, **kw_rem)
            fact = next(f for f in current_journal.targets if f.target_key == target_key)
        except FileNotFoundError:
            kw_none = {pres_attr: False, rem_attr: False}
            current_journal = _transition(journal.operation_id, journal_root, target_key=target_key, **kw_none)
            fact = next(f for f in current_journal.targets if f.target_key == target_key)
        except OSError as exc:
            raise ReplacementPersistenceError("Synthetic sidecar handling failed") from exc

    return current_journal


def _run_complete_postcheck(
    *,
    operation_id: str,
    selected: ValidatedBackupSnapshot,
    safety: ValidatedBackupSnapshot,
    destinations: tuple[SyntheticRestoreTarget, ...],
    selected_entries: tuple,
    journal_root: Path,
) -> None:
    """Verify all replacement postcheck constraints."""
    journal = load_restore_journal(operation_id, root=journal_root)
    if len(journal.targets) != len(destinations):
        raise ReplacementPostcheckError("Synthetic replacement postcheck failed")
    for fact in journal.targets:
        if not fact.replacement_completed or fact.state is not TargetRestoreState.REPLACED:
            raise ReplacementPostcheckError("Synthetic replacement postcheck failed")

    for target, entry in zip(destinations, selected_entries):
        rec = _read_record(target.path, size=entry[4], digest=entry[5])
        _verify(entry, target.path)
        if os.name != "nt" and permission_health(target.path) != "private":
            raise ReplacementPostcheckError("Synthetic replacement postcheck failed")
        if os.name != "nt" and permission_health(target.path.parent, directory=True) != "private":
            raise ReplacementPostcheckError("Synthetic replacement postcheck failed")
        if Path(str(target.path) + "-wal").exists() or Path(str(target.path) + "-shm").exists():
            raise ReplacementPostcheckError("Synthetic replacement postcheck failed")

    _snapshot(selected, label="Selected")
    _snapshot(safety, label="Safety")


def _run_reentrant_rollback(
    *,
    operation_id: str,
    safety: ValidatedBackupSnapshot,
    destinations: tuple[SyntheticRestoreTarget, ...],
    entries: tuple,
    journal_root: Path,
) -> None:
    """Perform re-entrant rollback in reverse replacement order."""
    try:
        journal = load_restore_journal(operation_id, root=journal_root)
        if journal.stage is not RestoreStage.ROLLBACK_REQUIRED:
            journal = _transition(operation_id, journal_root, stage=RestoreStage.ROLLBACK_REQUIRED)
    except SyntheticReplacementError:
        raise
    except Exception as exc:
        raise ManualRecoveryRequiredError("Manual recovery is required") from exc

    try:
        indices = list(range(len(destinations)))
        control_indices = [i for i in indices if destinations[i].kind == "control"]
        data_indices = [i for i in indices if destinations[i].kind != "control"]
        reverse_ordered = list(reversed(control_indices)) + list(reversed(data_indices))

        for index in reverse_ordered:
            target, entry = destinations[index], entries[index]
            journal = load_restore_journal(operation_id, root=journal_root)
            fact = next(f for f in journal.targets if f.target_key == target.target_key)

            if fact.rollback_completed and fact.state is TargetRestoreState.ROLLED_BACK:
                _read_record(target.path, size=entry[4], digest=entry[5])
                continue

            if not fact.replacement_intent and not fact.replacement_completed and fact.state is not TargetRestoreState.REPLACED:
                # Target was never replaced; destination is original/safety bytes.
                continue

            already_safety = False
            try:
                _read_record(target.path, size=entry[4], digest=entry[5])
                _verify(entry, target.path)
                already_safety = True
            except Exception:
                already_safety = False

            if fact.state is not TargetRestoreState.REPLACED and already_safety:
                # Target was not successfully replaced and destination already matches safety bytes
                continue

            if not fact.rollback_intent:
                journal = _transition(operation_id, journal_root, target_key=target.target_key, rollback_intent=True)

            if not already_safety:
                artifact = _verify_binding(_rollback_directory(target, operation_id, index), operation_id=operation_id, safety=safety, entry=entry, index=index)
                journal = _handle_durable_sidecars(target.path, journal, target.target_key, journal_root)

                if not _same_device(target.path.parent, artifact):
                    raise ReplacementPreconditionError("Rollback filesystem is unsafe")

                os.replace(artifact, target.path)
                _private(target.path)
                _read_record(target.path, size=entry[4], digest=entry[5])
                _verify(entry, target.path)
                _fsync(target.path)
                _fsync(target.path.parent, directory=True)

            journal = _transition(
                operation_id,
                journal_root,
                target_key=target.target_key,
                target_state=TargetRestoreState.ROLLED_BACK,
                rollback_completed=True,
            )

        _transition(operation_id, journal_root, stage=RestoreStage.ROLLED_BACK)
        _transition(operation_id, journal_root, stage=RestoreStage.FAILED_SAFE)
    except Exception as exc:
        try:
            j = load_restore_journal(operation_id, root=journal_root)
            if j.stage in {RestoreStage.ROLLBACK_REQUIRED, RestoreStage.REPLACING, RestoreStage.REPLACED, RestoreStage.POSTCHECK_PASSED}:
                _transition(operation_id, journal_root, stage=RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED)
        except Exception:
            pass
        raise ManualRecoveryRequiredError("Manual recovery is required") from exc


def _cleanup(operation_id: str, destinations: tuple[SyntheticRestoreTarget, ...]) -> None:
    """Descriptor-safe evidence cleanup without recursive rmtree."""
    plans = []
    seen_dirs: set[Path] = set()
    try:
        for index, target in enumerate(destinations):
            for name in (f".garmincoach-restore-stage-{operation_id}", f".garmincoach-restore-rollback-{operation_id}-{index:03d}"):
                directory = target.path.parent / name
                if directory in seen_dirs:
                    continue
                try:
                    dir_stat = os.lstat(directory)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(dir_stat.st_mode) or stat.S_ISLNK(dir_stat.st_mode) or has_symlink_component(directory):
                    raise OSError("unsafe directory")
                if os.name != "nt" and stat.S_IMODE(dir_stat.st_mode) != 0o700:
                    raise OSError("unsafe directory permissions")

                dir_id = _directory_identity(directory)
                _open_verified_directory(path=directory, expected_identity=dir_id, fsync=False)

                binding = directory / (_STAGING_BINDING_NAME if "stage" in name else _BINDING)
                if not binding.exists() or binding.is_symlink():
                    raise OSError("binding missing")

                binding_rec, data = _cleanup_file_record(binding, maximum_size=_MAX_STAGING_BINDING_BYTES)
                parsed = json.loads(data.decode("utf-8"))
                if "stage" in name:
                    expected_artifacts = {item["staged_filename"] for item in parsed["artifacts"]}
                else:
                    expected_artifacts = {parsed["rollback_filename"]}

                allowed_names = {binding.name} | expected_artifacts
                with os.scandir(directory) as scan:
                    for child in scan:
                        if child.name not in allowed_names:
                            raise OSError("unexpected directory child")
                        st = os.lstat(child.path)
                        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                            raise OSError("unexpected directory child")

                artifacts = []
                with os.scandir(directory) as scan:
                    for child in scan:
                        if child.name == binding.name:
                            continue
                        rec, _ = _cleanup_file_record(Path(child.path))
                        artifacts.append(rec)

                seen_dirs.add(directory)
                plans.append((directory, dir_id, binding_rec, sorted(artifacts, key=lambda r: r.path.name)))

        for directory, dir_id, binding_rec, artifacts in plans:
            _open_verified_directory(path=directory, expected_identity=dir_id, fsync=False)
            for art in artifacts:
                cur, _ = _cleanup_file_record(art.path)
                if (cur.device, cur.inode, cur.size) != (art.device, art.inode, art.size):
                    raise OSError("artifact changed before unlink")
                os.unlink(art.path)

            cur_bind, _ = _cleanup_file_record(binding_rec.path)
            if (cur_bind.device, cur_bind.inode, cur_bind.size) != (binding_rec.device, binding_rec.inode, binding_rec.size):
                raise OSError("binding changed before unlink")
            os.unlink(binding_rec.path)

            with os.scandir(directory) as scan:
                if next(scan, None) is not None:
                    raise OSError("directory not empty")

            _fsync(directory, directory=True)
            directory.rmdir()
            _fsync(directory.parent, directory=True)
    except Exception as exc:
        raise EvidenceCleanupError("Operation evidence requires cleanup") from exc


def replace_and_verify_synthetic_restore(*, operation_id: str, selected_backup: ValidatedBackupSnapshot,
                                          safety_backup: ValidatedBackupSnapshot,
                                          destinations: tuple[SyntheticRestoreTarget, ...], staging_result: StagingResult,
                                          fixture_root: Path, journal_root: Path) -> SyntheticReplacementResult:
    """Replace only prepared fixture DBs; automatically roll back a partial run."""
    journal = load_restore_journal(operation_id, root=journal_root)

    # Handle terminal stages and re-entry
    if journal.stage is RestoreStage.COMPLETED:
        _cleanup(operation_id, destinations)
        replaced_keys = tuple(f.target_key for f in journal.targets if f.replacement_completed or f.state is TargetRestoreState.REPLACED)
        return SyntheticReplacementResult(operation_id, RestoreStage.COMPLETED, replaced_keys, False, "COMPLETED")
    if journal.stage is RestoreStage.FAILED_SAFE:
        raise RollbackCompletedError("Synthetic replacement was rolled back")
    if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        raise ManualRecoveryRequiredError("Manual recovery is required")
    if journal.stage is RestoreStage.ROLLED_BACK:
        _transition(operation_id, journal_root, stage=RestoreStage.FAILED_SAFE)
        raise RollbackCompletedError("Synthetic replacement was rolled back")

    try:
        selected, safety = _snapshot(selected_backup, label="Selected"), _snapshot(safety_backup, label="Safety")
        journal, root, selected_entries, safety_entries = _preflight(
            operation_id=operation_id, selected=selected, safety=safety, destinations=destinations,
            staging_result=staging_result, fixture_root=fixture_root, journal_root=journal_root
        )

        if journal.stage is RestoreStage.REPLACEMENT_READY:
            rollback_artifacts = _prepare_rollbacks(operation_id=operation_id, safety=safety, destinations=destinations, fixture_root=root)
            for index, target in enumerate(destinations):
                _read_record(target.path)
                _read_record(staging_result.artifacts[index].path, size=selected_entries[index][4], digest=selected_entries[index][5])
                _verify(selected_entries[index], staging_result.artifacts[index].path)
                _verify_binding(_rollback_directory(target, operation_id, index), operation_id=operation_id, safety=safety, entry=safety_entries[index], index=index)
            if load_restore_journal(operation_id, root=journal_root) != journal:
                raise ReplacementPreconditionError("Synthetic replacement journal changed")
            journal = _transition(operation_id, journal_root, stage=RestoreStage.REPLACING)
    except SyntheticReplacementError:
        _safe_fail(operation_id, journal_root)
        raise
    except Exception as exc:
        _safe_fail(operation_id, journal_root)
        raise ReplacementPreconditionError("Synthetic replacement preflight failed") from exc

    if journal.stage is RestoreStage.ROLLBACK_REQUIRED:
        _run_reentrant_rollback(
            operation_id=operation_id, safety=safety, destinations=destinations,
            entries=safety_entries, journal_root=journal_root
        )

    if journal.stage is RestoreStage.REPLACED:
        try:
            _run_complete_postcheck(
                operation_id=operation_id, selected=selected, safety=safety,
                destinations=destinations, selected_entries=selected_entries, journal_root=journal_root
            )
            _transition(operation_id, journal_root, stage=RestoreStage.POSTCHECK_PASSED)
            journal = load_restore_journal(operation_id, root=journal_root)
        except Exception as cause:
            _run_reentrant_rollback(
                operation_id=operation_id, safety=safety, destinations=destinations,
                entries=safety_entries, journal_root=journal_root
            )

    if journal.stage is RestoreStage.POSTCHECK_PASSED:
        _run_complete_postcheck(
            operation_id=operation_id, selected=selected, safety=safety,
            destinations=destinations, selected_entries=selected_entries, journal_root=journal_root
        )
        completed_journal = _transition(operation_id, journal_root, stage=RestoreStage.COMPLETED)
        _cleanup(operation_id, destinations)
        replaced_keys = tuple(f.target_key for f in completed_journal.targets)
        return SyntheticReplacementResult(operation_id, completed_journal.stage, replaced_keys, False, "COMPLETED")

    # If stage is REPLACING, execute / reconcile target replacements
    replaced_list: list[str] = []
    ordered = tuple(index for index, target in enumerate(destinations) if target.kind in {"tenant", "single_user"}) + tuple(index for index, target in enumerate(destinations) if target.kind == "control")

    try:
        if len([target for target in destinations if target.kind == "control"]) != 1:
            raise ReplacementPreconditionError("Synthetic replacement target set is invalid")

        for index in ordered:
            target, entry, artifact = destinations[index], selected_entries[index], staging_result.artifacts[index]
            journal = load_restore_journal(operation_id, root=journal_root)
            fact = next(f for f in journal.targets if f.target_key == target.target_key)

            if fact.replacement_completed and fact.state is TargetRestoreState.REPLACED:
                _read_record(target.path, size=entry[4], digest=entry[5])
                _verify(entry, target.path)
                replaced_list.append(target.target_key)
                continue

            # Reconciliation: if replacement_intent was written
            if fact.replacement_intent:
                # Check if destination already has selected content
                try:
                    _read_record(target.path, size=entry[4], digest=entry[5])
                    _verify(entry, target.path)
                    journal = _handle_durable_sidecars(target.path, journal, target.target_key, journal_root)
                    _fsync(target.path)
                    _fsync(target.path.parent, directory=True)
                    journal = _transition(
                        operation_id,
                        journal_root,
                        target_key=target.target_key,
                        target_state=TargetRestoreState.REPLACED,
                        replacement_completed=True,
                    )
                    replaced_list.append(target.target_key)
                    continue
                except Exception:
                    pass

            if not fact.replacement_intent:
                journal = _transition(operation_id, journal_root, target_key=target.target_key, replacement_intent=True)

            _read_record(artifact.path, size=entry[4], digest=entry[5])
            _verify(entry, artifact.path)
            journal = _handle_durable_sidecars(target.path, journal, target.target_key, journal_root)

            if not _same_device(target.path.parent, artifact.path):
                raise ReplacementPreconditionError("Synthetic replacement filesystem is unsafe")

            os.replace(artifact.path, target.path)
            _private(target.path)
            replaced_list.append(target.target_key)

            _read_record(target.path, size=entry[4], digest=entry[5])
            _verify(entry, target.path)
            _fsync(target.path)
            _fsync(target.path.parent, directory=True)

            journal = _transition(
                operation_id,
                journal_root,
                target_key=target.target_key,
                target_state=TargetRestoreState.REPLACED,
                replacement_completed=True,
            )

        _transition(operation_id, journal_root, stage=RestoreStage.REPLACED)
        _run_complete_postcheck(
            operation_id=operation_id, selected=selected, safety=safety,
            destinations=destinations, selected_entries=selected_entries, journal_root=journal_root
        )
        _transition(operation_id, journal_root, stage=RestoreStage.POSTCHECK_PASSED)
        completed = _transition(operation_id, journal_root, stage=RestoreStage.COMPLETED)
        _cleanup(operation_id, destinations)
        return SyntheticReplacementResult(operation_id, completed.stage, tuple(replaced_list), False, "COMPLETED")
    except EvidenceCleanupError:
        raise
    except Exception as cause:
        try:
            _run_reentrant_rollback(
                operation_id=operation_id, safety=safety, destinations=destinations,
                entries=safety_entries, journal_root=journal_root
            )
        except ManualRecoveryRequiredError:
            raise
        raise RollbackCompletedError("Synthetic replacement was rolled back") from cause
