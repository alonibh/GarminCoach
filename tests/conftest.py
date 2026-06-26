"""Shared pytest fixtures: an isolated in-memory SQLite session."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db as db_module


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
