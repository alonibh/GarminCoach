from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3

import pytest

import config
from guarded_restore import RestoreStage, create_restore_journal, create_restore_plan, load_restore_journal, update_restore_journal
from guarded_restore_replacement import ReplacementPreconditionError, RollbackCompletedError, replace_and_verify_synthetic_restore
from guarded_restore_staging import SyntheticRestoreTarget, stage_and_verify_synthetic_restore
from verified_backup import create_verified_backup, load_validated_backup_snapshot


def _db(path: Path, ledger: str, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute(f"CREATE TABLE {ledger}({key} TEXT PRIMARY KEY)")
    connection.execute(f"INSERT INTO {ledger} VALUES ('base')")
    connection.commit(); connection.close()
    if os.name != "nt": os.chmod(path, 0o600)


def _prepared(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONTROL_DB_PATH", tmp_path / "data" / "control.db")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "single.db")
    monkeypatch.setattr(config, "MULTI_USER_DATA_ROOT", tmp_path / "data" / "users")
    monkeypatch.setattr(config, "OPERATOR_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(config, "OPERATOR_RESTORE_ROOT", tmp_path / "journals")
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", False)
    _db(config.CONTROL_DB_PATH, "migration_versions", "version")
    _db(config.DB_PATH, "app_migrations", "migration_key")
    selected = load_validated_backup_snapshot(create_verified_backup(config.OPERATOR_BACKUP_ROOT))
    # The safety source is distinct and represents the fixture's current bytes.
    for path in (config.CONTROL_DB_PATH, config.DB_PATH):
        connection = sqlite3.connect(path); connection.execute("INSERT INTO sample(value) VALUES ('safety')"); connection.commit(); connection.close()
    safety = load_validated_backup_snapshot(create_verified_backup(config.OPERATOR_BACKUP_ROOT))
    plan = create_restore_plan(selected_backup_id=selected.backup_id, selected_backup_manifest_sha256=selected.manifest_sha256,
                               expected_application_commit=selected.application_commit, runtime_mode="single_user",
                               target_keys=("control", "single-user"))
    journal = create_restore_journal(plan, root=config.OPERATOR_RESTORE_ROOT)
    update_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT, stage=RestoreStage.VERIFIED)
    update_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT,
                           stage=RestoreStage.CURRENT_SNAPSHOT_CREATED, safety_backup_id=safety.backup_id)
    root = tmp_path / "fixture"; root.mkdir()
    if os.name != "nt": os.chmod(root, 0o700)
    destinations = []
    for entry, name in zip(safety.entries, ("control.sqlite", "single.sqlite")):
        destination = root / name
        shutil.copyfile(safety.directory / entry.filename, destination)
        if os.name != "nt": os.chmod(destination, 0o600)
        destinations.append(SyntheticRestoreTarget(entry.target_key, entry.kind, destination))
    destinations = tuple(destinations)
    staged = stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=selected,
                                                 destinations=destinations, fixture_root=root,
                                                 journal_root=config.OPERATOR_RESTORE_ROOT)
    return selected, safety, journal, root, destinations, staged


def test_successful_fixture_replacement_is_data_before_control(tmp_path, monkeypatch):
    selected, safety, journal, root, destinations, staged = _prepared(tmp_path, monkeypatch)
    import guarded_restore_replacement as replacement
    original, order = replacement.os.replace, []
    selected_paths = {artifact.path for artifact in staged.artifacts}
    def record(source, target):
        if Path(source) in selected_paths:
            order.append(Path(target).name)
        return original(source, target)
    monkeypatch.setattr(replacement.os, "replace", record)
    result = replace_and_verify_synthetic_restore(operation_id=journal.operation_id, selected_backup=selected, safety_backup=safety,
                                                   destinations=destinations, staging_result=staged, fixture_root=root,
                                                   journal_root=config.OPERATOR_RESTORE_ROOT)
    assert result.final_stage is RestoreStage.COMPLETED and not result.rollback_occurred
    assert order == ["single.sqlite", "control.sqlite"]
    assert load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.COMPLETED
    for entry, target in zip(selected.entries, destinations):
        assert target.path.read_bytes() == (selected.directory / entry.filename).read_bytes()
        if os.name != "nt": assert (target.path.stat().st_mode & 0o777) == 0o600
        assert not Path(str(target.path) + "-wal").exists() and not Path(str(target.path) + "-shm").exists()
    assert selected.directory.exists() and safety.directory.exists()


def test_configured_destination_is_refused_without_mutation(tmp_path, monkeypatch):
    selected, safety, journal, root, destinations, staged = _prepared(tmp_path, monkeypatch)
    before = destinations[0].path.read_bytes()
    bad = (SyntheticRestoreTarget("control", "control", config.CONTROL_DB_PATH), destinations[1])
    with pytest.raises(ReplacementPreconditionError):
        replace_and_verify_synthetic_restore(operation_id=journal.operation_id, selected_backup=selected, safety_backup=safety,
                                             destinations=bad, staging_result=staged, fixture_root=root,
                                             journal_root=config.OPERATOR_RESTORE_ROOT)
    assert destinations[0].path.read_bytes() == before
    assert load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.FAILED_SAFE


def test_destination_drift_after_staging_is_refused_without_mutation(tmp_path, monkeypatch):
    selected, safety, journal, root, destinations, staged = _prepared(tmp_path, monkeypatch)
    before = destinations[0].path.read_bytes()
    connection = sqlite3.connect(destinations[0].path); connection.execute("INSERT INTO sample(value) VALUES ('drift')"); connection.commit(); connection.close()
    with pytest.raises(ReplacementPreconditionError):
        replace_and_verify_synthetic_restore(operation_id=journal.operation_id, selected_backup=selected, safety_backup=safety,
                                             destinations=destinations, staging_result=staged, fixture_root=root,
                                             journal_root=config.OPERATOR_RESTORE_ROOT)
    assert destinations[0].path.read_bytes() != before
    assert load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.FAILED_SAFE


def test_replacement_failure_rolls_back_in_reverse_order(tmp_path, monkeypatch):
    selected, safety, journal, root, destinations, staged = _prepared(tmp_path, monkeypatch)
    import guarded_restore_replacement as replacement
    original, calls = replacement.os.replace, [0]
    def fail_control(source, target):
        if Path(source) == staged.artifacts[0].path:
            raise OSError("injected")
        return original(source, target)
    monkeypatch.setattr(replacement.os, "replace", fail_control)
    with pytest.raises(RollbackCompletedError):
        replace_and_verify_synthetic_restore(operation_id=journal.operation_id, selected_backup=selected, safety_backup=safety,
                                             destinations=destinations, staging_result=staged, fixture_root=root,
                                             journal_root=config.OPERATOR_RESTORE_ROOT)
    assert load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.FAILED_SAFE
    for entry, target in zip(safety.entries, destinations):
        assert target.path.read_bytes() == (safety.directory / entry.filename).read_bytes()


def test_symlink_sidecar_refuses_and_rolls_back(tmp_path, monkeypatch):
    if os.name == "nt": pytest.skip("symlink permissions are environment dependent")
    selected, safety, journal, root, destinations, staged = _prepared(tmp_path, monkeypatch)
    sidecar = Path(str(destinations[1].path) + "-wal"); sidecar.symlink_to(destinations[1].path)
    with pytest.raises(RollbackCompletedError):
        replace_and_verify_synthetic_restore(operation_id=journal.operation_id, selected_backup=selected, safety_backup=safety,
                                             destinations=destinations, staging_result=staged, fixture_root=root,
                                             journal_root=config.OPERATOR_RESTORE_ROOT)
    assert load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.FAILED_SAFE
