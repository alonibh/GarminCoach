from __future__ import annotations
import sqlite3
import os
from pathlib import Path
import pytest
from dataclasses import replace
import config
from guarded_restore import RestoreJournalError, RestoreJournalPersistenceError, RestoreStage, TargetRestoreState, create_restore_journal, create_restore_plan, load_restore_journal, update_restore_journal
from guarded_restore_staging import StagedVerificationError, StagingJournalPersistenceError, StagingManualCleanupRequiredError, StagingOwnershipIndeterminateError, StagingSourceError, SyntheticDestinationError, SyntheticRestoreTarget, stage_and_verify_synthetic_restore
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

def _assert_verification_failure(validated, journal, root, destinations, unlinked):
    source = [(validated.directory / entry.filename).read_bytes() for entry in validated.entries]
    destination = [item.path.read_bytes() for item in destinations]
    with pytest.raises(StagedVerificationError) as raised:
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert str(raised.value) == "Staged artifact verification failed"
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == (TargetRestoreState.STAGED, TargetRestoreState.STAGED)
    assert len(_stage_files(root)) == 2 and not [path for path in unlinked if path.name.endswith(".sqlite.staged")]
    assert [(validated.directory / entry.filename).read_bytes() for entry in validated.entries] == source and [item.path.read_bytes() for item in destinations] == destination
    return raised.value

@pytest.mark.parametrize("kind", ["metadata", "sha_source", "sha_ordinary", "inspection", "fingerprint", "markers"])
def test_verification_operational_failures_are_sanitized_and_preserve_owned_artifacts(tmp_path, monkeypatch, kind):
    import guarded_restore_staging as staging
    from operator_storage import DatabaseIntegrityError
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original_unlink = staging.os.unlink; unlinked = []
    def recording_unlink(path, *args, **kwargs):
        unlinked.append(Path(path)); return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(staging.os, "unlink", recording_unlink)
    raw = "C:/private/path.sqlite fake sqlite detail tenant:bad"
    if kind == "metadata":
        original_stat, original_impl, verifying = Path.stat, staging._verify_impl, [False]
        def fail_staged_stat(path, *args, **kwargs):
            if verifying[0] and path.name.endswith(".sqlite.staged"): raise OSError(raw)
            return original_stat(path, *args, **kwargs)
        def only_during_verification(entry, path):
            verifying[0] = True
            try: return original_impl(entry, path)
            finally: verifying[0] = False
        monkeypatch.setattr(Path, "stat", fail_staged_stat)
        monkeypatch.setattr(staging, "_verify_impl", only_during_verification)
    elif kind == "sha_source": monkeypatch.setattr(staging, "_sha", lambda path: (_ for _ in ()).throw(StagingSourceError(raw)))
    elif kind == "sha_ordinary": monkeypatch.setattr(staging, "_sha", lambda path: (_ for _ in ()).throw(RuntimeError(raw)))
    elif kind == "inspection": monkeypatch.setattr(staging, "inspect_sqlite", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(raw)))
    elif kind == "fingerprint": monkeypatch.setattr(staging, "schema_fingerprint", lambda path: (_ for _ in ()).throw(DatabaseIntegrityError(raw)))
    else: monkeypatch.setattr(staging, "migration_markers", lambda path, kind: (_ for _ in ()).throw(DatabaseIntegrityError(raw)))
    error = _assert_verification_failure(validated, journal, root, destinations, unlinked)
    assert raw not in str(error) and error.__cause__ is not None

@pytest.mark.parametrize("kind", ["hash", "unreadable", "quick", "integrity", "foreign_keys", "fingerprint_malformed", "fingerprint_mismatch", "markers_malformed", "ledger", "keys", "state"])
def test_verification_mismatches_are_normalized(tmp_path, monkeypatch, kind):
    import guarded_restore_staging as staging
    from dataclasses import replace as dataclass_replace
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original_unlink = staging.os.unlink; unlinked = []
    monkeypatch.setattr(staging.os, "unlink", lambda path, *args, **kwargs: (unlinked.append(Path(path)), original_unlink(path, *args, **kwargs))[1])
    if kind == "hash": monkeypatch.setattr(staging, "_sha", lambda path: "0" * 64)
    elif kind in {"unreadable", "quick", "integrity", "foreign_keys"}:
        original = staging.inspect_sqlite
        field = {"unreadable": "readable", "quick": "quick_check_ok", "integrity": "integrity_check_ok", "foreign_keys": "foreign_keys_ok"}[kind]
        monkeypatch.setattr(staging, "inspect_sqlite", lambda *args, **kwargs: dataclass_replace(original(*args, **kwargs), **{field: False}))
    elif kind.startswith("fingerprint"):
        monkeypatch.setattr(staging, "schema_fingerprint", lambda path: "not-a-fingerprint" if kind == "fingerprint_malformed" else "0" * 64)
    else:
        original = staging.migration_markers
        def marker_mismatch(path, target_kind):
            result = original(path, target_kind)
            if kind == "markers_malformed": return {"ledger": result["ledger"], "keys": result["keys"]}
            if kind == "ledger": return {**result, "ledger": "wrong"}
            if kind == "keys": return {**result, "keys": ["wrong"]}
            return {**result, "state": "wrong"}
        monkeypatch.setattr(staging, "migration_markers", marker_mismatch)
    error = _assert_verification_failure(validated, journal, root, destinations, unlinked)
    assert error.__cause__ is None

@pytest.mark.parametrize("fail_after, expected", [(0, (TargetRestoreState.STAGED, TargetRestoreState.STAGED)), (1, (TargetRestoreState.STAGED_VERIFIED, TargetRestoreState.STAGED))])
def test_first_and_later_verification_failures_preserve_exact_target_progress(tmp_path, monkeypatch, fail_after, expected):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original, calls = staging._verify, [0]
    def fail_selected(entry, path):
        if calls[0] == fail_after: raise StagedVerificationError("Staged artifact verification failed")
        calls[0] += 1; return original(entry, path)
    monkeypatch.setattr(staging, "_verify", fail_selected)
    with pytest.raises(StagedVerificationError):
        stage_and_verify_synthetic_restore(operation_id=journal.operation_id, validated_backup=validated, destinations=destinations, fixture_root=root, journal_root=config.OPERATOR_RESTORE_ROOT)
    current = load_restore_journal(journal.operation_id, root=config.OPERATOR_RESTORE_ROOT)
    assert current.stage is RestoreStage.FAILED_SAFE and _states(current) == expected and len(_stage_files(root)) == 2

@pytest.mark.skipif(os.name == "nt", reason="POSIX private-mode verification")
def test_posix_permission_helper_exception_is_sanitized_and_preserves_owned_artifacts(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original, original_unlink = staging.permission_health, staging.os.unlink; unlinked = []
    raw = "/private/operator/path.sqlite sqlite detail tenant:fake"
    def fail_staged_permission(path, directory=False):
        if Path(path).name.endswith(".sqlite.staged"): raise RuntimeError(raw)
        return original(path, directory=directory)
    monkeypatch.setattr(staging, "permission_health", fail_staged_permission)
    monkeypatch.setattr(staging.os, "unlink", lambda path, *args, **kwargs: (unlinked.append(Path(path)), original_unlink(path, *args, **kwargs))[1])
    error = _assert_verification_failure(validated, journal, root, destinations, unlinked)
    assert raw not in str(error) and isinstance(error.__cause__, RuntimeError)

@pytest.mark.skipif(os.name == "nt", reason="POSIX private-mode verification")
def test_posix_non_private_permission_result_is_a_mismatch(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original, original_unlink = staging.permission_health, staging.os.unlink; unlinked = []
    def broad_staged_permission(path, directory=False):
        if Path(path).name.endswith(".sqlite.staged"): return "permissions_too_broad"
        return original(path, directory=directory)
    monkeypatch.setattr(staging, "permission_health", broad_staged_permission)
    monkeypatch.setattr(staging.os, "unlink", lambda path, *args, **kwargs: (unlinked.append(Path(path)), original_unlink(path, *args, **kwargs))[1])
    error = _assert_verification_failure(validated, journal, root, destinations, unlinked)
    assert error.__cause__ is None

def test_staged_size_mismatch_preserves_both_owned_artifacts(tmp_path, monkeypatch):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original_verify, original_unlink = staging._verify, staging.os.unlink; modified, unlinked = [None], []
    def enlarge_first_staged(entry, path):
        if modified[0] is None:
            with Path(path).open("ab") as stream: stream.write(b"x")
            modified[0] = Path(path)
        return original_verify(entry, path)
    monkeypatch.setattr(staging, "_verify", enlarge_first_staged)
    monkeypatch.setattr(staging.os, "unlink", lambda path, *args, **kwargs: (unlinked.append(Path(path)), original_unlink(path, *args, **kwargs))[1])
    error = _assert_verification_failure(validated, journal, root, destinations, unlinked)
    files = _stage_files(root)
    assert error.__cause__ is None and modified[0] in files and modified[0].stat().st_size != validated.entries[0].size_bytes
    assert (validated.directory / validated.entries[1].filename).read_bytes() == files[1].read_bytes()

@pytest.mark.parametrize("shape", ["extra", "duplicate", "unordered"])
def test_migration_marker_shape_mismatches_are_normalized(tmp_path, monkeypatch, shape):
    import guarded_restore_staging as staging
    validated, journal, root, destinations = _prepared(tmp_path, monkeypatch)
    original, original_unlink = staging.migration_markers, staging.os.unlink; unlinked = []
    def malformed_shape(path, kind):
        result = original(path, kind)
        if shape == "extra": return {**result, "unexpected": "value"}
        if shape == "duplicate": return {**result, "keys": [result["keys"][0], result["keys"][0]]}
        return {**result, "keys": ["z", "a"]}
    monkeypatch.setattr(staging, "migration_markers", malformed_shape)
    monkeypatch.setattr(staging.os, "unlink", lambda path, *args, **kwargs: (unlinked.append(Path(path)), original_unlink(path, *args, **kwargs))[1])
    error = _assert_verification_failure(validated, journal, root, destinations, unlinked)
    assert error.__cause__ is None

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
