"""Synthetic-fixture-only offline guarded-restore staging (Phase 6B2B)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
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

class FinalReadinessError(StagingError): pass

@dataclass(frozen=True)
class SyntheticRestoreTarget:
    target_key: str
    kind: Literal["control", "single_user", "tenant"]
    path: Path = field(repr=False)

@dataclass(frozen=True)
class StagedArtifact:
    operation_id: str; target_key: str; kind: str; target_order: int
    path: Path = field(repr=False); size_bytes: int = 0; sha256: str = ""; schema_fingerprint: str = ""; migration_markers: tuple[str, tuple[str, ...], str] = ()
    # Kept in the frozen in-memory result so 6B2C can reject a destination
    # whose contents drifted after the final 6B2B readiness barrier.
    destination_size_bytes: int = 0; destination_sha256: str = ""

@dataclass(frozen=True)
class StagingResult:
    operation_id: str; artifacts: tuple[StagedArtifact, ...]

@dataclass(frozen=True)
class _ReadyFileRecord:
    path: Path
    device: int
    inode: int
    file_type: int
    size: int
    mtime_ns: int | None
    mode: int | None
    sha256: str

@dataclass(frozen=True)
class _DestinationBaseline:
    target_order: int
    target_key: str
    kind: str
    file: _ReadyFileRecord
    parent_path: Path
    parent_device: int
    parent_inode: int
    parent_file_type: int
    parent_mode: int | None

@dataclass(frozen=True)
class _ReadyDirectoryRecord:
    path: Path
    device: int
    inode: int
    file_type: int
    mode: int | None
    parent_path: Path
    parent_device: int
    parent_inode: int
    parent_file_type: int
    parent_mode: int | None
    entries: tuple[str, ...]
    target_indices: tuple[int, ...]

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
    fd=os.open(str(path),os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|_NOFOLLOW_FLAG)
    try: os.fsync(fd)
    finally: os.close(fd)

def _identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size, getattr(info, "st_mtime_ns", None))

def _directory_identity(path: Path):
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode): raise OSError("not a directory")
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), getattr(info, "st_mtime_ns", None), stat.S_IMODE(info.st_mode))

def _same_directory_identity(actual, expected) -> bool:
    # A directory's mtime legitimately changes as its own binding entries are created.
    return actual[:3]+actual[4:] == expected[:3]+expected[4:]

def _open_verified_directory(*, path: Path, expected_identity, fsync=True) -> None:
    """Open a directory without text/binary conversion and bind it to its identity."""
    if os.name=="nt":
        if not _same_directory_identity(_directory_identity(path),expected_identity): raise OSError("directory identity changed")
        return
    fd=os.open(str(path), os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|_NOFOLLOW_FLAG)
    try:
        info=os.fstat(fd)
        actual=(info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode),getattr(info,"st_mtime_ns",None),stat.S_IMODE(info.st_mode))
        if not _same_directory_identity(actual,expected_identity) or not stat.S_ISDIR(info.st_mode) or (os.name!="nt" and stat.S_IMODE(info.st_mode)!=0o700): raise OSError("directory identity changed")
        if fsync and os.name!="nt": os.fsync(fd)
    finally: os.close(fd)

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

def _compensate_unpublished_binding(*, stage_directory: Path, stage_identity, parent_identity, temporary: Path, temporary_identity=None) -> None:
    """Remove only the invocation's empty, as-yet-unpublished staging directory."""
    try:
        if not _stage_directory_is_private(stage_directory) or not _same_directory_identity(_directory_identity(stage_directory),stage_identity): raise OSError("unsafe stage")
        try: item=os.lstat(temporary)
        except FileNotFoundError: item=None
        if item is not None:
            if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode) or temporary_identity is None or _identity(item)!=temporary_identity: raise OSError("unsafe temporary")
            os.unlink(temporary)
        if tuple(stage_directory.iterdir()): raise OSError("unexpected entries")
        if not _same_directory_identity(_directory_identity(stage_directory),stage_identity): raise OSError("stage replaced")
        stage_directory.rmdir()
        # rmdir changes the parent's mtime, so preserve and compare its stable identity fields.
        parent_now=_directory_identity(stage_directory.parent)
        if not _same_directory_identity(parent_now,parent_identity): raise OSError("parent replaced")
        _open_verified_directory(path=stage_directory.parent,expected_identity=parent_now)
    except OSError as exc:
        raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc

def _write_staging_binding(*, stage_directory: Path, stage_identity, parent_identity, payload: dict[str, object]) -> None:
    final=stage_directory/_STAGING_BINDING_NAME; temporary=stage_directory/_STAGING_BINDING_TEMP_NAME; data=_canonical_json(payload); fd=None; published=False; temporary_identity=None
    try:
        if not _stage_directory_is_private(stage_directory) or not _same_directory_identity(_directory_identity(stage_directory),stage_identity) or final.exists() or temporary.exists(): raise OSError("unsafe")
        fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL|_NOFOLLOW_FLAG|_BINARY_FLAG,0o600); offset=0
        while offset<len(data):
            count=os.write(fd,data[offset:])
            if count<=0: raise OSError("write")
            offset+=count
        os.fsync(fd); temporary_identity=_identity(os.fstat(fd)); os.close(fd); fd=None
        try: os.lstat(final)
        except FileNotFoundError: pass
        else: raise OSError("final exists")
        if not _same_directory_identity(_directory_identity(stage_directory),stage_identity): raise OSError("stage replaced")
        os.replace(temporary,final); published=True; _private(final)
        verify=os.open(str(final),os.O_RDONLY|_NOFOLLOW_FLAG|_BINARY_FLAG)
        try:
            info=os.fstat(verify)
            if not stat.S_ISREG(info.st_mode) or info.st_size!=len(data) or (os.name!="nt" and stat.S_IMODE(info.st_mode)!=0o600): raise OSError("verify")
            before=_identity(info); path_state=os.lstat(final)
            if _read_exact(verify,len(data))!=data or before!=_identity(os.fstat(verify)) or before!=_identity(path_state): raise OSError("verify")
            if os.name!="nt": os.fsync(verify)
        finally: os.close(verify)
        if not _same_directory_identity(_directory_identity(stage_directory),stage_identity): raise OSError("stage replaced")
        _open_verified_directory(path=stage_directory,expected_identity=stage_identity)
    except StagingPersistenceError as exc:
        if published: raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc
        _compensate_unpublished_binding(stage_directory=stage_directory,stage_identity=stage_identity,parent_identity=parent_identity,temporary=temporary,temporary_identity=temporary_identity)
        raise StagingBindingPersistenceError("Restore staging binding could not be persisted") from exc
    except OSError as exc:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        if published: raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc
        _compensate_unpublished_binding(stage_directory=stage_directory,stage_identity=stage_identity,parent_identity=parent_identity,temporary=temporary,temporary_identity=temporary_identity)
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

@dataclass(frozen=True)
class _CleanupDirectoryRecord:
    path: Path
    device: int
    inode: int
    file_type: int
    mode: int | None
    parent_path: Path
    parent_device: int
    parent_inode: int
    parent_file_type: int
    parent_mode: int | None
    parent_mtime_ns: int | None
    target_indices: tuple[int, ...]

@dataclass(frozen=True)
class _CleanupBindingRecord:
    path: Path
    device: int
    inode: int
    file_type: int
    size: int
    mtime_ns: int | None
    mode: int | None
    canonical_bytes: bytes

@dataclass(frozen=True)
class _CleanupArtifactRecord:
    target_order: int
    path: Path
    device: int
    inode: int
    file_type: int
    size: int
    mtime_ns: int | None
    mode: int | None
    expected_sha256: str

@dataclass(frozen=True)
class _CleanupStagePlan:
    directory: _CleanupDirectoryRecord
    binding: _CleanupBindingRecord
    artifacts: tuple[_CleanupArtifactRecord, ...]

def _cleanup_file_record(path: Path, *, maximum_size: int | None = None, expected_size: int | None = None, expected_sha256: str | None = None) -> tuple[_CleanupBindingRecord, bytes]:
    """Read a regular cleanup file via a no-follow descriptor and bind its contents."""
    fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
    try:
        before = os.fstat(fd)
        mode = stat.S_IMODE(before.st_mode) if os.name != "nt" else None
        if not stat.S_ISREG(before.st_mode) or (os.name != "nt" and mode != 0o600): raise OSError("unsafe file")
        if before.st_size < 0 or (maximum_size is not None and before.st_size > maximum_size) or (expected_size is not None and before.st_size != expected_size): raise OSError("unsafe size")
        data = _read_exact(fd, before.st_size)
        after = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode), before.st_size, getattr(before, "st_mtime_ns", None), mode)
        if identity != (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode), after.st_size, getattr(after, "st_mtime_ns", None), stat.S_IMODE(after.st_mode) if os.name != "nt" else None): raise OSError("file changed")
        if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256: raise OSError("wrong digest")
        return _CleanupBindingRecord(path, before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode), before.st_size, getattr(before, "st_mtime_ns", None), mode, data), data
    finally:
        os.close(fd)

def _cleanup_directory_identity(state) -> tuple[int, int, int, int | None, int | None]:
    return (state.st_dev, state.st_ino, stat.S_IFMT(state.st_mode), getattr(state, "st_mtime_ns", None), stat.S_IMODE(state.st_mode))

def _stable_cleanup_directory_identity(identity) -> tuple[int, int, int, int | None]:
    return (identity[0], identity[1], identity[2], identity[4])

def _record_directory_identity(record: _CleanupDirectoryRecord) -> tuple[int, int, int, int | None, int | None]:
    return (record.device, record.inode, record.file_type, None, record.mode)

def _record_parent_identity(record: _CleanupDirectoryRecord) -> tuple[int, int, int, int | None, int | None]:
    return (record.parent_device, record.parent_inode, record.parent_file_type, record.parent_mtime_ns, record.parent_mode)

def _fsync_verified_directory(*, path: Path, expected_identity) -> None:
    """Fsync a no-follow directory descriptor that still names the preflighted directory."""
    if os.name == "nt":
        _open_verified_directory(path=path, expected_identity=expected_identity, fsync=False)
        return
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW_FLAG)
    try:
        actual = _cleanup_directory_identity(os.fstat(fd))
        if not stat.S_ISDIR(os.fstat(fd).st_mode) or not _same_directory_identity(actual, expected_identity): raise OSError("directory changed")
        os.fsync(fd)
    finally:
        os.close(fd)

def _cleanup_directory_record(path: Path, indices: tuple[int, ...]) -> _CleanupDirectoryRecord:
    state = os.lstat(path)
    parent = path.parent
    parent_state = os.lstat(parent)
    if (not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode) or has_symlink_component(path)
            or not stat.S_ISDIR(parent_state.st_mode) or stat.S_ISLNK(parent_state.st_mode)
            or (os.name != "nt" and stat.S_IMODE(state.st_mode) != 0o700)):
        raise OSError("unsafe directory")
    stage_identity = _cleanup_directory_identity(state)
    parent_identity = _cleanup_directory_identity(parent_state)
    _open_verified_directory(path=path, expected_identity=stage_identity, fsync=False)
    _open_verified_directory(path=parent, expected_identity=parent_identity, fsync=False)
    return _CleanupDirectoryRecord(path, state.st_dev, state.st_ino, stat.S_IFMT(state.st_mode), stat.S_IMODE(state.st_mode), parent, parent_state.st_dev, parent_state.st_ino, stat.S_IFMT(parent_state.st_mode), stat.S_IMODE(parent_state.st_mode), getattr(parent_state, "st_mtime_ns", None), indices)

def _same_cleanup_record(record: _CleanupBindingRecord, actual: _CleanupBindingRecord) -> bool:
    return (record.device, record.inode, record.file_type, record.size, record.mtime_ns, record.mode) == (actual.device, actual.inode, actual.file_type, actual.size, actual.mtime_ns, actual.mode)

def _revalidate_cleanup_directory(record: _CleanupDirectoryRecord) -> None:
    actual = _cleanup_directory_record(record.path, record.target_indices)
    if (_stable_cleanup_directory_identity(_record_directory_identity(actual)), actual.parent_path, _stable_cleanup_directory_identity(_record_parent_identity(actual))) != (_stable_cleanup_directory_identity(_record_directory_identity(record)), record.parent_path, _stable_cleanup_directory_identity(_record_parent_identity(record))): raise OSError("directory changed")

def _load_staging_binding(*, stage_directory: Path, expected_payload: dict[str, object]) -> _CleanupBindingRecord:
    binding=stage_directory/_STAGING_BINDING_NAME
    try:
        if not _stage_directory_is_private(stage_directory) or binding.is_symlink(): raise OSError("unsafe")
        record, data = _cleanup_file_record(binding, maximum_size=_MAX_STAGING_BINDING_BYTES)
        if not data: raise OSError("invalid")
        parsed=json.loads(data.decode("utf-8"))
        _validate_binding_schema(parsed)
        if _canonical_json(parsed)!=data or parsed!=expected_payload: raise ValueError("invalid")
        return record
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

def _ready_file_record(path: Path, *, expected_size: int | None = None, expected_sha256: str | None = None) -> _ReadyFileRecord:
    """Bind a private regular file, its name, and its bytes to one no-follow read."""
    fd = None
    try:
        if path.is_symlink() or has_symlink_component(path): raise OSError("unsafe file")
        fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW_FLAG | _BINARY_FLAG)
        before = os.fstat(fd); mode = stat.S_IMODE(before.st_mode) if os.name != "nt" else None
        if not stat.S_ISREG(before.st_mode) or (os.name != "nt" and mode != 0o600): raise OSError("unsafe file")
        if expected_size is not None and before.st_size != expected_size: raise OSError("wrong size")
        named_before = os.lstat(path)
        if _identity(named_before) != _identity(before): raise OSError("file replaced")
        digest = hashlib.sha256(_read_exact(fd, before.st_size)).hexdigest()
        after = os.fstat(fd); named_after = os.lstat(path)
        if _identity(before) != _identity(after) or _identity(before) != _identity(named_after): raise OSError("file changed")
        if expected_sha256 is not None and digest != expected_sha256: raise OSError("wrong digest")
        return _ReadyFileRecord(path, before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode), before.st_size, getattr(before, "st_mtime_ns", None), mode, digest)
    finally:
        if fd is not None: os.close(fd)

def _same_ready_file(left: _ReadyFileRecord, right: _ReadyFileRecord) -> bool:
    return (left.path, left.device, left.inode, left.file_type, left.size, left.mtime_ns, left.mode, left.sha256) == (right.path, right.device, right.inode, right.file_type, right.size, right.mtime_ns, right.mode, right.sha256)

def _ready_directory_record(path: Path, indices: tuple[int, ...], expected_entries: tuple[str, ...]) -> _ReadyDirectoryRecord:
    """Capture a stage directory and its parent without granting pathname races authority."""
    try:
        state, parent_state = os.lstat(path), os.lstat(path.parent)
        if (not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode) or has_symlink_component(path)
                or not stat.S_ISDIR(parent_state.st_mode) or stat.S_ISLNK(parent_state.st_mode)
                or (os.name != "nt" and (stat.S_IMODE(state.st_mode) != 0o700 or stat.S_IMODE(parent_state.st_mode) != 0o700))): raise OSError("unsafe directory")
        identity = _directory_identity(path); parent_identity = _directory_identity(path.parent)
        _open_verified_directory(path=path, expected_identity=identity, fsync=False)
        _open_verified_directory(path=path.parent, expected_identity=parent_identity, fsync=False)
        names=[]
        with os.scandir(path) as scan:
            for child in scan:
                item=os.lstat(child.path)
                if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode): raise OSError("unsafe entry")
                names.append(child.name)
        entries=tuple(sorted(names))
        if entries != tuple(sorted(expected_entries)) or _STAGING_BINDING_TEMP_NAME in entries or any(name.endswith(".partial") for name in entries): raise OSError("unexpected entry")
        return _ReadyDirectoryRecord(path,state.st_dev,state.st_ino,stat.S_IFMT(state.st_mode),stat.S_IMODE(state.st_mode) if os.name != "nt" else None,path.parent,parent_state.st_dev,parent_state.st_ino,stat.S_IFMT(parent_state.st_mode),stat.S_IMODE(parent_state.st_mode) if os.name != "nt" else None,entries,indices)
    except OSError:
        raise

def _same_ready_directory(record: _ReadyDirectoryRecord, actual: _ReadyDirectoryRecord) -> bool:
    return (record.path,record.device,record.inode,record.file_type,record.mode,record.parent_path,record.parent_device,record.parent_inode,record.parent_file_type,record.parent_mode,record.entries,record.target_indices) == (actual.path,actual.device,actual.inode,actual.file_type,actual.mode,actual.parent_path,actual.parent_device,actual.parent_inode,actual.parent_file_type,actual.parent_mode,actual.entries,actual.target_indices)

def _capture_destination_baselines(destinations: tuple[SyntheticRestoreTarget, ...]) -> tuple[_DestinationBaseline, ...]:
    try:
        captured=[]
        for index, destination in enumerate(destinations):
            parent_state=os.lstat(destination.path.parent)
            if not stat.S_ISDIR(parent_state.st_mode) or stat.S_ISLNK(parent_state.st_mode) or has_symlink_component(destination.path.parent): raise OSError("unsafe parent")
            parent_identity=_directory_identity(destination.path.parent)
            _open_verified_directory(path=destination.path.parent, expected_identity=parent_identity, fsync=False)
            file=_ready_file_record(destination.path)
            after_parent=_directory_identity(destination.path.parent)
            if not _same_directory_identity(parent_identity,after_parent): raise OSError("parent changed")
            captured.append(_DestinationBaseline(index,destination.target_key,destination.kind,file,destination.path.parent,parent_state.st_dev,parent_state.st_ino,stat.S_IFMT(parent_state.st_mode),stat.S_IMODE(parent_state.st_mode) if os.name != "nt" else None))
        return tuple(captured)
    except OSError as exc:
        raise SyntheticDestinationError("Synthetic destination is unsafe") from exc

def _revalidate_destination_baselines(baselines: tuple[_DestinationBaseline, ...], destinations: tuple[SyntheticRestoreTarget, ...]) -> None:
    try:
        if len(baselines) != len(destinations): raise OSError("destination set changed")
        for index, (baseline, destination) in enumerate(zip(baselines, destinations)):
            if (baseline.target_order,baseline.target_key,baseline.kind,baseline.file.path) != (index,destination.target_key,destination.kind,destination.path): raise OSError("destination substituted")
            parent_state=os.lstat(destination.path.parent)
            parent=(parent_state.st_dev,parent_state.st_ino,stat.S_IFMT(parent_state.st_mode),stat.S_IMODE(parent_state.st_mode) if os.name != "nt" else None)
            if parent != (baseline.parent_device,baseline.parent_inode,baseline.parent_file_type,baseline.parent_mode): raise OSError("parent changed")
            fresh=_ready_file_record(destination.path,expected_size=baseline.file.size,expected_sha256=baseline.file.sha256)
            if not _same_ready_file(baseline.file,fresh): raise OSError("destination changed")
    except OSError as exc:
        raise SyntheticDestinationError("Synthetic destination changed during staging") from exc

def _final_journal_token(*, operation_id: str, journal_root: Path, validated: ValidatedBackupSnapshot, destinations: tuple[SyntheticRestoreTarget, ...]):
    try:
        journal=load_restore_journal(operation_id,root=journal_root)
    except (RestoreJournalError, RestoreJournalPersistenceError, OSError, ValueError) as exc:
        raise FinalReadinessError("Restore staging journal is invalid") from exc
    if (journal.operation_id != operation_id or journal.stage is not RestoreStage.STAGED_VERIFIED or not journal.safety_backup_id
            or journal.selected_backup_id != validated.backup_id or journal.selected_backup_manifest_sha256 != validated.manifest_sha256
            or journal.runtime_mode != validated.runtime_mode or journal.target_keys != tuple(item.target_key for item in destinations)
            or (journal.expected_application_commit != "unknown" and journal.expected_application_commit != validated.application_commit)
            or len(journal.targets) != len(journal.target_keys) or tuple(item.target_key for item in journal.targets) != journal.target_keys
            or any(item.state is not TargetRestoreState.STAGED_VERIFIED for item in journal.targets) or journal.final_result is not None):
        raise FinalReadinessError("Restore staging journal is invalid")
    return journal

def _selected_backup_directory_identity(path: Path) -> tuple[int, int, int, int | None]:
    state=None; fd=None
    try:
        state=os.lstat(path)
        if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode) or has_symlink_component(path): raise OSError("unsafe backup directory")
        if os.name == "nt": return (state.st_dev,state.st_ino,stat.S_IFMT(state.st_mode),None)
        fd=os.open(str(path),os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|_NOFOLLOW_FLAG)
        actual=os.fstat(fd)
        if (actual.st_dev,actual.st_ino,stat.S_IFMT(actual.st_mode)) != (state.st_dev,state.st_ino,stat.S_IFMT(state.st_mode)) or not stat.S_ISDIR(actual.st_mode): raise OSError("backup directory changed")
        return (actual.st_dev,actual.st_ino,stat.S_IFMT(actual.st_mode),stat.S_IMODE(actual.st_mode))
    finally:
        if fd is not None: os.close(fd)

def _fresh_selected_backup(*, validated: ValidatedBackupSnapshot, journal) -> tuple[ValidatedBackupSnapshot, tuple[tuple[str,str,str|None,str,int,str,str,tuple[str,tuple[str,...],str]], ...], tuple[_ReadyFileRecord, ...], tuple[int, int, int, int | None]]:
    try:
        fresh=load_validated_backup_snapshot(validated.directory)
        if fresh != validated or fresh.backup_id != journal.selected_backup_id or fresh.manifest_sha256 != journal.selected_backup_manifest_sha256 or fresh.runtime_mode != journal.runtime_mode or fresh.target_keys != journal.target_keys: raise OSError("backup changed")
        entries=tuple(_entry_tuple(entry) for entry in fresh.entries)
        if tuple(entry[0] for entry in entries) != journal.target_keys: raise OSError("backup targets changed")
        directory_identity=_selected_backup_directory_identity(fresh.directory)
        records=tuple(_ready_file_record(fresh.directory / entry[3],expected_size=entry[4],expected_sha256=entry[5]) for entry in entries)
        if _selected_backup_directory_identity(fresh.directory) != directory_identity: raise OSError("backup directory changed")
        return fresh,entries,records,directory_identity
    except (Exception,) as exc:
        if isinstance(exc, FinalReadinessError): raise
        raise StagingSourceError("Validated staging source changed") from exc

def _load_final_binding(*, directory: _ReadyDirectoryRecord, expected_payload: dict[str, object]) -> tuple[_ReadyFileRecord, bytes]:
    try:
        record=_ready_file_record(directory.path/_STAGING_BINDING_NAME)
        if record.size > _MAX_STAGING_BINDING_BYTES: raise OSError("binding too large")
        fd=os.open(str(record.path),os.O_RDONLY|_NOFOLLOW_FLAG|_BINARY_FLAG)
        try: data=_read_exact(fd,record.size)
        finally: os.close(fd)
        parsed=json.loads(data.decode("utf-8")); _validate_binding_schema(parsed)
        if _canonical_json(parsed) != data or parsed != expected_payload: raise ValueError("binding mismatch")
        # Reopen after parsing so an equal-content replacement cannot pass.
        after=_ready_file_record(record.path)
        if not _same_ready_file(record,after): raise OSError("binding changed")
        return record,data
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise FinalReadinessError("Synthetic staging binding is invalid") from exc

def _final_revalidate_before_replacement_ready(*, operation_id: str, validated_backup: ValidatedBackupSnapshot, destinations: tuple[SyntheticRestoreTarget,...], fixture_root: Path, journal_root: Path, staged, baselines: tuple[_DestinationBaseline, ...]) -> None:
    """Perform the complete read-only readiness proof and return only after its barrier."""
    journal=_final_journal_token(operation_id=operation_id,journal_root=journal_root,validated=validated_backup,destinations=destinations)
    fresh,entries,source_records,source_directory_identity=_fresh_selected_backup(validated=validated_backup,journal=journal)
    try: root=_validate_fixture_root(Path(fixture_root),fresh.directory)
    except StagingError: raise
    if len(entries) != len(staged) or len(destinations) != len(entries): raise FinalReadinessError("Synthetic staging artifacts are invalid")
    _revalidate_destination_baselines(baselines,destinations)
    groups={}
    for index,destination in enumerate(destinations): groups.setdefault(destination.path.parent,[]).append(index)
    directories=[]; bindings={}; artifacts={}
    try:
        for parent,indices in groups.items():
            stage=_stage_dir(parent,operation_id); expected=(_STAGING_BINDING_NAME,*(_staged_filename(entries[index],index) for index in indices))
            directory=_ready_directory_record(stage,tuple(indices),tuple(expected)); directories.append(directory)
            payload=_binding_payload(journal,entries,destinations,root,parent,tuple(indices))
            bindings[stage]=_load_final_binding(directory=directory,expected_payload=payload)
        for index,(entry,path,staged_index) in enumerate(staged):
            expected=_stage_dir(destinations[index].path.parent,operation_id)/_staged_filename(entries[index],index)
            if staged_index != index or entry != entries[index] or path != expected: raise OSError("artifact substituted")
            record=_ready_file_record(path,expected_size=entry[4],expected_sha256=entry[5]); _verify(entry,path)
            after=_ready_file_record(path,expected_size=entry[4],expected_sha256=entry[5])
            if not _same_ready_file(record,after): raise OSError("artifact changed")
            artifacts[index]=record
        for directory in directories:
            expected=(_STAGING_BINDING_NAME,*(_staged_filename(entries[index],index) for index in directory.target_indices))
            current=_ready_directory_record(directory.path,directory.target_indices,tuple(expected))
            if not _same_ready_directory(directory,current): raise OSError("stage directory changed")
            binding,data=bindings[directory.path]; again,again_data=_load_final_binding(directory=directory,expected_payload=_binding_payload(journal,entries,destinations,root,directory.parent_path,directory.target_indices))
            if not _same_ready_file(binding,again) or data != again_data: raise OSError("binding changed")
        # The final mutation-free barrier intentionally repeats every mutable proof.
        if _final_journal_token(operation_id=operation_id,journal_root=journal_root,validated=validated_backup,destinations=destinations) != journal: raise OSError("journal changed")
        if _selected_backup_directory_identity(fresh.directory) != source_directory_identity: raise OSError("backup directory changed")
        for source in source_records:
            current=_ready_file_record(source.path,expected_size=source.size,expected_sha256=source.sha256)
            if not _same_ready_file(source,current): raise OSError("backup changed")
        _revalidate_destination_baselines(baselines,destinations)
        for directory in directories:
            expected=(_STAGING_BINDING_NAME,*(_staged_filename(entries[index],index) for index in directory.target_indices))
            current=_ready_directory_record(directory.path,directory.target_indices,tuple(expected))
            if not _same_ready_directory(directory,current): raise OSError("stage directory changed")
            binding,data=bindings[directory.path]; again,again_data=_load_final_binding(directory=directory,expected_payload=_binding_payload(journal,entries,destinations,root,directory.parent_path,directory.target_indices))
            if not _same_ready_file(binding,again) or data != again_data: raise OSError("binding changed")
        for index,record in artifacts.items():
            current=_ready_file_record(record.path,expected_size=record.size,expected_sha256=record.sha256)
            if not _same_ready_file(record,current): raise OSError("artifact changed")
    except FinalReadinessError: raise
    except (OSError, StagedVerificationError, ValueError) as exc:
        raise StagedVerificationError(_STAGED_VERIFICATION_ERROR) from exc
    return journal

def _verify_replacement_ready_transition(*, operation_id: str, journal_root: Path, token) -> None:
    try:
        current=load_restore_journal(operation_id,root=journal_root)
        unchanged=("operation_id","selected_backup_id","selected_backup_manifest_sha256","safety_backup_id","expected_application_commit","runtime_mode","target_keys","target_set_hash","confirmation_value","targets","created_at","final_result")
        if current.stage is not RestoreStage.REPLACEMENT_READY or any(getattr(current,name) != getattr(token,name) for name in unchanged) or any(item.state is not TargetRestoreState.STAGED_VERIFIED for item in current.targets): raise OSError("wrong journal state")
        if datetime.fromisoformat(current.updated_at.replace("Z","+00:00")) < datetime.fromisoformat(token.updated_at.replace("Z","+00:00")): raise OSError("journal time moved backward")
    except (RestoreJournalError, RestoreJournalPersistenceError, OSError, ValueError) as exc:
        raise FinalReadinessError("Restore staging journal transition is invalid") from exc

def stage_and_verify_synthetic_restore(*,operation_id: str,validated_backup: ValidatedBackupSnapshot,destinations: tuple[SyntheticRestoreTarget,...],fixture_root: Path,journal_root: Path)->StagingResult:
    try:
        journal,entries,root=_validate_inputs(operation_id,validated_backup,destinations,fixture_root,journal_root)
        # This precedes every staging mutation and is held only in memory.
        destination_baselines=_capture_destination_baselines(destinations)
        try:
            update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.RESTORE_STAGED)
        except (RestoreJournalError, RestoreJournalPersistenceError) as exc:
            raise _journal_failure(operation_id=operation_id,journal_root=journal_root) from exc
        parent_indices={}
        for index,d in enumerate(destinations): parent_indices.setdefault(d.path.parent,[]).append(index)
        dirs={}
        for parent,indices in parent_indices.items():
            stage=_stage_dir(parent,operation_id)
            created=False; stage_identity=None; parent_identity=None
            try: os.lstat(stage)
            except FileNotFoundError: pass
            else: raise StagingPersistenceError("Synthetic staging directory already exists")
            try:
                stage.mkdir(mode=0o700); created=True; _private(stage,True)
                if not _stage_directory_is_private(stage) or (os.name!="nt" and permission_health(stage,directory=True)!="private"): raise OSError("unsafe stage")
                stage_identity=_directory_identity(stage); parent_identity=_directory_identity(parent)
                _write_staging_binding(stage_directory=stage,stage_identity=stage_identity,parent_identity=parent_identity,payload=_binding_payload(journal,entries,destinations,root,parent,tuple(indices)))
            except StagingManualCleanupRequiredError: raise
            except StagingBindingPersistenceError: raise
            except (OSError, StagingPersistenceError) as exc:
                if created and stage_identity is not None and parent_identity is not None: _compensate_unpublished_binding(stage_directory=stage,stage_identity=stage_identity,parent_identity=parent_identity,temporary=stage/_STAGING_BINDING_TEMP_NAME)
                elif created: raise StagingManualCleanupRequiredError("Unrecorded staging artifact requires manual cleanup") from exc
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
            baseline = destination_baselines[index].file
            artifacts.append(StagedArtifact(operation_id,entry[0],entry[1],index,file,entry[4],entry[5],entry[6],entry[7],baseline.size,baseline.sha256))
        try:
            journal_token=_final_revalidate_before_replacement_ready(operation_id=operation_id,validated_backup=validated_backup,destinations=destinations,fixture_root=fixture_root,journal_root=journal_root,staged=staged,baselines=destination_baselines)
            update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.REPLACEMENT_READY)
            _verify_replacement_ready_transition(operation_id=operation_id,journal_root=journal_root,token=journal_token)
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
    except (StagingError,RestoreJournalError,RestoreJournalPersistenceError,OSError,ValueError,TypeError) as exc: raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid") from exc
    groups={}
    for index,destination in enumerate(destinations): groups.setdefault(destination.path.parent,[]).append(index)
    prepared: list[_CleanupStagePlan]=[]
    try:
        for parent,indices in groups.items():
            stage=_stage_dir(parent,operation_id)
            try: os.lstat(stage)
            except FileNotFoundError: continue
            directory = _cleanup_directory_record(stage, tuple(indices))
            payload=_binding_payload(journal,entries,destinations,root,parent,tuple(indices)); binding = _load_staging_binding(stage_directory=stage,expected_payload=payload)
            allowed={_STAGING_BINDING_NAME,*(_staged_filename(entries[index],index) for index in indices)}
            names=[]
            with os.scandir(stage) as children:
                for child in children:
                    item = Path(child.path); names.append(child.name)
                    state=os.lstat(item)
                    if child.name not in allowed or stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode): raise OSError("unexpected entry")
            if _STAGING_BINDING_NAME not in names: raise OSError("binding absent")
            present=[]
            for index in indices:
                artifact=stage/_staged_filename(entries[index],index)
                if artifact.name not in names: continue
                record, _ = _cleanup_file_record(artifact, expected_size=entries[index][4], expected_sha256=entries[index][5])
                present.append(_CleanupArtifactRecord(index, artifact, record.device, record.inode, record.file_type, record.size, record.mtime_ns, record.mode, entries[index][5]))
            _revalidate_cleanup_directory(directory)
            prepared.append(_CleanupStagePlan(directory,binding,tuple(present)))
    except StagingCleanupBindingError: raise
    except (OSError,ValueError,StagingError,UnicodeError,json.JSONDecodeError) as exc: raise StagingCleanupBindingError("Synthetic staging cleanup binding is invalid") from exc
    try:
        for plan in prepared:
            directory=plan.directory
            for artifact in plan.artifacts:
                _revalidate_cleanup_directory(directory)
                current, _ = _cleanup_file_record(artifact.path, expected_size=artifact.size, expected_sha256=artifact.expected_sha256)
                expected = _CleanupBindingRecord(artifact.path,artifact.device,artifact.inode,artifact.file_type,artifact.size,artifact.mtime_ns,artifact.mode,b"")
                if not _same_cleanup_record(expected,current): raise OSError("artifact changed")
                _revalidate_cleanup_directory(directory)
                os.unlink(artifact.path)
            _revalidate_cleanup_directory(directory)
            current, data = _cleanup_file_record(plan.binding.path, maximum_size=_MAX_STAGING_BINDING_BYTES)
            if not _same_cleanup_record(plan.binding,current) or data != plan.binding.canonical_bytes: raise OSError("binding changed")
            _revalidate_cleanup_directory(directory)
            os.unlink(plan.binding.path)
            _revalidate_cleanup_directory(directory)
            # The directory is now allowed to contain no entries, and only then may it be removed.
            with os.scandir(directory.path) as children:
                if next(children, None) is not None: raise OSError("directory not empty")
            _fsync_verified_directory(path=directory.path, expected_identity=_record_directory_identity(directory))
            _revalidate_cleanup_directory(directory)
            directory.path.rmdir()
            _fsync_verified_directory(path=directory.parent_path, expected_identity=_record_parent_identity(directory))
    except OSError as exc: raise StagingCleanupPersistenceError("Synthetic staging cleanup could not be persisted") from exc
