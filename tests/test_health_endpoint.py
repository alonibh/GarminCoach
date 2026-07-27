from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

import app
import config


def test_health_is_public_in_legacy_mode_without_application_work(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)
    monkeypatch.setattr(config, "APP_USERNAME", "protected")
    monkeypatch.setattr(app, "get_control_session", lambda: (_ for _ in ()).throw(AssertionError("database read")))
    monkeypatch.setattr(app, "tenant_scope", lambda _tenant: (_ for _ in ()).throw(AssertionError("tenant resolution")))

    response = TestClient(app.app).get("/health", follow_redirects=False)

    assert response.status_code == 200
    assert response.content == b'{"status":"ok"}'


def test_health_is_public_in_multi_user_mode_and_similar_path_is_not(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(app, "get_control_session", lambda: (_ for _ in ()).throw(AssertionError("database read")))
    monkeypatch.setattr(app, "resolve_web_session", lambda *_args: (_ for _ in ()).throw(AssertionError("session lookup")))
    monkeypatch.setattr(app, "tenant_scope", lambda _tenant: (_ for _ in ()).throw(AssertionError("tenant resolution")))

    client = TestClient(app.app)
    assert client.get("/health", follow_redirects=False).status_code == 200

    @contextmanager
    def control_session():
        yield object()

    monkeypatch.setattr(app, "get_control_session", control_session)
    monkeypatch.setattr(app, "resolve_web_session", lambda *_args: None)
    assert client.get("/health/details", follow_redirects=False).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_deployment_health_check_requires_direct_200():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000/health" in workflow
    assert "--write-out '%{http_code}'" in workflow
    assert '[ "$status" = "200" ]' in workflow
    assert "--location" not in workflow
