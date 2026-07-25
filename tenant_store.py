"""Physical per-user athlete database management."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
from uuid import UUID

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import config
from db import Base
from tenant_context import require_tenant


_engines: dict[str, Engine] = {}
_engine_lock = threading.RLock()


def canonical_user_id(user_id: str) -> str:
    try:
        canonical = str(UUID(user_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("User ID must be a canonical UUID") from exc
    if canonical != user_id:
        raise ValueError("User ID must be a canonical UUID")
    return canonical


def user_root(user_id: str, root: Path | str | None = None) -> Path:
    canonical = canonical_user_id(user_id)
    base_root = root if root is not None else config.MULTI_USER_DATA_ROOT
    base = Path(base_root).resolve()
    target = (base / canonical).resolve()
    if target.parent != base:
        raise ValueError("User storage path escaped the configured root")
    return target


def athlete_db_path(
    user_id: str, root: Path | str | None = None
) -> Path:
    return user_root(user_id, root) / "athlete.db"


def verify_and_repair_sqlite(db_path: Path) -> bool:
    """Check if SQLite database file at db_path is valid. If malformed, quarantine it and return False."""
    if not db_path.exists():
        return True
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA quick_check")
            row = cursor.fetchone()
            if row and row[0] == "ok":
                return True
            import logging
            logging.getLogger(__name__).error("Database quick_check failed for %s: %s", db_path, row)
        finally:
            conn.close()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Database connection/integrity check failed for %s: %s", db_path, exc)

    # Before quarantining, attempt SQLite backup recovery to preserve all user data
    recovered_path = db_path.with_name(f"{db_path.name}.recovered")
    try:
        source_conn = sqlite3.connect(db_path, timeout=5)
        dest_conn = sqlite3.connect(recovered_path, timeout=5)
        try:
            source_conn.backup(dest_conn)
            dest_conn.close()
            source_conn.close()
            os.replace(recovered_path, db_path)
            import logging
            logging.getLogger(__name__).info("Successfully recovered database for %s via backup stream", db_path)
            return True
        except Exception:
            dest_conn.close()
            source_conn.close()
    except Exception:
        pass
    if recovered_path.exists():
        try:
            recovered_path.unlink()
        except OSError:
            pass

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine = db_path.with_name(f"{db_path.name}.corrupt.{timestamp}")
    import logging
    logging.getLogger(__name__).warning("Quarantining malformed database %s -> %s", db_path, quarantine)
    try:
        for ext in ("", "-wal", "-shm"):
            p = Path(f"{db_path}{ext}")
            if p.exists():
                os.replace(p, Path(f"{quarantine}{ext}"))
    except Exception as q_exc:
        logging.getLogger(__name__).error("Failed to quarantine malformed database %s: %s", db_path, q_exc)
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def _create_engine_for_path(db_path: Path) -> Engine:
    verify_and_repair_sqlite(db_path)
    engine = create_engine(
        f"sqlite:///{db_path}", future=True, connect_args={"timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def provision_user_store(
    user_id: str,
    root: Path | str | None = None,
    *,
    seed_database: Path | str | None = None,
) -> Path:
    directory = user_root(user_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        # Windows ACLs are managed by the service account; chmod is best-effort.
        pass
    db_path = directory / "athlete.db"
    verify_and_repair_sqlite(db_path)
    seed_path = Path(seed_database).resolve() if seed_database else None
    if not db_path.exists() and seed_path and seed_path.exists():
        temporary = directory / ".athlete.db.bootstrap"
        try:
            source = sqlite3.connect(f"file:{seed_path.as_posix()}?mode=ro", uri=True, timeout=10)
            destination = sqlite3.connect(temporary, timeout=10)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.replace(temporary, db_path)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to seed user database from %s: %s", seed_path, exc)
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    engine = _create_engine_for_path(db_path)
    try:
        Base.metadata.create_all(engine)
        from db import _migrate_add_columns
        _migrate_add_columns(engine)
    finally:
        engine.dispose()
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return db_path


def engine_for_user(
    user_id: str, root: Path | str | None = None
) -> Engine:
    canonical = canonical_user_id(user_id)
    base_root = root if root is not None else config.MULTI_USER_DATA_ROOT
    # Test/alternate roots intentionally bypass the process cache so a user ID
    # can be exercised safely against multiple temporary roots.
    use_cache = Path(base_root).resolve() == Path(config.MULTI_USER_DATA_ROOT).resolve()
    if not use_cache:
        db_path = athlete_db_path(canonical, base_root)
        if not db_path.exists():
            provision_user_store(canonical, base_root)
        return _create_engine_for_path(db_path)

    with _engine_lock:
        engine = _engines.get(canonical)
        if engine is None:
            db_path = athlete_db_path(canonical, base_root)
            if not db_path.exists():
                provision_user_store(canonical, base_root)
            engine = _create_engine_for_path(db_path)
            _engines[canonical] = engine
        return engine


@contextmanager
def get_user_session(
    user_id: str, root: Path | str | None = None
) -> Iterator:
    engine = engine_for_user(user_id, root)
    base_root = root if root is not None else config.MULTI_USER_DATA_ROOT
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        if (
            Path(base_root).resolve() != Path(config.MULTI_USER_DATA_ROOT).resolve()
            and str(engine.url) != "sqlite://"
        ):
            engine.dispose()


@contextmanager
def get_current_user_session() -> Iterator:
    tenant = require_tenant()
    with get_user_session(tenant.user_id) as session:
        yield session


def dispose_user_engine(user_id: str) -> None:
    canonical = canonical_user_id(user_id)
    with _engine_lock:
        engine = _engines.pop(canonical, None)
    if engine is not None:
        engine.dispose()
