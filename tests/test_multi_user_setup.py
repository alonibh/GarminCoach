from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

import config
import setup_routes
from control_db import ControlBase, User, create_control_engine


class FakeRegistry:
    def begin_login(self, user_id: str, email: str, password: str) -> str:
        assert user_id and email == "athlete@garmin.test" and password == "secret"
        return "connected"


def test_required_setup_flow_activates_only_after_garmin_connection(monkeypatch, tmp_path):
    engine = create_control_engine(tmp_path / "control.db")
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    user_id = str(uuid4())
    with Session.begin() as session:
        session.add(User(id=user_id, email="athlete@example.com", status="onboarding"))

    @contextmanager
    def sessions():
        with Session.begin() as session:
            yield session

    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=user_id)))
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(setup_routes, "get_control_session", sessions)
    monkeypatch.setattr(setup_routes, "get_garmin_registry", lambda: FakeRegistry())
    started = []
    monkeypatch.setattr(
        setup_routes.sync_runner,
        "try_start_sync",
        lambda *, full: started.append(full),
    )

    setup_routes.accept_privacy_notice(request, accepted="yes")
    with Session() as session:
        user = session.get(User, user_id)
        assert user.status == "onboarding"
        assert user.onboarding_step == "timezone"
        assert user.consented_at is not None

    setup_routes.choose_timezone(request, timezone_name="Asia/Jerusalem")
    with Session() as session:
        user = session.get(User, user_id)
        assert user.status == "onboarding"
        assert user.timezone == "Asia/Jerusalem"
        assert user.onboarding_step == "garmin"

    response = setup_routes.connect_garmin(
        request,
        garmin_email="athlete@garmin.test",
        garmin_password="secret",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    with Session() as session:
        user = session.get(User, user_id)
        assert user.status == "active"
        assert user.garmin_connected is True
        assert user.onboarding_step == "complete"
    assert started == [True]
    engine.dispose()


def test_invalid_timezone_does_not_modify_user(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    response = setup_routes.choose_timezone(
        SimpleNamespace(state=SimpleNamespace()), timezone_name="Not/A_Timezone"
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding?error=invalid_timezone"
