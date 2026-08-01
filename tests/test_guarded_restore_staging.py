from __future__ import annotations
import sqlite3
from pathlib import Path
import pytest
import config
from guarded_restore import RestoreStage, create_restore_journal, create_restore_plan, update_restore_journal
from guarded_restore_staging import SyntheticDestinationError, SyntheticRestoreTarget, stage_and_verify_synthetic_restore
from verified_backup import create_verified_backup, load_validated_backup

def _db(path: Path, ledger: str, key: str):
    path.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(path); c.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)"); c.execute(f"CREATE TABLE {ledger}({key} TEXT PRIMARY KEY)"); c.execute(f"INSERT INTO {ledger} VALUES ('base')"); c.commit(); c.close()
def _prepared(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"PROJECT_ROOT",tmp_path); monkeypatch.setattr(config,"CONTROL_DB_PATH",tmp_path/"data"/"control.db"); monkeypatch.setattr(config,"DB_PATH",tmp_path/"data"/"single.db"); monkeypatch.setattr(config,"MULTI_USER_DATA_ROOT",tmp_path/"data"/"users"); monkeypatch.setattr(config,"OPERATOR_BACKUP_ROOT",tmp_path/"backups"); monkeypatch.setattr(config,"OPERATOR_RESTORE_ROOT",tmp_path/"journals"); monkeypatch.setattr(config,"MULTI_USER_ENABLED",False)
    _db(config.CONTROL_DB_PATH,"migration_versions","version"); _db(config.DB_PATH,"app_migrations","migration_key")
    backup=create_verified_backup(tmp_path/"backups"); validated=load_validated_backup(backup); import hashlib
    plan=create_restore_plan(selected_backup_id=validated.manifest["backup_id"],selected_backup_manifest_sha256=hashlib.sha256((backup/"manifest.json").read_bytes()).hexdigest(),expected_application_commit=validated.manifest["application_commit"],runtime_mode="single_user",target_keys=("control","single-user"))
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
