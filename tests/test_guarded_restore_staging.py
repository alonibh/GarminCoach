from __future__ import annotations
import sqlite3
import os
from pathlib import Path
import pytest
from dataclasses import replace
import config
from guarded_restore import RestoreJournalError, RestoreJournalPersistenceError, RestoreStage, TargetRestoreState, create_restore_journal, create_restore_plan, load_restore_journal, update_restore_journal
from guarded_restore_staging import StagingJournalPersistenceError, StagingManualCleanupRequiredError, StagingOwnershipIndeterminateError, StagingSourceError, SyntheticDestinationError, SyntheticRestoreTarget, stage_and_verify_synthetic_restore
from verified_backup import create_verified_backup, load_validated_backup_snapshot

def _db(path: Path, ledger: str, key: str):
    path.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(path); c.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)"); c.execute(f"CREATE TABLE {ledger}({key} TEXT PRIMARY KEY)"); c.execute(f"INSERT INTO {ledger} VALUES ('base')"); c.commit(); c.close()
def _prepared(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"PROJECT_ROOT",tmp_path); monkeypatch.setattr(config,"CONTROL_DB_PATH",tmp_path/"data"/"control.db"); monkeypatch.setattr(config,"DB_PATH",tmp_path/"data"/"single.db"); monkeypatch.setattr(config,"MULTI_USER_DATA_ROOT",tmp_path/"data"/"users"); monkeypatch.setattr(config,"OPERATOR_BACKUP_ROOT",tmp_path/"backups"); monkeypatch.setattr(config,"OPERATOR_RESTORE_ROOT",tmp_path/"journals"); monkeypatch.setattr(config,"MULTI_USER_ENABLED",False)
    _db(config.CONTROL_DB_PATH,"migration_versions","version"); _db(config.DB_PATH,"app_migrations","migration_key")
    backup=create_verified_backup(tmp_path/"backups"); validated=load_validated_backup_snapshot(backup)
    plan=create_restore_plan(selected_backup_id=validated.backup_id,selected_backup_manifest_sha256=validated.manifest_sha256,expected_application_commit=validated.application_commit,runtime_mode="single_user",target_keys=("control","single-user"))
    journal=create_restore_journal(plan,root=config.OPERATOR_RESTORE_ROOT); update_restore_journal(journal.operation_id,root=config.OPERATOR_RESTORE_ROOT,stage=RestoreStage.VERIFIED); update_restore_journal(journal.operation_id,root=config.OPERATOR_RESTORE_ROOT,stage=RestoreStage.CURRENT_SNAPSHOT_CREATED,safety_backup_id="20260801T120001Z-a1b2c3d4")
    root=tmp_path/"fixture"; root.mkdir();
    if __import__('os').name!='nt': __import__('os').chmod(root,0o700)
    a=root/"control.sqlite"; b=root/"single.sqlite"; _db(a,"migration_versions","version"); _db(b,"app_migrations","migration_key")
    if __import__('os').name!='nt': __import__('os').chmod(a.parent,0o700)
    return validated,journal,root,(SyntheticRestoreTarget("control","control",a),SyntheticRestoreTarget("single-user","single_user",b))
def test_synthetic_staging_reaches_replacement_ready_without_destination_mutation(tmp_path,monkeypatch):
    validated,journal,root,destinations=_prepared(tmp_path,monkeypatch); before=[d.path.read_bytes() for d in destinations]
    result=stage_and_verify_synthetic_restore(operation_id=journal.operation_id,validated_backup=validated,destinations=destinations,fixture_root=root,journal_root=config.OPERATOR_RESTORE_ROOT)
    assert len(result.artifacts)==2 and [d.path.read_bytes() for d in destinations]==before
    assert __import__('guarded_restore').load_restore_journal(journal.operation_id,root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.REPLACEMENT_READY
def test_configured_destination_is_refused(tmp_path,monkeypatch):
    validated,journal,root,destinations=_prepared(tmp_path,monkeypatch)
    bad=(SyntheticRestoreTarget("control","control",config.CONTROL_DB_PATH),destinations[1])
    with pytest.raises(SyntheticDestinationError): stage_and_verify_synthetic_restore(operation_id=journal.operation_id,validated_backup=validated,destinations=bad,fixture_root=root,journal_root=config.OPERATOR_RESTORE_ROOT)
def test_forged_or_stale_snapshot_is_refused(tmp_path,monkeypatch):
    validated,journal,root,destinations=_prepared(tmp_path,monkeypatch)
    with pytest.raises(StagingSourceError): stage_and_verify_synthetic_restore(operation_id=journal.operation_id,validated_backup=replace(validated,backup_id="20260801T120000Z-a1b2c3d4"),destinations=destinations,fixture_root=root,journal_root=config.OPERATOR_RESTORE_ROOT)

def _stage_files(root: Path):
    return sorted(root.glob(".garmincoach-restore-stage-*/*.sqlite.staged"))

def _states(journal):
    return tuple(item.state for item in journal.targets)

def test_first_staged_journal_failure_compensates_only_unrecorded_artifact(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    source = [entry.path.read_bytes() for entry in destinations]
    unrelated = root / "unrelated.txt"; unrelated.write_text("keep", encoding="utf-8")
    original = staging.update_restore_journal
    def fail_first(*args, **kwargs):
        if kwargs.get("target_key") == "control" and kwargs.get("target_state") is TargetRestoreState.STAGED:
            raise RestoreJournalError("injected")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_first)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == (TargetRestoreState.PENDING, TargetRestoreState.PENDING)
    assert not _stage_files(root) and not list(root.glob(".garmincoach-restore-stage-*/*.partial"))
    assert unrelated.read_text(encoding="utf-8") == "keep" and [entry.path.read_bytes() for entry in destinations] == source

def test_later_staged_journal_failure_preserves_owned_artifact(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original = staging.update_restore_journal
    def fail_second(*args, **kwargs):
        if kwargs.get("target_key") == "single-user" and kwargs.get("target_state") is TargetRestoreState.STAGED:
            raise RestoreJournalPersistenceError("injected")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_second)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    files = _stage_files(root)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == (TargetRestoreState.STAGED, TargetRestoreState.PENDING)
    assert len(files) == 1 and files[0].name.startswith("000-control")

def test_unrecorded_artifact_unlink_failure_requires_manual_cleanup(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original_update, original_unlink = staging.update_restore_journal, staging.os.unlink
    def fail_staged(*args, **kwargs):
        if kwargs.get("target_state") is TargetRestoreState.STAGED:
            raise RestoreJournalError("injected")
        return original_update(*args, **kwargs)
    def fail_exact(path, *args, **kwargs):
        if str(path).endswith(".sqlite.staged"):
            raise OSError("injected")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_staged); monkeypatch.setattr(staging.os, "unlink", fail_exact)
    with pytest.raises(StagingManualCleanupRequiredError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    assert load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.RESTORE_STAGED
    assert len(_stage_files(root)) == 1

def test_initial_global_staging_transition_failure_reaches_failed_safe_without_artifacts(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original = staging.update_restore_journal
    def fail_initial(*args, **kwargs):
        if kwargs.get("stage") is RestoreStage.RESTORE_STAGED: raise RestoreJournalError("injected")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_initial)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == (TargetRestoreState.PENDING, TargetRestoreState.PENDING)
    assert not _stage_files(root)

@pytest.mark.skipif(os.name == "nt", reason="directory fsync durability is unavailable on Windows")
def test_unrecorded_artifact_directory_fsync_failure_requires_manual_cleanup(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    import stat
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    source = [(validated.directory / entry.filename).read_bytes() for entry in validated.entries]
    destination = [item.path.read_bytes() for item in destinations]
    original_update, original_unlink, original_fsync = staging.update_restore_journal, staging.os.unlink, staging.os.fsync
    compensation_artifact_unlinked, normal_directory_fsyncs, failed_safe, attempted = [False], [0], [False], []
    def fail_staged(*args, **kwargs):
        attempted.append(kwargs)
        if kwargs.get("target_state") is TargetRestoreState.STAGED: raise RestoreJournalError("injected")
        return original_update(*args, **kwargs)
    def unlink_then_mark(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if Path(path).name.endswith(".sqlite.staged"): compensation_artifact_unlinked[0] = True
        return result
    def fail_only_compensation_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            if compensation_artifact_unlinked[0]:
                failed_safe[0] = True
                raise OSError("injected")
            normal_directory_fsyncs[0] += 1
        return original_fsync(fd)
    monkeypatch.setattr(staging, "update_restore_journal", fail_staged); monkeypatch.setattr(staging.os, "unlink", unlink_then_mark); monkeypatch.setattr(staging.os, "fsync", fail_only_compensation_directory_fsync)
    with pytest.raises(StagingManualCleanupRequiredError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    directories = list(root.glob(".garmincoach-restore-stage-*"))
    assert any(item.get("target_key") == "control" and item.get("target_state") is TargetRestoreState.STAGED for item in attempted)
    assert compensation_artifact_unlinked[0] and failed_safe[0] and normal_directory_fsyncs[0] >= 1
    assert current.stage is RestoreStage.RESTORE_STAGED and _states(current) == (TargetRestoreState.PENDING, TargetRestoreState.PENDING)
    assert not _stage_files(root) and not list(root.glob(".garmincoach-restore-stage-*/*.partial"))
    assert len(directories) == 1 and directories[0].is_dir()
    assert [(validated.directory / entry.filename).read_bytes() for entry in validated.entries] == source and [item.path.read_bytes() for item in destinations] == destination

@pytest.mark.parametrize("target_key, expected", [("control", (TargetRestoreState.STAGED, TargetRestoreState.STAGED)), ("single-user", (TargetRestoreState.STAGED_VERIFIED, TargetRestoreState.STAGED))])
def test_verified_target_journal_failures_preserve_exact_owned_progress(tmp_path, monkeypatch, target_key, expected):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original = staging.update_restore_journal
    def fail_target(*args, **kwargs):
        if kwargs.get("target_key") == target_key and kwargs.get("target_state") is TargetRestoreState.STAGED_VERIFIED: raise RestoreJournalError("injected")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_target)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == expected and len(_stage_files(root)) == 2

@pytest.mark.parametrize("stage, states", [(RestoreStage.STAGED_VERIFIED, (TargetRestoreState.STAGED, TargetRestoreState.STAGED)), (RestoreStage.REPLACEMENT_READY, (TargetRestoreState.STAGED_VERIFIED, TargetRestoreState.STAGED_VERIFIED))])
def test_global_journal_failures_preserve_owned_artifacts(tmp_path, monkeypatch, stage, states):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original = staging.update_restore_journal
    def fail_global(*args, **kwargs):
        if kwargs.get("stage") is stage: raise RestoreJournalPersistenceError("injected")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_global)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == states and len(_stage_files(root)) == 2

def test_failed_safe_persistence_failure_leaves_prior_journal_and_compensates_unrecorded(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original = staging.update_restore_journal
    def fail_transitions(*args, **kwargs):
        if kwargs.get("target_state") is TargetRestoreState.STAGED or kwargs.get("stage") is RestoreStage.FAILED_SAFE:
            raise RestoreJournalPersistenceError("injected")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_transitions)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.RESTORE_STAGED and _states(current) == (TargetRestoreState.PENDING, TargetRestoreState.PENDING)
    assert not _stage_files(root)

def test_first_persisted_staged_transition_exception_preserves_owned_artifact(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    sources, backup = [item.path.read_bytes() for item in destinations], [ (validated.directory / entry.filename).read_bytes() for entry in validated.entries ]
    original, original_unlink = staging.update_restore_journal, staging.os.unlink; unlinked = []
    def persist_then_fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("target_key") == "control" and kwargs.get("target_state") is TargetRestoreState.STAGED:
            raise RestoreJournalPersistenceError("injected")
        return result
    def recording_unlink(path, *args, **kwargs):
        unlinked.append(Path(path)); return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", persist_then_fail); monkeypatch.setattr(staging.os, "unlink", recording_unlink)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current, files = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT), _stage_files(root)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == (TargetRestoreState.STAGED, TargetRestoreState.PENDING)
    assert len(files) == 1 and not [path for path in unlinked if path.name.endswith(".sqlite.staged")]
    assert [item.path.read_bytes() for item in destinations] == sources and [(validated.directory / entry.filename).read_bytes() for entry in validated.entries] == backup

def test_later_persisted_staged_transition_exception_preserves_both_owned_artifacts(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original, original_unlink = staging.update_restore_journal, staging.os.unlink; unlinked = []
    def persist_then_fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("target_key") == "single-user" and kwargs.get("target_state") is TargetRestoreState.STAGED:
            raise RestoreJournalError("injected")
        return result
    def recording_unlink(path, *args, **kwargs):
        unlinked.append(Path(path)); return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", persist_then_fail); monkeypatch.setattr(staging.os, "unlink", recording_unlink)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == (TargetRestoreState.STAGED, TargetRestoreState.STAGED)
    assert len(_stage_files(root)) == 2 and not [path for path in unlinked if path.name.endswith(".sqlite.staged")]

def test_persisted_staged_transition_with_failed_safe_write_preserves_artifact(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original, original_unlink = staging.update_restore_journal, staging.os.unlink; unlinked = []
    def persist_then_fail(*args, **kwargs):
        if kwargs.get("stage") is RestoreStage.FAILED_SAFE: raise RestoreJournalPersistenceError("injected")
        result = original(*args, **kwargs)
        if kwargs.get("target_key") == "control" and kwargs.get("target_state") is TargetRestoreState.STAGED:
            raise RestoreJournalPersistenceError("injected")
        return result
    def recording_unlink(path, *args, **kwargs):
        unlinked.append(Path(path)); return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", persist_then_fail); monkeypatch.setattr(staging.os, "unlink", recording_unlink)
    with pytest.raises(StagingJournalPersistenceError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.RESTORE_STAGED and _states(current) == (TargetRestoreState.STAGED, TargetRestoreState.PENDING)
    assert len(_stage_files(root)) == 1 and not [path for path in unlinked if path.name.endswith(".sqlite.staged")]

def test_ownership_reload_failure_preserves_evidence_without_failed_safe(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original_update, original_load, original_unlink = staging.update_restore_journal, staging.load_restore_journal, staging.os.unlink
    loads, unlinked, updates = [0], [], []
    def fail_after_update(*args, **kwargs):
        updates.append(kwargs)
        result = original_update(*args, **kwargs)
        if kwargs.get("target_key") == "control" and kwargs.get("target_state") is TargetRestoreState.STAGED: raise RestoreJournalPersistenceError("injected")
        return result
    def fail_reconciliation(*args, **kwargs):
        loads[0] += 1
        if loads[0] == 2: raise RestoreJournalError("injected")
        return original_load(*args, **kwargs)
    def recording_unlink(path, *args, **kwargs):
        unlinked.append(Path(path)); return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", fail_after_update); monkeypatch.setattr(staging, "load_restore_journal", fail_reconciliation); monkeypatch.setattr(staging.os, "unlink", recording_unlink)
    with pytest.raises(StagingOwnershipIndeterminateError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    assert len(_stage_files(root)) == 1 and not unlinked and not [item for item in updates if item.get("stage") is RestoreStage.FAILED_SAFE]

def test_unexpected_durable_target_state_preserves_artifact_without_normalization(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original_update, original_load, original_unlink = staging.update_restore_journal, staging.load_restore_journal, staging.os.unlink
    loads, unlinked, updates = [0], [], []
    def persist_then_fail(*args, **kwargs):
        updates.append(kwargs)
        result = original_update(*args, **kwargs)
        if kwargs.get("target_key") == "control" and kwargs.get("target_state") is TargetRestoreState.STAGED: raise RestoreJournalError("injected")
        return result
    def unexpected_second_load(*args, **kwargs):
        loads[0] += 1; result = original_load(*args, **kwargs)
        if loads[0] == 2: return replace(result, targets=(replace(result.targets[0], state=TargetRestoreState.STAGED_VERIFIED), result.targets[1]))
        return result
    def recording_unlink(path, *args, **kwargs):
        unlinked.append(Path(path)); return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging, "update_restore_journal", persist_then_fail); monkeypatch.setattr(staging, "load_restore_journal", unexpected_second_load); monkeypatch.setattr(staging.os, "unlink", recording_unlink)
    with pytest.raises(StagingOwnershipIndeterminateError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    assert len(_stage_files(root)) == 1 and not unlinked and not [item for item in updates if item.get("stage") is RestoreStage.FAILED_SAFE]

@pytest.mark.skipif(os.name != "nt", reason="Windows binary descriptor regression")
def test_windows_staging_uses_binary_regular_file_descriptors(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    before = [item.path.read_bytes() for item in destinations]
    real_open = os.open; opens = []
    def recording_open(path, flags, *args):
        text = str(path)
        if text.startswith(str(validated.directory)) or text.startswith(str(root)):
            opens.append((Path(path).name, flags))
        return real_open(path, flags, *args)
    monkeypatch.setattr(staging.os, "open", recording_open)
    result = stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    assert __import__('guarded_restore').load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT).stage is RestoreStage.REPLACEMENT_READY
    assert [item.path.read_bytes() for item in destinations] == before
    for artifact, entry in zip(result.artifacts, validated.entries):
        assert artifact.path.stat().st_size == entry.size_bytes
        assert artifact.path.read_bytes() == (validated.directory / entry.filename).read_bytes()
    source = [flags for name, flags in opens if name.endswith(".sqlite") and not name.endswith(".staged")]
    partial = [flags for name, flags in opens if name.endswith(".partial")]
    final = [flags for name, flags in opens if name.endswith(".staged")]
    assert source and partial and final
    assert all(flags & staging._BINARY_FLAG for flags in source + partial + final)
    assert all((flags & os.O_WRONLY) == 0 for flags in source + final)
    assert all(flags & os.O_WRONLY and flags & os.O_CREAT and flags & os.O_EXCL for flags in partial)
