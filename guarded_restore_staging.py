"""Synthetic-fixture-only offline guarded-restore staging (Phase 6B2B)."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib, os, shutil
from pathlib import Path
from typing import Literal

import config
from guarded_restore import RestoreJournalError, RestoreStage, TargetRestoreState, load_restore_journal, update_restore_journal
from operator_storage import has_symlink_component, inspect_sqlite, migration_markers, permission_health, schema_fingerprint
from verified_backup import ValidatedBackup

class StagingError(RuntimeError): pass
class StagingSourceError(StagingError): pass
class SyntheticDestinationError(StagingError): pass
class StagedVerificationError(StagingError): pass
class StagingPersistenceError(StagingError): pass

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
def _validate_fixture_root(root: Path, backup: Path) -> Path:
    if not root.exists() or not root.is_dir() or root.is_symlink() or has_symlink_component(root): raise SyntheticDestinationError("Synthetic fixture root is unsafe")
    resolved=root.resolve()
    protected=(Path(config.CONTROL_DB_PATH).resolve(),Path(config.DB_PATH).resolve(),Path(config.MULTI_USER_DATA_ROOT).resolve(),Path(config.OPERATOR_BACKUP_ROOT).resolve(),Path(config.OPERATOR_RESTORE_ROOT).resolve(),backup.resolve())
    if any(resolved==p or resolved in p.parents or p in resolved.parents for p in protected): raise SyntheticDestinationError("Synthetic fixture root is unsafe")
    if os.name!="nt" and permission_health(resolved,directory=True)!="private": raise SyntheticDestinationError("Synthetic fixture root is unsafe")
    return resolved
def _entry_tuple(entry: dict) -> tuple[str,str,str|None,str,int,str,str,tuple[str,tuple[str,...],str]]:
    markers=entry["migration_markers"]
    return (entry["target_key"],entry["kind"],entry["tenant_id"],entry["filename"],entry["size_bytes"],entry["sha256"],entry["schema_fingerprint"],(markers["ledger"],tuple(markers["keys"]),markers["state"]))
def _validate_inputs(operation_id: str, validated: ValidatedBackup, destinations: tuple[SyntheticRestoreTarget,...], fixture_root: Path, journal_root: Path):
    journal=load_restore_journal(operation_id,root=journal_root)
    if journal.stage is not RestoreStage.CURRENT_SNAPSHOT_CREATED or not journal.safety_backup_id: raise StagingError("Restore journal is not ready for synthetic staging")
    manifest=validated.manifest
    raw=(validated.directory/"manifest.json").read_bytes(); manifest_hash=hashlib.sha256(raw).hexdigest()
    if (manifest["backup_id"]!=journal.selected_backup_id or manifest_hash!=journal.selected_backup_manifest_sha256 or manifest["runtime_mode"]!=journal.runtime_mode or tuple(manifest["runtime_target_keys"])!=journal.target_keys or (journal.expected_application_commit!="unknown" and manifest["application_commit"]!=journal.expected_application_commit)):
        raise StagingSourceError("Validated backup does not match restore journal")
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
    if source.is_symlink() or has_symlink_component(source) or not source.is_file() or source.stat().st_size!=size or _sha(source)!=digest: raise StagingSourceError("Validated staging source changed")
    identity="control" if kind=="control" else "single-user" if kind=="single_user" else f"tenant-{tenant}"
    final=stage/f"{index:03d}-{identity}.sqlite.staged"; partial=stage/f".{index:03d}-{identity}.partial"
    if final.exists() or partial.exists() or final.is_symlink() or partial.is_symlink(): raise StagingPersistenceError("Synthetic staging artifact already exists")
    try:
        fd=os.open(str(partial),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        h=hashlib.sha256(); count=0
        with source.open("rb") as inp, os.fdopen(fd,"wb") as out:
            for chunk in iter(lambda:inp.read(1024*1024),b""):
                h.update(chunk); count+=len(chunk); out.write(chunk)
            out.flush(); os.fsync(out.fileno())
        _private(partial)
        if count!=size or h.hexdigest()!=digest: raise StagingSourceError("Validated staging source changed")
        os.replace(partial,final); _private(final); return final
    except StagingError: raise
    except OSError as exc: raise StagingPersistenceError("Synthetic staging copy failed") from exc
    finally:
        if partial.exists():
            try: partial.unlink()
            except OSError: pass
def _verify(entry, staged: Path):
    key,kind,tenant,_,size,digest,fingerprint,markers=entry
    if staged.is_symlink() or has_symlink_component(staged) or not staged.is_file() or staged.stat().st_size!=size or _sha(staged)!=digest: raise StagedVerificationError("Staged artifact verification failed")
    if os.name!="nt" and permission_health(staged)!="private": raise StagedVerificationError("Staged artifact verification failed")
    inspected=inspect_sqlite(staged,deep=True)
    actual= migration_markers(staged,kind)
    if not inspected.readable or not inspected.quick_check_ok or not inspected.integrity_check_ok or not inspected.foreign_keys_ok or schema_fingerprint(staged)!=fingerprint or (actual["ledger"],tuple(actual["keys"]),actual["state"])!=markers: raise StagedVerificationError("Staged artifact verification failed")
def stage_and_verify_synthetic_restore(*,operation_id: str,validated_backup: ValidatedBackup,destinations: tuple[SyntheticRestoreTarget,...],fixture_root: Path,journal_root: Path)->StagingResult:
    try:
        journal,entries,root=_validate_inputs(operation_id,validated_backup,destinations,fixture_root,journal_root)
        update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.RESTORE_STAGED)
        dirs={}
        for d in destinations:
            parent=d.path.parent; stage=_stage_dir(parent,operation_id)
            if parent not in dirs:
                if stage.exists() or stage.is_symlink(): raise StagingPersistenceError("Synthetic staging directory already exists")
                stage.mkdir(mode=0o700); _private(stage,True); dirs[parent]=stage
        staged=[]
        for index,(entry,dest) in enumerate(zip(entries,destinations)):
            file=_copy(entry,validated_backup.directory,dirs[dest.path.parent],index)
            update_restore_journal(operation_id,root=journal_root,target_key=entry[0],target_state=TargetRestoreState.STAGED)
            staged.append((entry,file,index))
        update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.STAGED_VERIFIED)
        artifacts=[]
        for entry,file,index in staged:
            _verify(entry,file); update_restore_journal(operation_id,root=journal_root,target_key=entry[0],target_state=TargetRestoreState.STAGED_VERIFIED)
            artifacts.append(StagedArtifact(operation_id,entry[0],entry[1],index,file,entry[4],entry[5],entry[6],entry[7]))
        update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.REPLACEMENT_READY)
        return StagingResult(operation_id,tuple(artifacts))
    except (StagingError,RestoreJournalError):
        try:
            current=load_restore_journal(operation_id,root=journal_root)
            if current.stage not in {RestoreStage.FAILED_SAFE,RestoreStage.REPLACING,RestoreStage.REPLACED,RestoreStage.POSTCHECK_PASSED}: update_restore_journal(operation_id,root=journal_root,stage=RestoreStage.FAILED_SAFE)
        except Exception: pass
        raise
def cleanup_synthetic_staging(*,operation_id: str,destinations: tuple[SyntheticRestoreTarget,...],fixture_root: Path,journal_root: Path)->None:
    journal=load_restore_journal(operation_id,root=journal_root)
    if journal.stage is not RestoreStage.FAILED_SAFE: raise StagingError("Synthetic staging cleanup is not permitted")
    root=_validate_fixture_root(Path(fixture_root),Path(config.OPERATOR_BACKUP_ROOT))
    for parent in {d.path.parent for d in destinations}:
        stage=_stage_dir(parent,operation_id)
        if not stage.exists(): continue
        if stage.is_symlink() or not _inside(stage,root): raise StagingError("Synthetic staging cleanup is unsafe")
        expected={f"{i:03d}-{'control' if d.kind=='control' else 'single-user' if d.kind=='single_user' else 'tenant-'+d.target_key[7:]}.sqlite.staged" for i,d in enumerate(destinations) if d.path.parent==parent}
        names={p.name for p in stage.iterdir()}
        if not names.issubset(expected): raise StagingError("Synthetic staging cleanup is unsafe")
        for p in stage.iterdir():
            if p.is_symlink() or not p.is_file(): raise StagingError("Synthetic staging cleanup is unsafe")
            p.unlink()
        stage.rmdir()
