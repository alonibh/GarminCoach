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
        lambda *, full, allow_backfill=False: seen.append(("sync", require_tenant().user_id, full, allow_backfill)),
    )
    scheduler_module._run_for_user(user_id)
    assert seen == [("client", user_id), ("sync", user_id, False, True)]
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


def test_linked_user_gets_notification_jobs_and_unlinked_user_does_not(monkeypatch, tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    linked_id, unlinked_id = str(uuid4()), str(uuid4())
    with Session.begin() as session:
        session.add_all([
            User(
                id=linked_id, email="linked@example.com", status="active",
                timezone="UTC", garmin_connected=True, telegram_linked=True,
            ),
            User(
                id=unlinked_id, email="unlinked@example.com", status="active",
                timezone="UTC", garmin_connected=True, telegram_linked=False,
            ),
        ])

    @contextmanager
    def sessions():
        with Session.begin() as session:
            yield session

    class Job:
        def __init__(self, job_id): self.id = job_id

    class Scheduler:
        def __init__(self): self.jobs = {}
        def get_jobs(self): return list(self.jobs.values())
        def remove_job(self, job_id): self.jobs.pop(job_id)
        def add_job(self, _fn, _trigger, *, id, **_kwargs): self.jobs[id] = Job(id)

    fake = Scheduler()
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SYNC_TIMES", ["19:00"])
    monkeypatch.setattr(scheduler_module, "get_control_session", sessions)
    monkeypatch.setattr(scheduler_module, "_scheduler", fake)
    scheduler_module.refresh_user_jobs(linked_id)
    scheduler_module.refresh_user_jobs(unlinked_id)

    linked_jobs = {job_id for job_id in fake.jobs if linked_id in job_id}
    unlinked_jobs = {job_id for job_id in fake.jobs if unlinked_id in job_id}
    assert any(job_id.endswith("morning_watch") for job_id in linked_jobs)
    assert any(job_id.endswith("morning_deadline") for job_id in linked_jobs)
    assert any(job_id.endswith("weekly_summary") for job_id in linked_jobs)
    assert any(job_id.endswith("notification_outbox") for job_id in linked_jobs)
    assert len(unlinked_jobs) == 1
    assert next(iter(unlinked_jobs)).endswith("sync_0")
    engine.dispose()
