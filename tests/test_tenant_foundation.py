from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from control_db import ControlBase, Invitation, User, create_control_engine, utcnow
from db import Goal
from tenant_context import TenantIdentity, current_tenant, require_tenant, tenant_scope
from tenant_store import athlete_db_path, get_user_session, provision_user_store


def test_tenant_scope_is_required_and_restored():
    first = TenantIdentity(str(uuid4()))
    second = TenantIdentity(str(uuid4()), role="owner")

    with pytest.raises(RuntimeError):
        require_tenant()
    with tenant_scope(first):
        assert require_tenant() == first
        with tenant_scope(second):
            assert require_tenant() == second
        assert require_tenant() == first
    assert current_tenant() is None


@pytest.mark.parametrize("value", ["../escape", "not-a-uuid", "", str(uuid4()).upper()])
def test_user_storage_rejects_noncanonical_ids(tmp_path: Path, value: str):
    with pytest.raises(ValueError):
        athlete_db_path(value, tmp_path)


def test_each_user_gets_a_physically_isolated_database(tmp_path: Path):
    first = str(uuid4())
    second = str(uuid4())
    first_path = provision_user_store(first, tmp_path)
    second_path = provision_user_store(second, tmp_path)

    assert first_path != second_path
    assert first_path.parent.parent == tmp_path.resolve()
    assert second_path.parent.parent == tmp_path.resolve()

    with get_user_session(first, tmp_path) as session:
        session.add(Goal(id=1, goal="first athlete", custom_input=""))
    with get_user_session(second, tmp_path) as session:
        session.add(Goal(id=1, goal="second athlete", custom_input=""))

    with get_user_session(first, tmp_path) as session:
        assert session.get(Goal, 1).goal == "first athlete"
    with get_user_session(second, tmp_path) as session:
        assert session.get(Goal, 1).goal == "second athlete"


def test_owner_store_can_bootstrap_from_legacy_database(tmp_path: Path):
    legacy_user = str(uuid4())
    legacy_path = provision_user_store(legacy_user, tmp_path / "legacy-root")
    with get_user_session(legacy_user, tmp_path / "legacy-root") as session:
        session.add(Goal(id=1, goal="preserved owner history", custom_input=""))

    owner_id = str(uuid4())
    owner_path = provision_user_store(
        owner_id,
        tmp_path / "users",
        seed_database=legacy_path,
    )
    assert owner_path != legacy_path
    with get_user_session(owner_id, tmp_path / "users") as session:
        assert session.get(Goal, 1).goal == "preserved owner history"
    with get_user_session(legacy_user, tmp_path / "legacy-root") as session:
        assert session.get(Goal, 1).goal == "preserved owner history"


def test_control_db_contains_identity_metadata_not_athlete_tables(tmp_path: Path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    owner_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(
            id=owner_id,
            google_sub="google-owner",
            email="owner@example.com",
            role="owner",
            status="active",
        ))
        session.flush()
        session.add(Invitation(
            email="invitee@example.com",
            token_hash="a" * 64,
            created_by_user_id=owner_id,
            expires_at=utcnow() + timedelta(days=7),
        ))

    table_names = set(ControlBase.metadata.tables)
    assert "users" in table_names
    assert "invitations" in table_names
    assert "activities" not in table_names
    with Session() as session:
        assert session.query(Invitation).one().email == "invitee@example.com"
    engine.dispose()
