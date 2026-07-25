"""Explicit, guarded reset of all configured GarminCoach SQLite databases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Callable
from uuid import UUID

from sqlalchemy import create_engine

import config
import db_migration


DESTRUCTIVE_CONFIRMATION_ARGUMENT = "--confirm-destroy-all-data"
SERVICE_NAME = "garmincoach"
SIDECAR_SUFFIXES = ("", "-wal", "-shm")


class DatabaseResetError(RuntimeError):
    """A sanitized operator-facing reset failure."""


@dataclass(frozen=True)
class ResetResult:
    quarantine_path: Path
    targeted_paths: tuple[Path, ...]
    recreated_paths: tuple[Path, ...]


def discover_reset_paths() -> list[Path]:
    """Use the migration's canonical discovery and deduplication contract."""
    return db_migration.discover_database_paths()


def _is_canonical_tenant_database(path: Path, tenant_root: Path) -> bool:
    try:
        relative = path.relative_to(tenant_root)
    except ValueError:
        return False
    if len(relative.parts) != 2 or relative.parts[1] != "athlete.db":
        return False
    try:
        return str(UUID(relative.parts[0])) == relative.parts[0]
    except ValueError:
        return False


def validate_reset_path(path: Path) -> Path:
    """Fail closed unless a path is one of the configured database targets."""
    resolved = path.resolve()
    control_path = Path(config.CONTROL_DB_PATH).resolve()
    single_user_path = Path(config.DB_PATH).resolve()
    tenant_root = Path(config.MULTI_USER_DATA_ROOT).resolve()
    if resolved in {control_path, single_user_path}:
        return resolved
    if _is_canonical_tenant_database(resolved, tenant_root):
        return resolved
    raise DatabaseResetError(
        f"Refusing database path outside configured data locations: {resolved.name}"
    )


def require_service_stopped(service_name: str = SERVICE_NAME) -> None:
    """Require both an inactive systemd unit and no matching Uvicorn process."""
    if os.name == "nt" or shutil.which("systemctl") is None:
        raise DatabaseResetError(
            "Cannot verify the GarminCoach service is stopped on this host"
        )
    state = subprocess.run(
        ["systemctl", "is-active", service_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if state.stdout.strip() not in {"inactive", "failed"}:
        raise DatabaseResetError("GarminCoach service must be stopped first")
    main_pid = subprocess.run(
        ["systemctl", "show", service_name, "-p", "MainPID", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    if main_pid.returncode != 0 or main_pid.stdout.strip() != "0":
        raise DatabaseResetError("A GarminCoach application process is still running")
    if shutil.which("pgrep") is None:
        raise DatabaseResetError("Cannot verify GarminCoach process state")
    processes = subprocess.run(
        ["pgrep", "-x", "uvicorn"],
        check=False,
        capture_output=True,
        text=True,
    )
    if processes.returncode == 0:
        raise DatabaseResetError("A GarminCoach application process is still running")
    if processes.returncode != 1:
        raise DatabaseResetError("Could not verify GarminCoach process state")


def _sanitized_path(path: Path) -> str:
    # Database paths are operator configuration, not database contents. Emit
    # the resolved path so recovery logs identify the exact reset target.
    return str(path.resolve())


def _quarantine_label(path: Path) -> str:
    resolved = path.resolve()
    if resolved == Path(config.CONTROL_DB_PATH).resolve():
        return "control"
    if resolved == Path(config.DB_PATH).resolve():
        return "single-user"
    return f"tenant-{resolved.parent.name}"


def _set_private_permissions(path: Path, *, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError as exc:
        if os.name != "nt":
            raise DatabaseResetError(
                f"Could not set permissions for {_sanitized_path(path)}"
            ) from exc
    if os.name != "nt" and hasattr(os, "chown"):
        try:
            os.chown(path, os.getuid(), os.getgid())
        except OSError as exc:
            raise DatabaseResetError(
                f"Could not set ownership for {_sanitized_path(path)}"
            ) from exc


def _move_to_quarantine(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _set_private_permissions(destination.parent, directory=True)
    try:
        source.replace(destination)
    except OSError:
        try:
            shutil.move(str(source), str(destination))
        except Exception as exc:
            raise DatabaseResetError(
                f"Could not quarantine {_sanitized_path(source)}"
            ) from exc
    _set_private_permissions(destination)


def _integrity_check(path: Path) -> None:
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30
        )
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise DatabaseResetError(
            f"Integrity check could not read {_sanitized_path(path)}"
        ) from exc
    if rows != [("ok",)]:
        raise DatabaseResetError(
            f"Integrity check failed for {_sanitized_path(path)}"
        )


def _initialize_clean_databases(quarantine_path: Path) -> tuple[Path, ...]:
    """Create clean control and single-user schemas through app primitives."""
    from control_db import create_control_engine, init_control_db
    from db import init_db

    db_migration.run_destructive_migrations(
        quarantine_path / "fresh-schema-migration"
    )

    control_path = Path(config.CONTROL_DB_PATH).resolve()
    single_user_path = Path(config.DB_PATH).resolve()
    control_engine = create_control_engine(control_path)
    try:
        init_control_db(control_engine)
    finally:
        control_engine.dispose()

    single_user_path.parent.mkdir(parents=True, exist_ok=True)
    single_user_engine = create_engine(
        f"sqlite:///{single_user_path}",
        future=True,
        connect_args={"timeout": 30},
    )
    try:
        init_db(single_user_engine)
    finally:
        single_user_engine.dispose()

    recreated = tuple(dict.fromkeys((control_path, single_user_path)))
    for path in recreated:
        for suffix in SIDECAR_SUFFIXES:
            created_file = Path(f"{path}{suffix}")
            if created_file.exists():
                _set_private_permissions(created_file)
        _integrity_check(path)
    return recreated


def reset_all_databases(
    *,
    confirmed: bool,
    quarantine_parent: Path | str | None = None,
    service_stopped_check: Callable[[], None] = require_service_stopped,
) -> ResetResult:
    if not confirmed:
        raise DatabaseResetError(
            f"Destructive confirmation {DESTRUCTIVE_CONFIRMATION_ARGUMENT} is required"
        )
    service_stopped_check()
    db_migration.dispose_all_engines()

    discovered = discover_reset_paths()
    targeted = tuple(validate_reset_path(path) for path in discovered)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parent = Path(
        quarantine_parent
        or (Path(config.PROJECT_ROOT) / "database_quarantine")
    ).resolve()
    quarantine_path = parent / f"database-reset-{timestamp}"
    quarantine_path.mkdir(parents=True, exist_ok=False)
    _set_private_permissions(quarantine_path, directory=True)

    manifest_entries: list[dict[str, object]] = []
    try:
        for database_path in targeted:
            label = _quarantine_label(database_path)
            for suffix in SIDECAR_SUFFIXES:
                source = Path(f"{database_path}{suffix}")
                if not source.exists():
                    continue
                destination = (
                    quarantine_path
                    / label
                    / f"{database_path.name}{suffix}"
                )
                _move_to_quarantine(source, destination)
                manifest_entries.append(
                    {
                        "database": label,
                        "file": destination.name,
                        "quarantined": True,
                        "valid_backup": False,
                    }
                )

        manifest_path = quarantine_path / "quarantine-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "purpose": "destructive reset quarantine",
                    "valid_backup": False,
                    "files": manifest_entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _set_private_permissions(manifest_path)
        recreated = _initialize_clean_databases(quarantine_path)
    except Exception:
        db_migration.dispose_all_engines()
        raise

    print(f"Quarantine: {_sanitized_path(quarantine_path)}")
    for path in targeted:
        print(f"Reset target: {_sanitized_path(path)}")
    for path in recreated:
        print(f"Integrity ok: {_sanitized_path(path)}")
    return ResetResult(quarantine_path, targeted, recreated)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly quarantine and recreate GarminCoach databases."
    )
    parser.add_argument(
        DESTRUCTIVE_CONFIRMATION_ARGUMENT,
        action="store_true",
        help="required acknowledgement that all GarminCoach data will be destroyed",
    )
    parser.add_argument(
        "--quarantine-parent",
        type=Path,
        help="operator-selected parent directory for quarantined database files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_destroy_all_data:
        print(
            f"ERROR: {DESTRUCTIVE_CONFIRMATION_ARGUMENT} is required",
            file=sys.stderr,
        )
        return 2
    try:
        reset_all_databases(
            confirmed=True,
            quarantine_parent=args.quarantine_parent,
        )
    except DatabaseResetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("ERROR: database reset failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
