"""Shared, fail-closed database-target and read-only SQLite primitives."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import UUID

import config


class DatabaseIntegrityError(RuntimeError):
    """A configured SQLite file exists but cannot safely be used."""


class TargetProfile(str, Enum):
    RUNTIME = "runtime"
    ALL_CONFIGURED_MAINTENANCE = "all_configured_maintenance"


@dataclass(frozen=True)
class DatabaseTarget:
    target_key: str
    kind: Literal["control", "single_user", "tenant"]
    path: Path
    tenant_id: str | None
    required: bool


@dataclass(frozen=True)
class SqliteInspection:
    exists: bool; readable: bool; quick_check_ok: bool | None; integrity_check_ok: bool | None
    foreign_keys_ok: bool | None; page_count: int | None; freelist_count: int | None
    journal_mode: str | None; schema_version: int | None; user_version: int | None; error_code: str | None


def canonical_user_id(user_id: str) -> str:
    try: canonical = str(UUID(user_id))
    except (TypeError, ValueError, AttributeError) as exc: raise ValueError("User ID must be a canonical UUID") from exc
    if canonical != user_id: raise ValueError("User ID must be a canonical UUID")
    return canonical


def has_symlink_component(path: Path | str) -> bool:
    """Inspect the original path, never a pre-resolved path, with lstat semantics."""
    candidate = Path(path).expanduser()
    parts = candidate.parts
    current = Path(parts[0]) if candidate.is_absolute() else Path()
    for part in parts[1:] if candidate.is_absolute() else parts:
        current = current / part
        try:
            if current.is_symlink(): return True
        except OSError:
            return True
    return False


def safe_resolve(path: Path | str, *, root: Path | None = None) -> Path:
    original = Path(path).expanduser()
    if has_symlink_component(original): raise ValueError("Symlinked path is not permitted")
    resolved = original.resolve(strict=False)
    if root is not None:
        root_resolved = root.resolve(strict=False)
        try: resolved.relative_to(root_resolved)
        except ValueError as exc: raise ValueError("Path escaped configured root") from exc
    return resolved


def discover_database_targets(*, profile: TargetProfile = TargetProfile.RUNTIME,
    control_path: Path | str | None = None, single_user_path: Path | str | None = None,
    tenant_root: Path | str | None = None, multi_user_enabled: bool | None = None) -> tuple[DatabaseTarget, ...]:
    """Discover configured canonical targets; never enumerate arbitrary DB files."""
    multi = config.MULTI_USER_ENABLED if multi_user_enabled is None else multi_user_enabled
    control = safe_resolve(control_path or config.CONTROL_DB_PATH)
    single = safe_resolve(single_user_path or config.DB_PATH)
    root_original = Path(tenant_root or config.MULTI_USER_DATA_ROOT)
    if root_original.exists() and has_symlink_component(root_original): raise ValueError("Tenant root may not be symlinked")
    root = root_original.resolve(strict=False)
    include_single = profile is TargetProfile.ALL_CONFIGURED_MAINTENANCE or not multi
    include_tenants = profile is TargetProfile.ALL_CONFIGURED_MAINTENANCE or multi
    targets: list[DatabaseTarget] = [DatabaseTarget("control", "control", control, None, True)]
    if include_single: targets.append(DatabaseTarget("single-user", "single_user", single, None, not multi))
    if include_tenants and root.exists():
        if not root.is_dir(): raise ValueError("Tenant root is not a directory")
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.is_dir(): continue
            try: tenant_id = canonical_user_id(child.name)
            except ValueError: continue
            if has_symlink_component(child): raise ValueError("Canonical tenant directory may not be symlinked")
            database = safe_resolve(child / "athlete.db", root=root)
            # Runtime targets are real databases, not merely candidate folders.
            if profile is TargetProfile.RUNTIME and not database.exists():
                continue
            targets.append(DatabaseTarget(f"tenant:{tenant_id}", "tenant", database, tenant_id, False))
    seen_keys: set[str] = set(); seen_paths: set[str] = set(); result: list[DatabaseTarget] = []
    for target in targets:
        key = os.path.normcase(str(target.path))
        if key in seen_paths and profile is TargetProfile.ALL_CONFIGURED_MAINTENANCE:
            # Established destructive-maintenance discovery deduplicates an
            # intentionally shared legacy/control path.
            continue
        if target.target_key in seen_keys or key in seen_paths: raise ValueError("Duplicate or ambiguous database target")
        seen_keys.add(target.target_key); seen_paths.add(key); result.append(target)
    return tuple(result)


def inspect_sqlite(path: Path, *, deep: bool = False) -> SqliteInspection:
    try: path = safe_resolve(path)
    except ValueError: return SqliteInspection(True, False, None, None, None, None, None, None, None, None, "invalid_path")
    if not path.exists(): return SqliteInspection(False, False, None, None, None, None, None, None, None, None, None)
    if not path.is_file(): return SqliteInspection(True, False, None, None, None, None, None, None, None, None, "invalid_path")
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            quick = c.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            integrity = c.execute("PRAGMA integrity_check").fetchall() == [("ok",)] if deep else None
            foreign = c.execute("PRAGMA foreign_key_check").fetchone() is None
            page, free = int(c.execute("PRAGMA page_count").fetchone()[0]), int(c.execute("PRAGMA freelist_count").fetchone()[0])
            journal, schema, user = str(c.execute("PRAGMA journal_mode").fetchone()[0]), int(c.execute("PRAGMA schema_version").fetchone()[0]), int(c.execute("PRAGMA user_version").fetchone()[0])
        finally: c.close()
    except (OSError, sqlite3.Error): return SqliteInspection(True, False, None, None, None, None, None, None, None, None, "unreadable")
    return SqliteInspection(True, True, quick, integrity, foreign, page, free, journal, schema, user, None)


def require_healthy_existing_database(path: Path) -> None:
    value = inspect_sqlite(path)
    if value.exists and (not value.readable or not value.quick_check_ok): raise DatabaseIntegrityError("Configured SQLite database failed read-only integrity preflight")


def schema_fingerprint(path: Path) -> str:
    try:
        path = safe_resolve(path); c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            payload=[]
            for name, sql in c.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                q='"'+str(name).replace('"','""')+'"'
                payload.append({"name":name,"columns":[tuple(r) for r in c.execute(f"PRAGMA table_info({q})")],"indexes":[tuple(r) for r in c.execute(f"PRAGMA index_list({q})")],"foreign_keys":[tuple(r) for r in c.execute(f"PRAGMA foreign_key_list({q})")],"sql":sql or ""})
        finally: c.close()
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=True).encode()).hexdigest()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc: raise DatabaseIntegrityError("Schema fingerprint inspection failed") from exc


def migration_markers(path: Path, kind: str) -> dict[str, object]:
    ledger = "migration_versions" if kind == "control" else "app_migrations"; column = "version" if kind == "control" else "migration_key"
    try:
        path=safe_resolve(path); c=sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ledger,)).fetchone(): return {"ledger":ledger,"keys":[],"state":"absent"}
            rows=c.execute(f"SELECT {column} FROM {ledger} ORDER BY {column}").fetchall()
        finally: c.close()
        if any(not isinstance(row[0], str) for row in rows): raise DatabaseIntegrityError("Migration ledger is malformed")
        keys=[row[0] for row in rows]
        if keys != sorted(set(keys)): raise DatabaseIntegrityError("Migration ledger is malformed")
        return {"ledger":ledger,"keys":keys,"state":"present"}
    except (OSError, sqlite3.Error, ValueError) as exc: raise DatabaseIntegrityError("Migration ledger is malformed") from exc


def active_user_target_mapping(control_path: Path) -> tuple[tuple[str, str], ...]:
    if not control_path.exists(): return ()
    try:
        path=safe_resolve(control_path); c=sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone(): return ()
            rows=c.execute("SELECT id FROM users WHERE status='active' ORDER BY id").fetchall()
        finally: c.close()
        result=[]
        for (user_id,) in rows:
            canonical=canonical_user_id(str(user_id)); result.append((canonical,f"tenant:{canonical}"))
        return tuple(result)
    except (OSError, sqlite3.Error, ValueError) as exc: raise DatabaseIntegrityError("Control-user mapping is unreadable") from exc
