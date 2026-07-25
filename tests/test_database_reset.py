import inspect
from pathlib import Path
import sqlite3

import pytest

import app
import config
import database_reset


USER_ID = "00000000-0000-0000-0000-000000000001"


def _old_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE old_data(value TEXT)")
        connection.execute("INSERT INTO old_data VALUES ('destroy me')")
        connection.commit()
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()


def _integrity(path: Path) -> list[tuple[str]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA integrity_check").fetchall()
    finally:
        connection.close()


def test_reset_refuses_without_exact_destructive_confirmation(tmp_path):
    called = False

    def service_check():
        nonlocal called
        called = True

    with pytest.raises(
        database_reset.DatabaseResetError,
        match="confirm-destroy-all-data",
    ):
        database_reset.reset_all_databases(
            confirmed=False,
            quarantine_parent=tmp_path,
            service_stopped_check=service_check,
        )
    assert called is False
    assert database_reset.main([]) == 2


def test_discovery_deduplicates_configured_paths(tmp_path, monkeypatch):
    shared = tmp_path / "shared.db"
    tenant_root = tmp_path / "users"
    athlete = tenant_root / USER_ID / "athlete.db"
    _old_database(shared)
    _old_database(athlete)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", shared)
    monkeypatch.setattr(config, "DB_PATH", shared)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tenant_root)

    assert database_reset.discover_reset_paths() == [
        shared.resolve(),
        athlete.resolve(),
    ]


def test_reset_quarantines_databases_and_sidecars_then_recreates_schemas(
    tmp_path, monkeypatch
):
    control = tmp_path / "data" / "control.db"
    single = tmp_path / "single.db"
    tenant_root = tmp_path / "data" / "users"
    athlete = tenant_root / USER_ID / "athlete.db"
    for path in (control, single, athlete):
        _old_database(path)
        Path(f"{path}-wal").write_bytes(b"old wal")
        Path(f"{path}-shm").write_bytes(b"old shm")
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control)
    monkeypatch.setattr(config, "DB_PATH", single)
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tenant_root)
    environment_file = tmp_path / ".env"
    unrelated_file = tmp_path / "keep.txt"
    environment_file.write_text("SECRET=unchanged\n", encoding="utf-8")
    unrelated_file.write_text("keep me", encoding="utf-8")

    result = database_reset.reset_all_databases(
        confirmed=True,
        quarantine_parent=tmp_path / "quarantine",
        service_stopped_check=lambda: None,
    )

    assert set(result.targeted_paths) == {
        control.resolve(),
        single.resolve(),
        athlete.resolve(),
    }
    assert set(result.recreated_paths) == {
        control.resolve(),
        single.resolve(),
    }
    assert control.exists()
    assert single.exists()
    assert not athlete.exists()
    for path in (control, single):
        for suffix, old_content in (
            ("-wal", b"old wal"),
            ("-shm", b"old shm"),
        ):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                assert sidecar.read_bytes() != old_content
    assert not Path(f"{athlete}-wal").exists()
    assert not Path(f"{athlete}-shm").exists()
    assert {"users", "ask_coach_consents", "migration_versions"} <= _tables(
        control
    )
    assert {"activities", "planned_sessions", "training_programs"} <= _tables(
        single
    )
    assert "old_data" not in _tables(control)
    assert "old_data" not in _tables(single)
    assert _integrity(control) == [("ok",)]
    assert _integrity(single) == [("ok",)]
    assert environment_file.read_text(encoding="utf-8") == "SECRET=unchanged\n"
    assert unrelated_file.read_text(encoding="utf-8") == "keep me"

    quarantine = result.quarantine_path
    assert (quarantine / "control" / "control.db").exists()
    assert (quarantine / "control" / "control.db-wal").exists()
    assert (quarantine / "control" / "control.db-shm").exists()
    assert (quarantine / "single-user" / "single.db").exists()
    assert (quarantine / f"tenant-{USER_ID}" / "athlete.db").exists()
    manifest = (
        quarantine / "quarantine-manifest.json"
    ).read_text(encoding="utf-8")
    assert '"valid_backup": false' in manifest
    assert "destroy me" not in manifest


def test_reset_refuses_discovered_path_outside_configured_locations(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside.db"
    _old_database(outside)
    monkeypatch.setattr(
        database_reset, "discover_reset_paths", lambda: [outside]
    )

    with pytest.raises(
        database_reset.DatabaseResetError,
        match="outside configured data locations",
    ):
        database_reset.reset_all_databases(
            confirmed=True,
            quarantine_parent=tmp_path / "quarantine",
            service_stopped_check=lambda: None,
        )
    assert outside.exists()
    assert "old_data" in _tables(outside)


def test_reset_requires_service_to_be_stopped_before_modifying_files(
    tmp_path, monkeypatch
):
    control = tmp_path / "control.db"
    _old_database(control)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", control)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "single.db")
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tmp_path / "users")

    def active_service():
        raise database_reset.DatabaseResetError(
            "GarminCoach service must be stopped first"
        )

    with pytest.raises(
        database_reset.DatabaseResetError,
        match="service must be stopped",
    ):
        database_reset.reset_all_databases(
            confirmed=True,
            quarantine_parent=tmp_path / "quarantine",
            service_stopped_check=active_service,
        )
    assert control.exists()
    assert "old_data" in _tables(control)


def test_reset_failure_returns_nonzero(monkeypatch, capsys):
    def fail(**_kwargs):
        raise database_reset.DatabaseResetError("sanitized failure")

    monkeypatch.setattr(database_reset, "reset_all_databases", fail)

    assert (
        database_reset.main(["--confirm-destroy-all-data"])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sanitized failure" in captured.err


def test_normal_startup_never_invokes_database_reset():
    source = inspect.getsource(app.lifespan)
    assert "reset_all_databases" not in source
    assert "database_reset" not in source


def test_recovery_workflow_is_manual_and_doubly_guarded():
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "reset-production-databases.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert workflow.count("WIPE_ALL_GARMINCOACH_DATABASES") >= 3
    assert "--confirm-destroy-all-data" in workflow
    assert 'Environment="APP_WORKER_COUNT=1"' in workflow
    assert "--workers 1" in workflow
    assert "systemctl show garmincoach -p MainPID --value" in workflow
    assert "pgrep -x uvicorn" in workflow
