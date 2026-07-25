"""Shared pytest fixtures: an isolated in-memory SQLite session."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db as db_module
import tenant_store


@pytest.fixture(autouse=True)
def isolate_tenant_engines(tmp_path, monkeypatch):
    """Ensure every test uses isolated temporary directories and clean engine caches.

    Prevents tests from mutating config.MULTI_USER_DATA_ROOT or user athlete.db files.
    """
    tenant_store._engines.clear()
    monkeypatch.setattr("config.MULTI_USER_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr("config.CONTROL_DB_PATH", tmp_path / "control.db")
    yield
    tenant_store._engines.clear()


@pytest.fixture
def session():
    """A fresh in-memory SQLite session with the full schema, per test.

    StaticPool keeps the single in-memory connection alive across the session's
    operations (in-memory DBs vanish when the connection closes).
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    db_module.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
