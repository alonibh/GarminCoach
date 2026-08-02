"""Synthetic-fixture-only offline guarded-restore staging (Phase 6B2B)."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib, json, os, shutil, stat
from pathlib import Path
from typing import Literal

import config
from guarded_restore import RestoreJournalError, RestoreJournalPersistenceError, RestoreStage, TargetRestoreState, load_restore_journal, update_restore_journal
from operator_storage import has_symlink_component, inspect_sqlite, migration_markers, permission_health, schema_fingerprint
from verified_backup import ValidatedBackupSnapshot, load_validated_backup_snapshot
_BINARY_FLAG = getattr(os, "O_BINARY", 0)
_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)
_STAGED_VERIFICATION_ERROR = "Staged artifact verification failed"
_STAGING_BINDING_FORMAT = "garmincoach-restore-staging-binding-v1"
_STAGING_BINDING_NAME = ".staging-binding.json"
_STAGING_BINDING_TEMP_NAME = ".staging-binding.json.partial"
_MAX_STAGING_BINDING_BYTES = 64 * 1024

class StagingError(RuntimeError): pass
class StagingSourceError(StagingError): pass
class SyntheticDestinationError(StagingError): pass
class StagedVerificationError(StagingError): pass
class StagingPersistenceError(StagingError): pass
class StagingJournalPersistenceError(StagingPersistenceError): pass
class StagingManualCleanupRequiredError(StagingPersistenceError): pass
class StagingOwnershipIndeterminateError(StagingManualCleanupRequiredError): pass
class StagingBindingPersistenceError(StagingPersistenceError): pass
class StagingCleanupBindingError(StagingError): pass
class StagingCleanupPersistenceError(StagingError): pass

@dataclass(frozen=True)
class SyntheticRestoreTarget:
    target_key: str
    kind: Literal["control", "single_user", "tenant"]
    path: Path = field(repr=False)

@dataclass(frozen=True)
class StagedArtifact:
    operation_id: str; target_key: str; kind: str; target_order: int
    path: Path = field(repr=False); size_bytes: int = 0; sha256: str = ""; schema_fingerprint: str = ""; migration_markers: tuple[str, tuple[str, ...], str] = ()

@dataclass(frozen=True)
class StagingResult:
    operation_id: str; artifacts: tuple[StagedArtifact, ...]

def _private(path: Path, directory=False):
    try: os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt": raise StagingPersistenceError("Staging permissions could not be set")
def _sha(path: Path) -> str:
    h=hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    except OSError as exc: raise StagingSourceError("Staging source cannot be read") from exc
    return h.hexdigest()
def _inside(child: Path, root: Path) -> bool:
    try: child.resolve(strict=False).relative_to(root.resolve(strict=False)); return True
    except ValueError: return False
def _stage_dir(parent: Path, operation_id: str) -> Path: return parent / f".garmincoach-restore-stage-{operation_id}"
def _staged_filename(entry, index: int) -> str:
    _,kind,tenant,_,_,_,_,_=entry
    identity="control" if kind=="control" else "single-user" if kind=="single_user" else f"tenant-{tenant}"
    return f"{index:03d}-{identity}.sqlite.staged"
def _canonical_json(value: object) -> bytes: return (json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n").encode("utf-8")
def _validate_fixture_root(root: Path, backup: Path) -> Path:
    if not root.exists() or not root.is_dir() or root.is_symlink() or has_symlink_component(root): raise SyntheticDestinationError("Synthetic fixture root is unsafe")
    resolved=root.resolve()
    protected=(Path(config.CONTROL_DB_PATH).resolve(),Path(config.DB_PATH).resolve(),Path(config.MULTI_USER_DATA_ROOT).resolve(),Path(config.OPERATOR_BACKUP_ROOT).resolve(),Path(config.OPERATOR_RESTORE_ROOT).resolve(),backup.resolve())
    if any(resolved==p or resolved in p.parents or p in resolved.parents for p in protected): raise SyntheticDestinationError("Synthetic fixture root is unsafe")
    if os.name!="nt" and permission_health(resolved,directory=True)!="private": raise SyntheticDestinationError("Synthetic fixture root is unsafe")
    return resolved
def _entry_tuple(entry) -> tuple[str,str,str|None,str,int,str,str,tuple[str,tuple[str,...],str]]:
    return (entry.target_key,entry.kind,entry.tenant_id,entry.filename,entry.size_bytes,entry.sha256,entry.schema_fingerprint,(entry.migration_ledger,entry.migration_keys,entry.migration_state))

def _binding_payload(journal, entries, destinations, root: Path, parent: Path, indices: tuple[int,...]) -> dict[str, object]:
    artifacts=[]
    for index in indices:
        entry=entries[index]; dest=destinations[index]
        artifacts.append({"target_key":entry[0],"kind":entry[1],"target_order":index,"destination":dest.path.resolve().relative_to(root).as_posix(),"staged_filename":_staged_filename(entry,index),"size_bytes":entry[4],"sha256":entry[5]})
    return {"format_version":_STAGING_BINDING_FORMAT,"operation_id":journal.operation_id,"selected_backup_id":journal.selected_backup_id,"selected_backup_manifest_sha256":journal.selected_backup_manifest_sha256,"safety_backup_id":journal.safety_backup_id,"runtime_mode":journal.runtime_mode,"target_set_hash":journal.target_set_hash,"stage_parent":parent.resolve().relative_to(root).as_posix(),"artifacts":artifacts}

def _fsync_directory(path: Path) -> None:
    if os.name=="nt": return
    fd=os.open(str(path),os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
    try: os.fsync(fd)
    finally: os.close(fd)

def _identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size, getattr(info, "st_mtime_ns", None))

def _read_exact(fd: int, size: int) -> bytes:
    data=b""
    while len(data) < size:
        chunk=os.read(fd, size-len(data))
        if not chunk: raise OSError("early eof")
        data+=chunk
    if os.read(fd, 1): raise OSError("trailing bytes")
    return data

def _stage_directory_is_private(stage_directory: Path) -> bool:
    state=os.lstat(stage_directory)
    return stat.S_ISDIR(state.st_mode) and not stage_directory.is_symlink() and not has_symlink_component(stage_directory) and (os.name=="nt" or stat.S_IMODE(state.st_mode)==0o700)

def _compensate_unpublished_binding(*, stage_directory: Path, temporary: Path) -> None:
    """Remove only the invocation's empty, as-yet-unpublished staging directory."""
    try:
        if not _stage_directory_is_private(stage_directory): raise OSError("unsafe stage")
        try: item=os.lstat(temporary)
        except FileNotFoundError: item=None
        if item is not None:
            if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode): raise OSError("unsafe temporary")
            os.unlink(temporary)
        if tuple(stage_directory.iterdir()): raise OSError("unexpected entries")
        stage_directory.rmdir(); _fsync_directory(stage_directory.parent)
    except OSError as exc:
        raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc

def _write_staging_binding(*, stage_directory: Path, payload: dict[str, object]) -> None:
    final=stage_directory/_STAGING_BINDING_NAME; temporary=stage_directory/_STAGING_BINDING_TEMP_NAME; data=_canonical_json(payload); fd=None; published=False
    try:
        if not _stage_directory_is_private(stage_directory) or final.exists() or temporary.exists(): raise OSError("unsafe")
        fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL|_NOFOLLOW_FLAG|_BINARY_FLAG,0o600); offset=0
        while offset<len(data):
            count=os.write(fd,data[offset:])
            if count<=0: raise OSError("write")
            offset+=count
        os.fsync(fd); os.close(fd); fd=None; os.replace(temporary,final); published=True; _private(final)
        verify=os.open(str(final),os.O_RDONLY|_NOFOLLOW_FLAG|_BINARY_FLAG)
        try:
            info=os.fstat(verify)
            if not stat.S_ISREG(info.st_mode) or info.st_size!=len(data) or (os.name!="nt" and stat.S_IMODE(info.st_mode)!=0o600): raise OSError("verify")
            before=_identity(info)
            if _read_exact(verify,len(data))!=data or before!=_identity(os.fstat(verify)): raise OSError("verify")
            if os.name!="nt": os.fsync(verify)
        finally: os.close(verify)
        _fsync_directory(stage_directory)
    except StagingPersistenceError as exc:
        if published: raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc
        _compensate_unpublished_binding(stage_directory=stage_directory,temporary=temporary)
        raise StagingBindingPersistenceError("Restore staging binding could not be persisted") from exc
    except OSError as exc:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        if published: raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc
        _compensate_unpublished_binding(stage_directory=stage_directory,temporary=temporary)
        raise StagingBindingPersistenceError("Restore staging binding could not be persisted") from exc

_BINDING_KEYS={"format_version","operation_id","selected_backup_id","selected_backup_manifest_sha256","safety_backup_id","runtime_mode","target_set_hash","stage_parent","artifacts"}
_ARTIFACT_KEYS={"target_key","kind","target_order","destination","staged_filename","size_bytes","sha256"}
def _relative_binding_path(value: object, *, filename=False) -> bool:
    return isinstance(value,str) and value and "\\" not in value and not Path(value).is_absolute() and ".." not in value.split("/") and (not filename or "/" not in value)
def _validate_binding_schema(parsed: object) -> None:
    if not isinstance(parsed,dict) or set(parsed)!=_BINDING_KEYS: raise ValueError("keys")
    for name in ("format_version","operation_id","selected_backup_id","safety_backup_id","runtime_mode","target_set_hash"):
        if not isinstance(parsed[name],str) or not parsed[name]: raise ValueError(name)
    if not _lower_sha256(parsed["selected_backup_manifest_sha256"]) or not _relative_binding_path(parsed["stage_parent"]): raise ValueError("path")
    artifacts=parsed["artifacts"]
    if not isinstance(artifacts,list) or not artifacts: raise ValueError("artifacts")
    seen=[set(),set(),set(),set()]
    previous_order=-1
    for item in artifacts:
        if not isinstance(item,dict) or set(item)!=_ARTIFACT_KEYS: raise ValueError("artifact keys")
        if item["kind"] not in {"control","single_user","tenant"} or not isinstance(item["target_key"],str) or not item["target_key"]: raise ValueError("kind")
        for key in ("target_order","size_bytes"):
            if type(item[key]) is not int or item[key]<0: raise ValueError(key)
        if item["target_order"]<=previous_order or not _lower_sha256(item["sha256"]) or not _relative_binding_path(item["destination"]) or not _relative_binding_path(item["staged_filename"],filename=True): raise ValueError("value")
        previous_order=item["target_order"]
        for bucket,value in zip(seen,(item["target_key"],item["target_order"],item["destination"],item["staged_filename"])):
            if value in bucket: raise ValueError("duplicate")
            bucket.add(value)

def _load_staging_binding(*, stage_directory: Path, expected_payload: dict[str, object]) -> None:
    binding=stage_directory/_STAGING_BINDING_NAME
    try:
        if not _stage_directory_is_private(stage_directory) or binding.is_symlink(): raise OSError("unsafe")
        fd=os.open(str(binding),os.O_RDONLY|_NOFOLLOW_FLAG|_BINARY_FLAG)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size<=0 or before.st_size>_MAX_STAGING_BINDING_BYTES or (os.name!="nt" and stat.S_IMODE(before.st_mode)!=0o600): raise OSError("invalid")
            identity=_identity(before); data=_read_exact(fd,before.st_size)
            after=os.fstat(fd)
            if identity!=_identity(after): raise OSError("changed")
        finally: os.close(fd)
        parsed=json.loads(data.decode("utf-8"))
        _validate_binding_schema(parsed)
        if _canonical_json(parsed)!=data or parsed!=expected_payload: raise ValueError("invalid")
    except (OSError,UnicodeError,ValueError,json.JSONDecodeError) as exc: raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid") from exc
def _validate_inputs(operation_id: str, validated: ValidatedBackupSnapshot, destinations: tuple[SyntheticRestoreTarget,...], fixture_root: Path, journal_root: Path, expected_stage=RestoreStage.CURRENT_SNAPSHOT_CREATED):
    journal=load_restore_journal(operation_id,root=journal_root)
    if journal.stage is not expected_stage or not journal.safety_backup_id: raise StagingError("Restore journal is not ready for synthetic staging")
    if type(validated) is not ValidatedBackupSnapshot: raise StagingSourceError("Validated backup does not match restore journal")
    try: fresh=load_validated_backup_snapshot(validated.directory)
    except Exception as exc: raise StagingSourceError("Validated backup does not match restore journal") from exc
    if fresh != validated or (fresh.backup_id!=journal.selected_backup_id or fresh.manifest_sha256!=journal.selected_backup_manifest_sha256 or fresh.runtime_mode!=journal.runtime_mode or fresh.target_keys!=journal.target_keys or (journal.expected_application_commit!="unknown" and fresh.application_commit!=journal.expected_application_commit)):
        raise StagingSourceError("Validated backup does not match restore journal")
    validated=fresh
    root=_validate_fixture_root(Path(fixture_root),validated.directory)
    if len(destinations)!=len(journal.target_keys) or tuple(d.target_key for d in destinations)!=journal.target_keys or len({str(d.path.resolve(strict=False)) for d in destinations})!=len(destinations): raise SyntheticDestinationError("Synthetic destinations do not match restore journal")
    entries=tuple(_entry_tuple(e) for e in validated.entries)
    for entry,dest in zip(entries,destinations):
        key,kind,_,_,_,_,_,_=entry
        if key!=dest.target_key or kind!=dest.kind or not _inside(dest.path,root) or dest.path.is_symlink() or has_symlink_component(dest.path) or not dest.path.exists() or not dest.path.is_file(): raise SyntheticDestinationError("Synthetic destination is unsafe")
        if not _inside(dest.path.parent,root) or (os.name!="nt" and permission_health(dest.path.parent,directory=True)!="private"): raise SyntheticDestinationError("Synthetic destination is unsafe")
    return journal,entries,root
def _copy(entry, backup: Path, stage: Path, index: int) -> Path:
    key,kind,tenant,filename,size,digest,_,_=entry
    if Path(filename).name!=filename or "/" in filename or "\\" in filename: raise StagingSourceError("Validated staging filename is unsafe")
    source=backup/filename
    if source.is_symlink() or has_symlink_component(source) or not source.is_file(): raise StagingSourceError("Validated staging source changed")
    staged_name=_staged_filename(entry,index); final=stage/staged_name; partial=stage/f".{staged_name.removesuffix('.sqlite.staged')}.partial"
    if final.exists() or partial.exists() or final.is_symlink() or partial.is_symlink(): raise StagingPersistenceError("Synthetic staging artifact already exists")
    source_fd: int|None=None; partial_fd: int|None=None
    try:
        source_fd=os.open(str(source),os.O_RDONLY|_NOFOLLOW_FLAG|_BINARY_FLAG); before=os.fstat(source_fd)
        import stat
        identity=(before.st_dev,before.st_ino,stat.S_IFMT(before.st_mode),before.st_size,getattr(before,"st_mtime_ns",None))
        if not stat.S_ISREG(before.st_mode) or before.st_size!=size: raise StagingSourceError("Validated staging source changed")
        partial_fd=os.open(str(partial),os.O_WRONLY|os.O_CREAT|os.O_EXCL|_NOFOLLOW_FLAG|_BINARY_FLAG,0o600)
        h=hashlib.sha256(); count=0
        while True:
            chunk=os.read(source_fd,1024*1024)
            if not chunk: break
            h.update(chunk); count+=len(chunk); offset=0
            while offset<len(chunk):
                written=os.write(partial_fd,chunk[offset:])
                if written<=0: raise OSError("staging write failed")
                offset+=written
        os.fsync(partial_fd); after=os.fstat(source_fd)
        if identity!=(after.st_dev,after.st_ino,stat.S_IFMT(after.st_mode),after.st_size,getattr(after,"st_mtime_ns",None)) or count!=size or h.hexdigest()!=digest: raise StagingSourceError("Validated staging source changed")
        os.close(source_fd); source_fd=None; os.close(partial_fd); partial_fd=None
        if stage.is_symlink() or partial.is_symlink() or final.exists() or final.is_symlink() or partial.parent!=stage or final.parent!=stage: raise StagingPersistenceError("Synthetic staging artifact is unsafe")
        os.replace(partial,final); _private(final)
        final_fd=os.open(str(final),os.O_RDONLY|_NOFOLLOW_FLAG|_BINARY_FLAG)
        try:
            state=os.fstat(final_fd)
            if not stat.S_ISREG(state.st_mode) or state.st_size!=size: raise StagingPersistenceError("Synthetic staging artifact is unsafe")
            if os.name!="nt" and stat.S_IMODE(state.st_mode)!=0o600: raise StagingPersistenceError("Synthetic staging artifact is unsafe")
            if os.name != "nt": os.fsync(final_fd)
        finally: os.close(final_fd)
        if os.name != "nt":
            directory_fd=os.open(str(stage),os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        return final
    except StagingError: raise
    except OSError as exc: raise StagingPersistenceError("Synthetic staging copy failed") from exc
    finally:
        for descriptor in (source_fd,partial_fd):
            if descriptor is not None:
                try: os.close(descriptor)
                except OSError: pass
        if partial.exists():
            try: partial.unlink()
            except OSError: pass
def _verification_failure() -> StagedVerificationError:
    return StagedVerificationError(_STAGED_VERIFICATION_ERROR)

def _lower_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)

def _verify_impl(entry, staged: Path) -> None:
    key,kind,tenant,_,size,digest,fingerprint,markers=entry
    if staged.is_symlink() or has_symlink_component(staged): raise _verification_failure()
    state = staged.stat()
    if not stat.S_ISREG(state.st_mode) or state.st_size != size or _sha(staged) != digest: raise _verification_failure()
    if os.name!="nt" and permission_health(staged)!="private": raise _verification_failure()
    inspected=inspect_sqlite(staged,deep=True)
    actual= migration_markers(staged,kind)
    if inspected.readable is not True or inspected.quick_check_ok is not True or inspected.integrity_check_ok is not True or inspected.foreign_keys_ok is not True: raise _verification_failure()
    actual_fingerprint = schema_fingerprint(staged)
    if not _lower_sha256(actual_fingerprint) or actual_fingerprint != fingerprint: raise _verification_failure()
    if not isinstance(actual, dict) or set(actual) != {"ledger", "keys", "state"}: raise _verification_failure()
    ledger, keys, marker_state = actual["ledger"], actual["keys"], actual["state"]
    if not isinstance(ledger, str) or not isinstance(marker_state, str) or not isinstance(keys, (list, tuple)) or any(not isinstance(item, str) for item in keys): raise _verification_failure()
    marker_keys = tuple(keys)
    if marker_keys != tuple(sorted(marker_keys)) or len(marker_keys) != len(set(marker_keys)) or (ledger, marker_keys, marker_state) != markers: raise _verification_failure()

def _verify(entry, staged: Path) -> None:
    try:
        _verify_impl(entry, staged)
    except StagedVerificationError:
        raise
    except Exception as exc:
        raise StagedVerificationError(_STAGED_VERIFICATION_ERROR) from exc

def _remove_unrecorded_staged_artifact(*, artifact: Path, stage_directory: Path) -> None:
    """Remove only a finalised artifact which never gained journal ownership."""
    import stat
    try:
        if artifact.parent != stage_directory or stage_directory.is_symlink() or has_symlink_component(stage_directory):
            raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup")
        directory_state = os.lstat(stage_directory)
        if not stat.S_ISDIR(directory_state.st_mode):
            raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup")
        try:
            artifact_state = os.lstat(artifact)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(artifact_state.st_mode) or not stat.S_ISREG(artifact_state.st_mode):
            raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup")
        os.unlink(artifact)
        if os.name != "nt":
            descriptor = os.open(str(stage_directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except StagingManualCleanupRequiredError:
        raise
    except OSError as exc:
        raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc

def _transition_staging_failure_to_failed_safe(*, operation_id: str, journal_root: Path) -> None:
    """Persist the only legal terminal transition available during staging."""
    try:
        journal = load_restore_journal(operation_id, root=journal_root)
        if journal.stage is RestoreStage.FAILED_SAFE:
            return
        if journal.stage not in {
            RestoreStage.CURRENT_SNAPSHOT_CREATED,
            RestoreStage.RESTORE_STAGED,
            RestoreStage.STAGED_VERIFIED,
            RestoreStage.REPLACEMENT_READY,
        }:
            raise RestoreJournalError("Restore journal cannot enter failed safe state")
        update_restore_journal(operation_id, root=journal_root, stage=RestoreStage.FAILED_SAFE)
    except (RestoreJournalError, RestoreJournalPersistenceError, OSError) as exc:
        raise StagingJournalPersistenceError("Restore staging journal could not be persisted") from exc

def _journal_failure(*, operation_id: str, journal_root: Path) -> StagingJournalPersistenceError:
    _transition_staging_failure_to_failed_safe(operation_id=operation_id, journal_root=journal_root)
    return StagingJournalPersistenceError("Restore staging journal could not be persisted")

def _reconcile_failed_staged_transition(*, operation_id: str, journal_root: Path, target_key: str, artifact: Path, stage_directory: Path, cause: Exception) -> None:
    """Resolve ownership after an ambiguous target-journal persistence error."""
    try:
        journal = load_restore_journal(operation_id, root=journal_root)
        facts = tuple(fact for fact in journal.targets if fact.target_key == target_key)
        if len(facts) != 1:
            raise RestoreJournalError("Restore journal target is indeterminate")
        state = facts[0].state
    except (RestoreJournalError, RestoreJournalPersistenceError, OSError, ValueError) as exc:
        raise StagingOwnershipIndeterminateError("Staged artifact ownership is indeterminate") from cause
    if state is TargetRestoreState.PENDING:
        try:
            _remove_unrecorded_staged_artifact(artifact=artifact, stage_directory=stage_directory)
        except StagingManualCleanupRequiredError:
            raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from cause
    elif state is not TargetRestoreState.STAGED:
        raise StagingOwnershipIndeterminateError("Staged artifact ownership is indeterminate") from cause
    try:
        _transition_staging_failure_to_failed_safe(operation_id=operation_id, journal_root=journal_root)
    except StagingJournalPersistenceError:
        raise StagingJournalPersistenceError("Restore staging journal could not be persisted") from cause
    raise StagingJournalPersistenceError("Restore staging journal could not be persisted") from cause

def _final_revalidate_before_replacement_ready(*, operation_id: str, validated_backup: ValidatedBackupSnapshot, destinations: tuple[SyntheticRestoreTarget,...], fixture_root: Path, journal_root: Path, staged) -> None:
    """Repeat read-only source, journal, destination, and artifact checks at the ready boundary."""
    journal, entries, root = _validate_inputs(operation_id, validated_backup, destinations, fixture_root, journal_root, expected_stage=RestoreStage.STAGED_VERIFIED)
    if any(fact.state is not TargetRestoreState.STAGED_VERIFIED for fact in journal.targets):
        raise StagingError("Restore journal is not ready for synthetic staging")
    if len(entries) != len(staged): raise StagingError("Validated backup does not match restore journal")
    for index, (entry, artifact, _) in enumerate(staged):
        if entry != entries[index] or artifact.parent != _stage_dir(destinations[index].path.parent, operation_id):
            raise StagingError("Validated backup does not match restore journal")
        _verify(entry, artifact)
        _load_staging_binding(stage_directory=artifact.parent, expected_payload=_binding_payload(journal, entries, destinations, root, destinations[index].path.parent, tuple(i for i, destination in enumerate(destinations) if destination.path.parent == destinations[index].path.parent)))

def stage_and_verify_synthetic_restore(*,operation_id: str,validated_backup: ValidatedBackupSnapshot,destinations: tuple[SyntheticRestoreTarget,...],fixture_root: Path,journal_root: Path)->StagingResult:
    try:
        journal,entries,root=_validate_inputs(operation_id,validated_backup,destinations,fixture_root,journal_root)
        try:
            update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.RESTORE_STAGED)
        except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
            raise _journal_failure(operation_id=operation_id,journal_root=journal_root) from exc
        parent_indices={}
        for index,d in enumerate(destinations): parent_indices.setdefault(d.path.parent,[]).append(index)
        dirs={}
        for parent,indices in parent_indices.items():
            stage=_stage_dir(parent,operation_id)
            created=False
            try: os.lstat(stage)
            except FileNotFoundError: pass
            else: raise StagingPersistenceError("Synthetic staging directory already exists")
            try:
                stage.mkdir(mode=0o700); created=True; _private(stage,True)
                if not _stage_directory_is_private(stage) or (os.name!="nt" and permission_health(stage,directory=True)!="private"): raise OSError("unsafe stage")
                _write_staging_binding(stage_directory=stage,payload=_binding_payload(journal,entries,destinations,root,parent,tuple(indices)))
            except StagingManualCleanupRequiredError: raise
            except StagingBindingPersistenceError: raise
            except (OSError, StagingPersistenceError) as exc:
                if created: _compensate_unpublished_binding(stage_directory=stage,temporary=stage/_STAGING_BINDING_TEMP_NAME)
                raise StagingBindingPersistenceError("Restore staging binding could not be persisted") from exc
            dirs[parent]=stage
        staged=[]
        for index,(entry,dest) in enumerate(zip(entries,destinations)):
            file=_copy(entry,validated_backup.directory,dirs[dest.path.parent],index)
            try:
                update_restore_journal(operation_id,root=journal_root,target_key=entry[0],target_state=TargetRestoreState.STAGED)
            except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
                _reconcile_failed_staged_transition(operation_id=operation_id,journal_root=journal_root,target_key=entry[0],artifact=file,stage_directory=dirs[dest.path.parent],cause=exc)
            staged.append((entry,file,index))
        try:
            update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.STAGED_VERIFIED)
        except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
            raise _journal_failure(operation_id=operation_id,journal_root=journal_root) from exc
        artifacts=[]
        for entry,file,index in staged:
            _verify(entry,file)
            try:
                update_restore_journal(operation_id,root=journal_root,target_key=entry[0],target_state=TargetRestoreState.STAGED_VERIFIED)
            except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
                raise _journal_failure(operation_id=operation_id,journal_root=journal_root) from exc
            artifacts.append(StagedArtifact(operation_id,entry[0],entry[1],index,file,entry[4],entry[5],entry[6],entry[7]))
        try:
            _final_revalidate_before_replacement_ready(operation_id=operation_id,validated_backup=validated_backup,destinations=destinations,fixture_root=fixture_root,journal_root=journal_root,staged=staged)
            update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.REPLACEMENT_READY)
        except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
            raise _journal_failure(operation_id=operation_id,journal_root=journal_root) from exc
        return StagingResult(operation_id,tuple(artifacts))
    except StagingManualCleanupRequiredError:
        raise
    except StagingJournalPersistenceError:
        raise
    except (StagingError,RestoreJournalError,RestoreJournalPersistenceError):
        _transition_staging_failure_to_failed_safe(operation_id=operation_id,journal_root=journal_root)
        raise
def cleanup_synthetic_staging(*,operation_id: str,validated_backup: ValidatedBackupSnapshot,destinations: tuple[SyntheticRestoreTarget,...],fixture_root: Path,journal_root: Path)->None:
    try:
        journal,entries,root=_validate_inputs(operation_id,validated_backup,destinations,fixture_root,journal_root,expected_stage=RestoreStage.FAILED_SAFE)
    except StagingError as exc: raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid") from exc
    groups={}
    for index,destination in enumerate(destinations): groups.setdefault(destination.path.parent,[]).append(index)
    prepared=[]
    try:
        for parent,indices in groups.items():
            stage=_stage_dir(parent,operation_id)
            if not stage.exists(): continue
            payload=_binding_payload(journal,entries,destinations,root,parent,tuple(indices)); _load_staging_binding(stage_directory=stage,expected_payload=payload)
            allowed={_STAGING_BINDING_NAME,*(_staged_filename(entries[index],index) for index in indices)}
            present=[]
            for child in stage.iterdir():
                if child.name not in allowed or child.is_symlink() or not child.is_file(): raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid")
                if child.name!=_STAGING_BINDING_NAME:
                    index=next(i for i in indices if _staged_filename(entries[i],i)==child.name)
                    if child.stat().st_size!=entries[index][4] or _sha(child)!=entries[index][5]: raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid")
                    present.append((index,child))
            prepared.append((stage,sorted(present)))
    except StagingCleanupBindingError: raise
    except (OSError,ValueError,StagingError) as exc: raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid") from exc
    try:
        for stage,present in prepared:
            for _,child in present: os.unlink(child)
            os.unlink(stage/_STAGING_BINDING_NAME); _fsync_directory(stage); stage.rmdir(); _fsync_directory(stage.parent)
    except OSError as exc: raise StagingCleanupPersistenceError("Synthetic staging cleanup could not be persisted") from exc
