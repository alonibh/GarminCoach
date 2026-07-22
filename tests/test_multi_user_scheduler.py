from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

import config
from control_db import ControlBase, User, create_control_engine
import sync.scheduler as scheduler_module
from tenant_context import require_tenant


def test_scheduled_sync_runs_inside_the_selected_users_tenant(monkeypatch, tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(
            id=user_id,
            email="athlete@example.com",
            status="active",
            timezone="Asia/Jerusalem",
            garmin_connected=True,
        ))

    @contextmanager
    def sessions():
        with Session.begin() as session:
            yield session

    seen = []

    class Client:
        def is_authenticated(self):
            seen.append(("client", require_tenant().user_id))
            return True

    monkeypatch.setattr(scheduler_module, "get_control_session", sessions)
    monkeypatch.setattr(scheduler_module, "client", Client())
    monkeypatch.setattr(
        scheduler_module.sync_runner,
        "try_start_sync",
        lambda *, full: seen.append(("sync", require_tenant().user_id, full)),
    )
    scheduler_module._run_for_user(user_id)
    assert seen == [("client", user_id), ("sync", user_id, False)]
    engine.dispose()


def test_inactive_user_never_runs_scheduled_work(monkeypatch, tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(id=user_id, email="athlete@example.com", status="deleting"))

    @contextmanager
    def sessions():
        with Session.begin() as session:
            yield session

    monkeypatch.setattr(scheduler_module, "get_control_session", sessions)
    monkeypatch.setattr(
        scheduler_module.sync_runner,
        "try_start_sync",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    scheduler_module._run_for_user(user_id)
    engine.dispose()
