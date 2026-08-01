"""Atomic, backed-up migration for the Ask Coach schema cutover."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import config

MIGRATION_VERSION = "ask_coach_v1"
OBSOLETE_TABLES = (
    "chat_intent_audit",
    "chat_dialogue_state",
    "athlete_safety_reports",
)


def dispose_all_engines() -> None:
    from control_db import dispose_control_engine
    from db import dispose_engine
    from tenant_store import dispose_all_user_engines

    dispose_control_engine()
    dispose_all_user_engines()
    dispose_engine()


def discover_database_paths() -> list[Path]:
    """Use the shared canonical target model without changing migration scope."""
    from operator_storage import TargetProfile, discover_database_targets

    return [target.path for target in discover_database_targets(
        profile=TargetProfile.ALL_CONFIGURED_MAINTENANCE
    )]


def _migration_completed(control_path: Path) -> bool:
    if not control_path.exists():
        return False
    try:
        with sqlite3.connect(control_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='migration_versions'"
            ).fetchone()
            if not exists:
                return False
            return connection.execute(
                "SELECT 1 FROM migration_versions WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone() is not None
    except sqlite3.DatabaseError:
        return False


def _integrity_check(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {path.name}")


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database(source_path: Path, backup_path: Path) -> str:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(
        f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30
    )
    destination = sqlite3.connect(backup_path, timeout=30)
    try:
        source.backup(destination, pages=256)
    finally:
        destination.close()
        source.close()
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
    _integrity_check(backup_path)
    return _checksum(backup_path)


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _restore(entries: list[dict]) -> None:
    for entry in entries:
        source = Path(entry["source_path"])
        if entry["existed"] and entry["status"] not in {"verified", "migrated"}:
            continue
        _remove_sidecars(source)
        if not entry["existed"]:
            source.unlink(missing_ok=True)
            continue
        backup = Path(entry["backup_path"])
        source.parent.mkdir(parents=True, exist_ok=True)
        backup_connection = sqlite3.connect(
            f"file:{backup.as_posix()}?mode=ro", uri=True, timeout=30
        )
        restored = sqlite3.connect(source, timeout=30)
        try:
            backup_connection.backup(restored, pages=256)
        finally:
            restored.close()
            backup_connection.close()
        _integrity_check(source)


def _create_consent_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_coach_consents (
            user_id VARCHAR(36) PRIMARY KEY
                REFERENCES users(id) ON DELETE CASCADE,
            consent_version VARCHAR(32) NOT NULL,
            provider VARCHAR(64) NOT NULL,
            data_categories_version VARCHAR(32) NOT NULL,
            data_categories_json TEXT NOT NULL,
            category_hash VARCHAR(64) NOT NULL,
            consented_at DATETIME NOT NULL,
            revoked_at DATETIME
        )
        """
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(ask_coach_consents)")
    }
    if "data_categories_version" not in columns:
        connection.execute(
            "ALTER TABLE ask_coach_consents "
            "ADD COLUMN data_categories_version VARCHAR(32) NOT NULL DEFAULT ''"
        )


def run_destructive_migrations(
    backup_root: Path | str | None = None,
) -> Path | None:
    control_path = Path(config.CONTROL_DB_PATH).resolve()
    if _migration_completed(control_path):
        return None
    dispose_all_engines()
    paths = discover_database_paths()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(
        backup_root
        or (
            config.PROJECT_ROOT
            / "migration_backups"
            / f"{MIGRATION_VERSION}-{timestamp}"
        )
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    entries: list[dict] = []
    manifest_path = root / "manifest.json"
    created_at = datetime.now(timezone.utc).isoformat()

    def write_manifest() -> None:
        manifest_path.write_text(
            json.dumps(
                {
                    "migration_version": MIGRATION_VERSION,
                    "created_at": created_at,
                    "databases": entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(manifest_path, 0o600)
        except OSError:
            pass

    try:
        for index, path in enumerate(paths):
            existed = path.exists()
            backup_path = root / f"{index:03d}-{path.name}.sqlite"
            entry = {
                "source_path": str(path),
                "backup_path": str(backup_path) if existed else None,
                "sha256": None,
                "status": "not_present" if not existed else "pending",
                "existed": existed,
            }
            entries.append(entry)
            if existed:
                entry["sha256"] = _backup_database(path, backup_path)
                entry["status"] = "verified"
        write_manifest()

        for path in paths:
            if path == control_path or not path.exists():
                continue
            with sqlite3.connect(path) as connection:
                for table in OBSOLETE_TABLES:
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')

        control_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(control_path) as connection:
            _create_consent_table(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS migration_versions ("
                "version VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO migration_versions(version, applied_at) "
                "VALUES (?, ?)",
                (MIGRATION_VERSION, datetime.now(timezone.utc).isoformat()),
            )
        for entry in entries:
            path = Path(entry["source_path"])
            if path.exists():
                _integrity_check(path)
            entry["status"] = "migrated"
        write_manifest()
        return manifest_path
    except Exception:
        _restore(entries)
        dispose_all_engines()
        raise
