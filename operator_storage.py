"""Safe, shared SQLite target discovery and read-only inspection primitives.

This module deliberately has no application side effects.  It is used by the
operator commands and by startup preflight so an existing malformed database is
never mistaken for a missing database.
"""
from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class DatabaseTarget:
    target_key: str
    kind: Literal["control", "single_user", "tenant"]
    path: Path
    tenant_id: str | None
    required: bool


@dataclass(frozen=True)
class SqliteInspection:
    exists: bool
    readable: bool
    quick_check_ok: bool | None
    integrity_check_ok: bool | None
    foreign_keys_ok: bool | None
    page_count: int | None
    freelist_count: int | None
    journal_mode: str | None
    schema_version: int | None
    user_version: int | None
    error_code: str | None


def canonical_user_id(user_id: str) -> str:
    """Validate the one canonical UUID representation used for tenant paths."""
    try:
        canonical = str(UUID(user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("User ID must be a canonical UUID") from exc
    if canonical != user_id:
        raise ValueError("User ID must be a canonical UUID")
    return canonical


def _resolved(path: Path | str) -> Path:
    return Path(path).resolve(strict=False)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_tenant_path(root: Path, name: str) -> Path | None:
    try:
        tenant_id = canonical_user_id(name)
    except ValueError:
        return None
    directory = root / tenant_id
    if directory.is_symlink() or not _within(_resolved(directory), root):
        return None
    path = directory / "athlete.db"
    if path.is_symlink() or not _within(_resolved(path), root):
        return None
    return _resolved(path)


def discover_database_targets(
    *,
    control_path: Path | str | None = None,
    single_user_path: Path | str | None = None,
    tenant_root: Path | str | None = None,
    multi_user_enabled: bool | None = None,
) -> tuple[DatabaseTarget, ...]:
    """Discover only configured canonical paths; never scan arbitrary *.db files."""
    control = _resolved(control_path or config.CONTROL_DB_PATH)
    single = _resolved(single_user_path or config.DB_PATH)
    root = _resolved(tenant_root or config.MULTI_USER_DATA_ROOT)
    multi = config.MULTI_USER_ENABLED if multi_user_enabled is None else multi_user_enabled
    items: list[DatabaseTarget] = [
        DatabaseTarget("control", "control", control, None, True),
        DatabaseTarget("single-user", "single_user", single, None, not multi),
    ]
    if root.exists() and not root.is_symlink() and root.is_dir():
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or child.is_symlink():
                continue
            path = _safe_tenant_path(root, child.name)
            if path is None:
                continue
            tenant_id = child.name
            items.append(DatabaseTarget(f"tenant:{tenant_id}", "tenant", path, tenant_id, False))
    seen_paths: dict[str, DatabaseTarget] = {}
    seen_keys: set[str] = set()
    result: list[DatabaseTarget] = []
    for item in items:
        if item.target_key in seen_keys:
            raise ValueError("Duplicate database target key")
        seen_keys.add(item.target_key)
        key = os.path.normcase(str(item.path))
        if key in seen_paths:
            # Same configured path is a single target.  Prefer the required
            # control target to preserve the legacy discovery contract.
            continue
        seen_paths[key] = item
        result.append(item)
    return tuple(result)


def inspect_sqlite(path: Path, *, deep: bool = False) -> SqliteInspection:
    """Inspect an existing SQLite database in URI read-only mode only."""
    path = _resolved(path)
    if not path.exists():
        return SqliteInspection(False, False, None, None, None, None, None, None, None, None, None)
    if path.is_symlink() or not path.is_file():
        return SqliteInspection(True, False, None, None, None, None, None, None, None, None, "invalid_path")
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
            quick_ok = quick_rows == [("ok",)]
            integrity_ok = None
            if deep:
                integrity_ok = connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            foreign_ok = connection.execute("PRAGMA foreign_key_check").fetchone() is None
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return SqliteInspection(True, False, None, None, None, None, None, None, None, None, "unreadable")
    return SqliteInspection(True, True, quick_ok, integrity_ok, foreign_ok, page_count, freelist_count, journal_mode, schema_version, user_version, None)


def require_healthy_existing_database(path: Path) -> None:
    inspection = inspect_sqlite(path)
    if inspection.exists and (not inspection.readable or not inspection.quick_check_ok):
        raise DatabaseIntegrityError("Configured SQLite database failed read-only integrity preflight")


def schema_fingerprint(path: Path) -> str:
    """Return a deterministic schema-only digest without reading application rows."""
    path = _resolved(path)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        tables = connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        payload: list[dict[str, object]] = []
        for name, sql in tables:
            quoted = '"' + str(name).replace('"', '""') + '"'
            payload.append({
                "name": name,
                "columns": [tuple(row) for row in connection.execute(f"PRAGMA table_info({quoted})")],
                "indexes": [tuple(row) for row in connection.execute(f"PRAGMA index_list({quoted})")],
                "foreign_keys": [tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({quoted})")],
                "sql": sql or "",
            })
    finally:
        connection.close()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def migration_markers(path: Path, kind: str) -> dict[str, object]:
    ledger = "migration_versions" if kind == "control" else "app_migrations"
    connection = sqlite3.connect(f"file:{_resolved(path).as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ledger,)).fetchone()
        if not exists:
            return {"ledger": ledger, "keys": [], "state": "absent"}
        column = "version" if ledger == "migration_versions" else "migration_key"
        rows = connection.execute(f"SELECT {column} FROM {ledger} ORDER BY {column}").fetchall()
        keys = [str(row[0]) for row in rows]
        if len(keys) != len(set(keys)):
            raise DatabaseIntegrityError("Migration ledger has duplicate keys")
        return {"ledger": ledger, "keys": keys, "state": "present"}
    except sqlite3.Error as exc:
        raise DatabaseIntegrityError("Migration ledger is malformed") from exc
    finally:
        connection.close()


def active_user_target_mapping(control_path: Path) -> tuple[tuple[str, str], ...]:
    """Read the control-plane active UUID-to-tenant-target mapping only."""
    if not control_path.exists():
        return ()
    try:
        connection = sqlite3.connect(f"file:{_resolved(control_path).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            if not exists:
                return ()
            rows = connection.execute("SELECT id FROM users WHERE status='active' ORDER BY id").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseIntegrityError("Control-user mapping is unreadable") from exc
    pairs: list[tuple[str, str]] = []
    for (user_id,) in rows:
        canonical = canonical_user_id(str(user_id))
        pairs.append((canonical, f"tenant:{canonical}"))
    return tuple(pairs)
