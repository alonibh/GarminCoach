"""Read-only, deterministic operator health reporting."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Literal

import config
from operator_storage import TargetProfile, active_user_target_mapping, discover_database_targets, inspect_sqlite, migration_markers
from verified_backup import BackupError, validate_backup_root, verify_verified_backup

Severity = Literal["healthy", "warning", "critical"]
EXIT_CODES = {"healthy": 0, "warning": 1, "critical": 2}
PARTIAL_STALE_SECONDS = 60 * 60


@dataclass(frozen=True)
class HealthCheck:
    code: str
    status: Severity
    message: str
    target_key: str | None = None
    path: str | None = None


def _status(checks: list[HealthCheck]) -> Severity:
    if any(item.status == "critical" for item in checks): return "critical"
    if any(item.status == "warning" for item in checks): return "warning"
    return "healthy"


def _add_sqlite_checks(checks: list[HealthCheck], target, deep: bool, show_paths: bool) -> None:
    inspection = inspect_sqlite(target.path, deep=deep)
    path = str(target.path) if show_paths else None
    if not inspection.exists:
        checks.append(HealthCheck("database_presence", "critical" if target.required else "warning", "Required database is missing" if target.required else "Database is absent", target.target_key, path)); return
    if not inspection.readable:
        checks.append(HealthCheck("database_readable", "critical", "Database is unreadable", target.target_key, path)); return
    checks.append(HealthCheck("database_quick_check", "healthy" if inspection.quick_check_ok else "critical", "Quick check ok" if inspection.quick_check_ok else "Quick check failed", target.target_key, path))
    if deep:
        checks.append(HealthCheck("database_integrity", "healthy" if inspection.integrity_check_ok else "critical", "Integrity check ok" if inspection.integrity_check_ok else "Integrity check failed", target.target_key, path))
    checks.append(HealthCheck("database_foreign_keys", "healthy" if inspection.foreign_keys_ok else "warning", "Foreign keys ok" if inspection.foreign_keys_ok else "Foreign key violations found", target.target_key, path))
    try:
        markers = migration_markers(target.path, target.kind)
        checks.append(HealthCheck("migration_ledger", "healthy" if markers["state"] == "present" else "warning", "Migration ledger present" if markers["state"] == "present" else "Migration ledger absent", target.target_key, path))
    except Exception:
        checks.append(HealthCheck("migration_ledger", "critical", "Migration ledger is malformed", target.target_key, path))


def collect_health(*, deep: bool = False, show_paths: bool = False, now: datetime | None = None) -> tuple[Severity, list[HealthCheck]]:
    now = now or datetime.now(timezone.utc)
    checks: list[HealthCheck] = [HealthCheck("configuration", "healthy", "Configuration loaded")]
    try:
        targets = discover_database_targets(profile=TargetProfile.RUNTIME)
    except Exception:
        return "critical", checks + [HealthCheck("target_discovery", "critical", "Canonical database discovery failed")]
    checks.append(HealthCheck("target_discovery", "healthy", "Canonical database discovery succeeded"))
    for target in targets:
        _add_sqlite_checks(checks, target, deep, show_paths)
        if os.name != "nt" and target.path.exists():
            mode = target.path.stat().st_mode & 0o777
            checks.append(HealthCheck("database_permissions", "warning" if mode & 0o077 else "healthy", "Database permissions are private" if not mode & 0o077 else "Database permissions are broader than private", target.target_key))
    root = Path(config.MULTI_USER_DATA_ROOT).resolve(strict=False)
    control = next(target for target in targets if target.kind == "control")
    try:
        active_targets = {target_key for _user_id, target_key in active_user_target_mapping(control.path)}
        present_tenants = {target.target_key for target in targets if target.kind == "tenant" and target.path.exists()}
        for target_key in sorted(active_targets - present_tenants):
            checks.append(HealthCheck("active_tenant_mapping", "critical", "Active user tenant database is missing", target_key))
        for target_key in sorted(present_tenants - active_targets):
            checks.append(HealthCheck("unexpected_tenant_mapping", "warning", "Canonical tenant database has no active control user", target_key))
        if not active_targets - present_tenants:
            checks.append(HealthCheck("active_tenant_mapping", "healthy", "Active control users map to canonical tenant targets"))
    except Exception:
        checks.append(HealthCheck("active_tenant_mapping", "critical", "Active control-user mapping is unreadable"))
    if root.exists():
        expected = {target.tenant_id for target in targets if target.tenant_id}
        for child in root.iterdir():
            if child.is_dir() and child.name not in expected and not child.is_symlink():
                checks.append(HealthCheck("unexpected_tenant_directory", "warning", "Unexpected tenant directory ignored"))
    try:
        backup_root = validate_backup_root()
        partial = sorted(item for item in backup_root.glob(".partial-*") if item.is_dir()) if backup_root.exists() else []
        for item in partial:
            age = (now - datetime.fromtimestamp(item.stat().st_mtime, timezone.utc)).total_seconds()
            checks.append(HealthCheck("partial_backup", "warning" if age > PARTIAL_STALE_SECONDS else "healthy", "Stale partial backup directory found" if age > PARTIAL_STALE_SECONDS else "Recent partial backup directory found"))
        complete = sorted((item for item in backup_root.glob("backup-*") if item.is_dir()), key=lambda item: item.name)
        if not complete:
            checks.append(HealthCheck("verified_backup", "warning", "No complete verified backup found"))
        else:
            latest = complete[-1]
            try:
                verified = verify_verified_backup(latest)
                completed = datetime.fromisoformat(verified["completed_at"].replace("Z", "+00:00"))
                age = (now - completed).total_seconds() / 3600
                if age < -(5 / 60):
                    raise BackupError("Backup completion timestamp is in the future")
                checks.append(HealthCheck("verified_backup", "warning" if age > config.OPERATOR_BACKUP_WARN_AGE_HOURS else "healthy", "Latest verified backup is stale" if age > config.OPERATOR_BACKUP_WARN_AGE_HOURS else "Latest verified backup is valid"))
            except BackupError:
                checks.append(HealthCheck("verified_backup", "critical", "Latest complete backup is invalid"))
        usage = shutil.disk_usage(backup_root if backup_root.exists() else config.PROJECT_ROOT)
        checks.append(HealthCheck("backup_disk_space", "warning" if usage.free < config.OPERATOR_BACKUP_MIN_FREE_MIB * 1024 * 1024 else "healthy", "Backup disk free space is below threshold" if usage.free < config.OPERATOR_BACKUP_MIN_FREE_MIB * 1024 * 1024 else "Backup disk free space is sufficient"))
    except BackupError:
        checks.append(HealthCheck("backup_root", "critical", "Backup root configuration is unsafe"))
    lock = Path(config.PROJECT_ROOT) / "garmincoach.lock"
    checks.append(HealthCheck("process_lock", "healthy", "Process lock exists" if lock.exists() else "Process lock is absent"))
    return _status(checks), checks


def render_health(*, deep: bool = False, as_json: bool = False, show_paths: bool = False) -> tuple[str, int]:
    status, checks = collect_health(deep=deep, show_paths=show_paths)
    if as_json:
        result = {"format_version": "garmincoach-operator-health-v1", "status": status, "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "checks": [item.__dict__ for item in checks]}
        return json.dumps(result, sort_keys=True, separators=(",", ":")), EXIT_CODES[status]
    lines = [f"GarminCoach operator health: {status.upper()}"]
    for item in checks:
        suffix = f" ({item.target_key})" if item.target_key else ""
        lines.append(f"[{item.status}] {item.code}{suffix}: {item.message}")
    lines.append(f"Exit code: {EXIT_CODES[status]}")
    return "\n".join(lines), EXIT_CODES[status]
