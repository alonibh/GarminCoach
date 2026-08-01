"""Explicit, atomic verified SQLite backups. Phase 6A has no restore mutation."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib, json, os
from pathlib import Path
import re, secrets, shutil, sqlite3, subprocess, sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import config
from operator_storage import (DatabaseIntegrityError, DatabaseTarget, TargetProfile,
    active_user_target_mapping, canonical_user_id, discover_database_targets,
    has_symlink_component, inspect_sqlite, migration_markers, safe_resolve, schema_fingerprint)

BACKUP_FORMAT="garmincoach-backup-v1"; _ID=re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$"); _HEX=re.compile(r"^[0-9a-f]{64}$")
class BackupError(RuntimeError): pass
@dataclass(frozen=True)
class ValidatedBackup:
    manifest: dict[str,Any]
    directory: Path
    entries: tuple[dict[str,Any], ...]
    compatible: bool | None = None

def _now()->str: return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def canonical_json(v:object)->bytes: return (json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n").encode("utf-8")
def _sha(p:Path)->str:
    try:
        h=hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
        return h.hexdigest()
    except OSError as exc: raise BackupError("Backup file cannot be read") from exc
def _private(p:Path,directory=False)->None:
    try: os.chmod(p,0o700 if directory else 0o600)
    except OSError:
        if os.name!="nt": raise BackupError("Could not set private backup permissions")
def _fsync(p:Path,directory=False)->None:
    if os.name=="nt": return
    try:
        fd=os.open(p,os.O_RDONLY|(getattr(os,"O_DIRECTORY",0) if directory else 0)); os.fsync(fd); os.close(fd)
    except OSError: pass
def _runtime_version()->str:
    try: return version("garminconnect")
    except PackageNotFoundError as exc: raise BackupError("Required runtime distribution is unavailable") from exc
def _timestamp(value:object)->datetime:
    if not isinstance(value,str): raise BackupError("Backup timestamp is invalid")
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset()!=timezone.utc.utcoffset(parsed): raise ValueError
        return parsed
    except ValueError as exc: raise BackupError("Backup timestamp is invalid") from exc
def _commit()->str:
    try: return subprocess.check_output(["git","rev-parse","HEAD"],cwd=config.PROJECT_ROOT,text=True,stderr=subprocess.DEVNULL,timeout=5).strip()
    except (OSError,subprocess.SubprocessError): return "unknown"

class BackupLock:
    def __init__(self,root:Path): self.path=root/".garmincoach-backup.lock"; self.handle=None
    def __enter__(self):
        self.handle=self.path.open("a+b"); _private(self.path)
        try:
            if os.name=="nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1): self.handle.write(b"\0"); self.handle.flush()
                self.handle.seek(0); msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl; fcntl.flock(self.handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc: self.handle.close(); self.handle=None; raise BackupError("Another verified backup is active") from exc
        return self
    def __exit__(self,*_):
        if not self.handle:return
        try:
            if os.name=="nt":
                import msvcrt; self.handle.seek(0); msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl; fcntl.flock(self.handle,fcntl.LOCK_UN)
        finally:self.handle.close();self.handle=None

def validate_backup_root(root:Path|str|None=None)->Path:
    original=Path(root or config.OPERATOR_BACKUP_ROOT).expanduser(); original=original if original.is_absolute() else Path(config.PROJECT_ROOT)/original
    if has_symlink_component(original): raise BackupError("Backup root may not be symlinked")
    try: selected=original.resolve(strict=False); tenant_root=safe_resolve(config.MULTI_USER_DATA_ROOT)
    except ValueError as exc: raise BackupError("Backup root configuration is unsafe") from exc
    if selected==tenant_root or tenant_root in selected.parents: raise BackupError("Backup root cannot equal or be inside tenant storage")
    try: targets=discover_database_targets(profile=TargetProfile.RUNTIME)
    except ValueError as exc: raise BackupError("Canonical database discovery failed") from exc
    for t in targets:
        if selected==t.path or selected in t.path.parents or t.path in selected.parents: raise BackupError("Backup root cannot overlap a configured database")
    return selected

def _filename(index:int,t:DatabaseTarget)->str:
    return f"{index:03d}-{'control' if t.kind=='control' else 'single-user' if t.kind=='single_user' else 'tenant-'+str(t.tenant_id)}.sqlite"
def create_verified_backup(output_root:Path|str|None=None)->Path:
    root=validate_backup_root(output_root); root.mkdir(parents=True,exist_ok=True); _private(root,True)
    backup_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+secrets.token_hex(4); staging=root/f".partial-{backup_id}"; final=root/f"backup-{backup_id}"
    if staging.exists() or final.exists(): raise BackupError("Backup identity collision")
    with BackupLock(root):
        try:
            staging.mkdir(mode=0o700); _private(staging,True); started=_now(); targets=discover_database_targets(profile=TargetProfile.RUNTIME)
            control=next(t for t in targets if t.kind=="control"); before=active_user_target_mapping(control.path)
            selected=tuple(t for t in targets if t.path.exists())
            if any(t.required and not t.path.exists() for t in targets) or any(key not in {t.target_key for t in selected} for _,key in before): raise BackupError("A required source database is missing")
            for t in selected:
                check=inspect_sqlite(t.path)
                if not check.readable or not check.quick_check_ok: raise DatabaseIntegrityError("Source database failed read-only integrity inspection")
            entries=[]
            for index,t in enumerate(selected):
                name=_filename(index,t); dest=staging/name; snapshot_started=_now(); source=sqlite3.connect(f"file:{t.path.as_posix()}?mode=ro",uri=True,timeout=30); target=sqlite3.connect(dest,timeout=30)
                try: source.backup(target,pages=256)
                finally: target.close();source.close()
                _private(dest); check=inspect_sqlite(dest,deep=True)
                if check.integrity_check_ok is not True: raise BackupError("Backup destination integrity verification failed")
                entries.append({"target_key":t.target_key,"kind":t.kind,"tenant_id":t.tenant_id,"filename":name,"size_bytes":dest.stat().st_size,"sha256":_sha(dest),"integrity_check":"ok","schema_fingerprint":schema_fingerprint(dest),"migration_markers":migration_markers(dest,t.kind),"snapshot_started_at":snapshot_started,"snapshot_completed_at":_now()});_fsync(dest)
            after=active_user_target_mapping(control.path)
            if before!=after: raise BackupError("Control-user target mapping changed during backup")
            runtime_mode="multi_user" if config.MULTI_USER_ENABLED else "single_user"
            m={"format_version":BACKUP_FORMAT,"backup_id":backup_id,"status":"complete","runtime_mode":runtime_mode,"runtime_target_keys":[t.target_key for t in selected],"started_at":started,"completed_at":_now(),"application_commit":_commit(),"python_version":sys.version.split()[0],"garminconnect_version":_runtime_version(),"database_count":len(entries),"control_user_tenant_mapping_before":[{"user_id":u,"target_key":k} for u,k in before],"control_user_tenant_mapping_after":[{"user_id":u,"target_key":k} for u,k in after],"databases":entries}
            data=canonical_json(m); (staging/"manifest.json").write_bytes(data); (staging/"manifest.sha256").write_text(hashlib.sha256(data).hexdigest()+"  manifest.json\n",encoding="ascii")
            for p in (staging/"manifest.json",staging/"manifest.sha256"):_private(p);_fsync(p)
            _fsync(staging,True);staging.replace(final);_private(final,True);_fsync(root,True);return final
        except Exception:
            if staging.exists(): shutil.rmtree(staging)
            raise

_TOP={"format_version","backup_id","status","runtime_mode","runtime_target_keys","started_at","completed_at","application_commit","python_version","garminconnect_version","database_count","control_user_tenant_mapping_before","control_user_tenant_mapping_after","databases"}
_ENTRY={"target_key","kind","tenant_id","filename","size_bytes","sha256","integrity_check","schema_fingerprint","migration_markers","snapshot_started_at","snapshot_completed_at"}
def _mapping(value:object)->set[str]:
    if not isinstance(value,list): raise BackupError("Backup control-user mapping is invalid")
    keys=set(); users=set()
    for item in value:
        if not isinstance(item,dict) or set(item)!={"user_id","target_key"} or not isinstance(item["user_id"],str) or not isinstance(item["target_key"],str): raise BackupError("Backup control-user mapping is invalid")
        user=canonical_user_id(item["user_id"]); key=item["target_key"]
        if key!=f"tenant:{user}" or user in users or key in keys: raise BackupError("Backup control-user mapping is invalid")
        users.add(user);keys.add(key)
    return keys
def _markers(value:object,kind:str)->None:
    ledger="migration_versions" if kind=="control" else "app_migrations"
    if not isinstance(value,dict) or set(value)!={"ledger","keys","state"} or value.get("ledger")!=ledger or value.get("state") not in {"present","absent"} or not isinstance(value.get("keys"),list): raise BackupError("Backup migration markers are invalid")
    keys=value["keys"]
    if any(not isinstance(k,str) for k in keys) or keys!=sorted(set(keys)) or (value["state"]=="absent" and keys): raise BackupError("Backup migration markers are invalid")
def _read(directory:Path)->dict[str,Any]:
    if has_symlink_component(directory) or directory.is_symlink() or not directory.is_dir(): raise BackupError("Backup directory is not a safe directory")
    manifest,checksum=directory/"manifest.json",directory/"manifest.sha256"
    try:
        if any(has_symlink_component(p) or p.is_symlink() or not p.is_file() for p in (manifest,checksum)): raise BackupError("Backup manifest is missing")
        raw=manifest.read_bytes(); m=json.loads(raw.decode("utf-8")); supplied=checksum.read_text(encoding="ascii")
    except (OSError,UnicodeError,json.JSONDecodeError) as exc: raise BackupError("Backup manifest is invalid") from exc
    if not isinstance(m,dict) or canonical_json(m)!=raw or supplied!=hashlib.sha256(raw).hexdigest()+"  manifest.json\n": raise BackupError("Backup manifest checksum failed")
    return m
def _strict(directory:Path)->ValidatedBackup:
    m=_read(directory)
    if set(m)!=_TOP or m.get("format_version")!=BACKUP_FORMAT or m.get("status")!="complete" or m.get("runtime_mode") not in {"single_user","multi_user"} or not isinstance(m.get("runtime_target_keys"),list) or not isinstance(m.get("backup_id"),str) or not _ID.fullmatch(m["backup_id"]) or directory.name!=f"backup-{m['backup_id']}": raise BackupError("Backup manifest semantics are invalid")
    start,end=_timestamp(m["started_at"]),_timestamp(m["completed_at"])
    if end<start or not all(isinstance(m[k],str) and m[k] for k in ("application_commit","python_version","garminconnect_version")) or not re.fullmatch(r"\d+(?:\.\d+)+(?:[A-Za-z0-9.+-]*)",m["garminconnect_version"]) or not isinstance(m["database_count"],int) or m["database_count"]<=0 or not isinstance(m["databases"],list) or len(m["databases"])!=m["database_count"]: raise BackupError("Backup manifest semantics are invalid")
    before,after=_mapping(m["control_user_tenant_mapping_before"]),_mapping(m["control_user_tenant_mapping_after"])
    if before!=after: raise BackupError("Backup control-user mapping changed")
    keys=set(); names=set(); tenant_keys=set(); control_count=0
    for i,e in enumerate(m["databases"]):
        if not isinstance(e,dict) or set(e)!=_ENTRY or not isinstance(e.get("target_key"),str) or not isinstance(e.get("kind"),str) or not isinstance(e.get("filename"),str) or not isinstance(e.get("size_bytes"),int) or e["size_bytes"]<0 or e.get("integrity_check")!="ok" or not all(isinstance(e.get(k),str) and _HEX.fullmatch(e[k]) for k in ("sha256","schema_fingerprint")): raise BackupError("Backup database entry is invalid")
        key,kind,name=e["target_key"],e["kind"],e["filename"]
        if key in keys or name in names or Path(name).name!=name: raise BackupError("Backup database entry is invalid")
        keys.add(key);names.add(name);_markers(e["migration_markers"],kind); ss,se=_timestamp(e["snapshot_started_at"]),_timestamp(e["snapshot_completed_at"])
        if se<ss or ss<start or se>end: raise BackupError("Backup snapshot timestamps are invalid")
        if kind=="control":
            if key!="control" or e["tenant_id"] is not None or i!=0: raise BackupError("Backup control target is invalid")
            control_count+=1
        elif kind=="single_user":
            if key!="single-user" or e["tenant_id"] is not None: raise BackupError("Backup single-user target is invalid")
        elif kind=="tenant":
            if not isinstance(e["tenant_id"],str) or key!=f"tenant:{canonical_user_id(e['tenant_id'])}": raise BackupError("Backup tenant target is invalid")
            tenant_keys.add(key)
        else: raise BackupError("Backup target kind is invalid")
        identity="control" if kind=="control" else "single-user" if kind=="single_user" else f"tenant-{e['tenant_id']}"
        if name!=f"{i:03d}-{identity}.sqlite": raise BackupError("Backup filename is invalid")
    entry_keys=[e["target_key"] for e in m["databases"]]
    if m["runtime_target_keys"] != entry_keys or len(entry_keys) != len(set(entry_keys)) or control_count!=1 or not before.issubset(tenant_keys): raise BackupError("Backup set is incomplete")
    if m["runtime_mode"]=="single_user":
        if entry_keys != ["control","single-user"] or before or tenant_keys: raise BackupError("Single-user backup set is incomplete")
    elif "single-user" in entry_keys:
        raise BackupError("Multi-user backup contains a single-user target")
    listed={"manifest.json","manifest.sha256",*names}
    try: unexpected=[p.name for p in directory.iterdir() if p.name not in listed and (p.suffix==".sqlite" or p.name.startswith("manifest"))]
    except OSError as exc: raise BackupError("Backup directory cannot be read") from exc
    if unexpected: raise BackupError("Backup contains unexpected files")
    for e in m["databases"]:
        try:
            file=directory/e["filename"]
            if has_symlink_component(file) or file.is_symlink() or not file.is_file() or file.stat().st_size!=e["size_bytes"] or _sha(file)!=e["sha256"]: raise BackupError("Backup file checksum or size failed")
            if inspect_sqlite(file,deep=True).integrity_check_ok is not True or schema_fingerprint(file)!=e["schema_fingerprint"] or migration_markers(file,e["kind"])!=e["migration_markers"]: raise BackupError("Backup SQLite verification failed")
        except (DatabaseIntegrityError,OSError,ValueError) as exc: raise BackupError("Backup SQLite verification failed") from exc
    return ValidatedBackup(m,directory,tuple(m["databases"]))

def _compatible(validated: ValidatedBackup) -> ValidatedBackup:
    try: current=discover_database_targets(profile=TargetProfile.RUNTIME)
    except (ValueError,OSError) as exc: raise BackupError("Current configuration is invalid") from exc
    current_by_key={t.target_key:t for t in current}; manifest=validated.manifest
    expected_mode="multi_user" if config.MULTI_USER_ENABLED else "single_user"
    if manifest["runtime_mode"]!=expected_mode or manifest["runtime_target_keys"] != [t.target_key for t in current] or manifest["garminconnect_version"]!=_runtime_version(): raise BackupError("Backup is incompatible with current configuration")
    if expected_mode=="multi_user":
        control=current_by_key.get("control")
        if control is None or set(active_user_target_mapping(control.path)) != {(item["user_id"],item["target_key"]) for item in manifest["control_user_tenant_mapping_before"]}: raise BackupError("Backup is incompatible with current configuration")
    for entry in validated.entries:
        target=current_by_key.get(entry["target_key"])
        if target is None or (target.required and not target.path.exists()): raise BackupError("Backup is incompatible with current configuration")
        check=inspect_sqlite(target.path)
        if not check.readable or not check.quick_check_ok or schema_fingerprint(target.path)!=entry["schema_fingerprint"] or migration_markers(target.path,target.kind)!=entry["migration_markers"]: raise BackupError("Backup is incompatible with current configuration")
    return ValidatedBackup(manifest,validated.directory,validated.entries,True)

def verify_verified_backup(directory:Path|str,*,against_current_config=False)->dict[str,Any]:
    try:
        validated=_strict(Path(directory))
        result={"backup_id":validated.manifest["backup_id"],"completed_at":validated.manifest["completed_at"],"entries":validated.entries,"backup_integrity_valid":True,"verified":True}
        if against_current_config:
            validated=_compatible(validated)
            current={t.target_key:t for t in discover_database_targets(profile=TargetProfile.RUNTIME)}
            result["compatible_with_current_configuration"]=True;result["configured_destinations"]={k:str(v.path) for k,v in current.items()}
        return result
    except BackupError: raise
    except (DatabaseIntegrityError, ValueError, OSError, sqlite3.Error, UnicodeError, PackageNotFoundError) as exc:
        raise BackupError("Backup verification failed") from exc
def restore_plan(directory:Path|str,*,against_current_config=False)->dict[str,Any]:
    try: validated=_strict(Path(directory)); validated=_compatible(validated) if against_current_config else validated; current={t.target_key:t for t in discover_database_targets(profile=TargetProfile.RUNTIME)}
    except (BackupError,DatabaseIntegrityError,ValueError,OSError) as exc: raise exc if isinstance(exc,BackupError) else BackupError("Backup verification failed") from exc
    return {"format_version":"garmincoach-restore-plan-v1","restorable":False,"reason":"Phase 6A verification only","operations":[{"target_key":e["target_key"],"backup_file":e["filename"],"configured_destination":str(current[e["target_key"]].path) if e["target_key"] in current else None,"action":"would_replace"} for e in validated.entries]}
