"""Explicit verified online SQLite backups; Phase 6A contains no restore path."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

import config
from operator_storage import (
    DatabaseIntegrityError, DatabaseTarget, discover_database_targets,
    active_user_target_mapping, inspect_sqlite, migration_markers, schema_fingerprint,
)

BACKUP_FORMAT = "garmincoach-backup-v1"


class BackupError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> bytes:
    """UTF-8, sorted keys, compact separators, one trailing LF."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private(path: Path, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt":
            raise BackupError("Could not set private backup permissions")


def _fsync(path: Path, directory: bool = False) -> None:
    if os.name == "nt":
        return
    try:
        flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if directory else 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class BackupLock:
    def __init__(self, root: Path):
        self.path = root / ".garmincoach-backup.lock"
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        _private(self.path)
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if self.handle.read(1) == b"":
                    self.handle.write(b"\0"); self.handle.flush()
                self.handle.seek(0); msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close(); self.handle = None
            raise BackupError("Another verified backup is active") from exc
        return self

    def __exit__(self, *_exc):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0); msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close(); self.handle = None


def validate_backup_root(root: Path | str | None = None) -> Path:
    selected = Path(root or config.OPERATOR_BACKUP_ROOT).expanduser()
    selected = selected if selected.is_absolute() else config.PROJECT_ROOT / selected
    selected = selected.resolve(strict=False)
    targets = discover_database_targets()
    for target in targets:
        if selected == target.path or selected in target.path.parents or target.path in selected.parents:
            raise BackupError("Backup root cannot overlap a configured database path")
    tenant_root = Path(config.MULTI_USER_DATA_ROOT).resolve(strict=False)
    if tenant_root in selected.parents:
        raise BackupError("Backup root cannot be inside a tenant directory")
    return selected


def _commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config.PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except Exception:
        return "unknown"


def create_verified_backup(output_root: Path | str | None = None) -> Path:
    root = validate_backup_root(output_root)
    root.mkdir(parents=True, exist_ok=True); _private(root, directory=True)
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    staging = root / f".partial-{backup_id}"
    final = root / f"backup-{backup_id}"
    if final.exists() or staging.exists():
        raise BackupError("Backup identity collision")
    with BackupLock(root):
        started = _now()
        try:
            staging.mkdir(mode=0o700); _private(staging, directory=True)
            targets = discover_database_targets()
            control = next(target for target in targets if target.kind == "control")
            before = active_user_target_mapping(control.path)
            selected = tuple(target for target in targets if target.path.exists())
            if any(target.required and not target.path.exists() for target in targets):
                raise BackupError("A required source database is missing")
            selected_keys = {target.target_key for target in selected}
            if any(target_key not in selected_keys for _user_id, target_key in before):
                raise BackupError("An active user database is missing")
            for target in selected:
                check = inspect_sqlite(target.path)
                if not check.readable or not check.quick_check_ok:
                    raise DatabaseIntegrityError("Source database failed read-only integrity inspection")
            entries: list[dict[str, Any]] = []
            for index, target in enumerate(selected):
                filename = f"{index:03d}-{'control' if target.kind == 'control' else 'single-user' if target.kind == 'single_user' else 'tenant-' + str(target.tenant_id)}.sqlite"
                destination = staging / filename
                snapshot_started = _now()
                source = sqlite3.connect(f"file:{target.path.as_posix()}?mode=ro", uri=True, timeout=30)
                dest = sqlite3.connect(destination, timeout=30)
                try:
                    source.backup(dest, pages=256)
                finally:
                    dest.close(); source.close()
                _private(destination)
                check = inspect_sqlite(destination, deep=True)
                if check.integrity_check_ok is not True:
                    raise BackupError("Backup destination integrity verification failed")
                entries.append({"target_key": target.target_key, "kind": target.kind, "tenant_id": target.tenant_id,
                    "filename": filename, "size_bytes": destination.stat().st_size, "sha256": _sha256(destination),
                    "integrity_check": "ok", "schema_fingerprint": schema_fingerprint(destination),
                    "migration_markers": migration_markers(destination, target.kind), "snapshot_started_at": snapshot_started,
                    "snapshot_completed_at": _now()})
                _fsync(destination)
            after = active_user_target_mapping(control.path)
            if before != after:
                raise BackupError("Control-user target mapping changed during backup")
            manifest: dict[str, Any] = {"format_version": BACKUP_FORMAT, "backup_id": backup_id, "status": "complete",
                "started_at": started, "completed_at": _now(), "application_commit": _commit(),
                "python_version": sys.version.split()[0], "garminconnect_version": "0.3.7", "database_count": len(entries),
                "control_user_tenant_mapping_before": [dict(user_id=user_id, target_key=key) for user_id, key in before],
                "control_user_tenant_mapping_after": [dict(user_id=user_id, target_key=key) for user_id, key in after], "databases": entries}
            bytes_ = canonical_json(manifest)
            manifest_path = staging / "manifest.json"; manifest_path.write_bytes(bytes_); _private(manifest_path)
            checksum = hashlib.sha256(bytes_).hexdigest() + "  manifest.json\n"
            checksum_path = staging / "manifest.sha256"; checksum_path.write_text(checksum, encoding="ascii"); _private(checksum_path)
            _fsync(manifest_path); _fsync(checksum_path); _fsync(staging, directory=True)
            staging.replace(final); _private(final, directory=True); _fsync(root, directory=True)
            return final
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


def _read_manifest(directory: Path) -> tuple[dict[str, Any], bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError("Backup directory is not a safe directory")
    manifest_path, checksum_path = directory / "manifest.json", directory / "manifest.sha256"
    if not manifest_path.is_file() or manifest_path.is_symlink() or not checksum_path.is_file() or checksum_path.is_symlink():
        raise BackupError("Backup manifest is missing")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is invalid") from exc
    if canonical_json(manifest) != raw:
        raise BackupError("Backup manifest is not canonical")
    expected = hashlib.sha256(raw).hexdigest() + "  manifest.json\n"
    if checksum_path.read_text(encoding="ascii") != expected:
        raise BackupError("Backup manifest checksum failed")
    return manifest, raw


def verify_verified_backup(directory: Path | str, *, against_current_config: bool = False) -> dict[str, Any]:
    directory = Path(directory).resolve(strict=False)
    manifest, _raw = _read_manifest(directory)
    backup_id = str(manifest.get("backup_id", ""))
    if manifest.get("format_version") != BACKUP_FORMAT or manifest.get("status") != "complete" or directory.name != f"backup-{backup_id}":
        raise BackupError("Backup format, completion state, or identity is invalid")
    entries = manifest.get("databases")
    if not isinstance(entries, list) or manifest.get("database_count") != len(entries):
        raise BackupError("Backup database count is invalid")
    keys: set[str] = set(); listed: set[str] = {"manifest.json", "manifest.sha256"}
    for entry in entries:
        if not isinstance(entry, dict): raise BackupError("Backup entry is invalid")
        key, filename, kind = entry.get("target_key"), entry.get("filename"), entry.get("kind")
        if not isinstance(key, str) or key in keys or not isinstance(filename, str) or Path(filename).name != filename or filename in listed:
            raise BackupError("Backup target or filename is invalid")
        keys.add(key); listed.add(filename)
        if kind not in {"control", "single_user", "tenant"} or (kind == "tenant") != key.startswith("tenant:"):
            raise BackupError("Backup target kind is invalid")
        expected_filename = "000-control.sqlite" if key == "control" else None
        if key == "control" and filename != expected_filename:
            raise BackupError("Backup control filename is invalid")
        if key == "single-user" and (kind != "single_user" or not filename.endswith("-single-user.sqlite")):
            raise BackupError("Backup single-user filename is invalid")
        if kind == "tenant":
            from operator_storage import canonical_user_id
            tenant_id = canonical_user_id(str(entry.get("tenant_id")))
            if tenant_id != key.split(":", 1)[1] or filename != f"{len(keys) - 1:03d}-tenant-{tenant_id}.sqlite":
                raise BackupError("Backup tenant relationship is invalid")
        file = directory / filename
        if file.is_symlink() or not file.is_file() or file.stat().st_size != entry.get("size_bytes") or _sha256(file) != entry.get("sha256"):
            raise BackupError("Backup file checksum or size failed")
        if inspect_sqlite(file, deep=True).integrity_check_ok is not True or schema_fingerprint(file) != entry.get("schema_fingerprint"):
            raise BackupError("Backup SQLite integrity or schema verification failed")
        if migration_markers(file, str(kind)) != entry.get("migration_markers"):
            raise BackupError("Backup migration markers failed")
    unexpected = [item.name for item in directory.iterdir() if item.name not in listed and (item.suffix == ".sqlite" or item.name.startswith("manifest"))]
    if unexpected: raise BackupError("Backup contains unexpected files")
    result: dict[str, Any] = {"backup_id": backup_id, "verified": True}
    if against_current_config:
        current = {target.target_key: target for target in discover_database_targets()}
        result["missing_current_targets"] = sorted(keys - set(current))
        result["new_current_targets"] = sorted(set(current) - keys)
        result["configured_destinations"] = {key: str(target.path) for key, target in current.items() if key in keys}
    return result


def restore_plan(directory: Path | str, *, against_current_config: bool = False) -> dict[str, Any]:
    result = verify_verified_backup(directory, against_current_config=against_current_config)
    manifest, _ = _read_manifest(Path(directory).resolve(strict=False))
    current = {target.target_key: target for target in discover_database_targets()}
    return {"format_version": "garmincoach-restore-plan-v1", "restorable": False,
        "reason": "Phase 6A verification only", "operations": [
            {"target_key": entry["target_key"], "backup_file": entry["filename"],
             "configured_destination": str(current[entry["target_key"]].path) if entry["target_key"] in current else None,
             "action": "would_replace"} for entry in manifest["databases"]]}
