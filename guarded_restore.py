"""Pure guarded-restore planning, journal, and lock primitives (Phase 6B2A).

This module deliberately has no SQLite, backup, service, or application-lock
orchestration. Later phases may compose these bounded primitives only after
separate review.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
from uuid import UUID

import config
from operator_storage import has_symlink_component


PLAN_FORMAT = "garmincoach-guarded-restore-plan-v1"
JOURNAL_FORMAT = "garmincoach-guarded-restore-journal-v1"
CONFIRMATION_DOMAIN = "garmincoach-guarded-restore-confirmation-v1"
MAX_JOURNAL_BYTES = 128 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_OPERATION_ID = re.compile(r"^restore-\d{8}T\d{6}Z-[0-9a-f]{8}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RestorePlanError(ValueError):
    """Bounded planning-input failure."""


class RestoreJournalError(ValueError):
    """Bounded journal validation failure."""


class RestoreTransitionError(RestoreJournalError):
    """A requested state transition is not legal."""


class RestoreJournalPersistenceError(RuntimeError):
    """A journal cannot be safely persisted."""


class RestoreLockError(RuntimeError):
    """The dedicated restore lock is already held or unsafe."""


class RestoreStage(str, Enum):
    PRECHECK = "PRECHECK"
    VERIFIED = "VERIFIED"
    CURRENT_SNAPSHOT_CREATED = "CURRENT_SNAPSHOT_CREATED"
    RESTORE_STAGED = "RESTORE_STAGED"
    STAGED_VERIFIED = "STAGED_VERIFIED"
    REPLACEMENT_READY = "REPLACEMENT_READY"
    REPLACING = "REPLACING"
    REPLACED = "REPLACED"
    POSTCHECK_PASSED = "POSTCHECK_PASSED"
    COMPLETED = "COMPLETED"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED_SAFE = "FAILED_SAFE"
    FAILED_MANUAL_RECOVERY_REQUIRED = "FAILED_MANUAL_RECOVERY_REQUIRED"


class TargetRestoreState(str, Enum):
    PENDING = "PENDING"
    STAGED = "STAGED"
    STAGED_VERIFIED = "STAGED_VERIFIED"
    REPLACED = "REPLACED"
    ROLLED_BACK = "ROLLED_BACK"


class FinalResult(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED_SAFE = "FAILED_SAFE"
    FAILED_MANUAL_RECOVERY_REQUIRED = "FAILED_MANUAL_RECOVERY_REQUIRED"


_GLOBAL_TRANSITIONS = {
    RestoreStage.PRECHECK: {RestoreStage.VERIFIED, RestoreStage.FAILED_SAFE},
    RestoreStage.VERIFIED: {RestoreStage.CURRENT_SNAPSHOT_CREATED, RestoreStage.FAILED_SAFE},
    RestoreStage.CURRENT_SNAPSHOT_CREATED: {RestoreStage.RESTORE_STAGED, RestoreStage.FAILED_SAFE},
    RestoreStage.RESTORE_STAGED: {RestoreStage.STAGED_VERIFIED, RestoreStage.FAILED_SAFE},
    RestoreStage.STAGED_VERIFIED: {RestoreStage.REPLACEMENT_READY, RestoreStage.FAILED_SAFE},
    RestoreStage.REPLACEMENT_READY: {RestoreStage.REPLACING, RestoreStage.FAILED_SAFE},
    RestoreStage.REPLACING: {RestoreStage.REPLACED, RestoreStage.ROLLBACK_REQUIRED},
    RestoreStage.REPLACED: {RestoreStage.POSTCHECK_PASSED, RestoreStage.ROLLBACK_REQUIRED},
    RestoreStage.POSTCHECK_PASSED: {RestoreStage.COMPLETED, RestoreStage.ROLLBACK_REQUIRED},
    RestoreStage.ROLLBACK_REQUIRED: {RestoreStage.ROLLED_BACK, RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED},
    RestoreStage.ROLLED_BACK: {RestoreStage.FAILED_SAFE},
    RestoreStage.COMPLETED: set(),
    RestoreStage.FAILED_SAFE: set(),
    RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED: set(),
}
_TARGET_TRANSITIONS = {
    TargetRestoreState.PENDING: {TargetRestoreState.STAGED},
    TargetRestoreState.STAGED: {TargetRestoreState.STAGED_VERIFIED},
    TargetRestoreState.STAGED_VERIFIED: {TargetRestoreState.REPLACED},
    TargetRestoreState.REPLACED: {TargetRestoreState.ROLLED_BACK},
    TargetRestoreState.ROLLED_BACK: set(),
}
_TERMINAL_RESULTS = {
    RestoreStage.COMPLETED: FinalResult.COMPLETED,
    RestoreStage.FAILED_SAFE: FinalResult.FAILED_SAFE,
    RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED: FinalResult.FAILED_MANUAL_RECOVERY_REQUIRED,
}


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, error: type[Exception]) -> datetime:
    if not isinstance(value, str):
        raise error("Invalid restore timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
        return parsed
    except ValueError as exc:
        raise error("Invalid restore timestamp") from exc


def _safe(value: object, *, pattern: re.Pattern[str] = _SAFE_VALUE, error: type[Exception] = RestorePlanError) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise error("Invalid bounded restore metadata")
    return value


def _sha(value: object, error: type[Exception] = RestorePlanError) -> str:
    return _safe(value, pattern=_SHA256, error=error)


def _target_key(value: object, error: type[Exception] = RestorePlanError) -> str:
    key = _safe(value, error=error)
    if key in {"control", "single-user"}:
        return key
    if not key.startswith("tenant:"):
        raise error("Invalid restore target key")
    try:
        identifier = str(UUID(key[7:]))
    except (ValueError, AttributeError) as exc:
        raise error("Invalid restore target key") from exc
    if key != f"tenant:{identifier}":
        raise error("Invalid restore target key")
    return key


def _targets(value: object, mode: str, error: type[Exception] = RestorePlanError) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise error("Invalid restore target set")
    keys = tuple(_target_key(item, error) for item in value)
    if len(keys) != len(set(keys)) or keys[0] != "control":
        raise error("Invalid restore target set")
    if mode == "single_user":
        if keys != ("control", "single-user"):
            raise error("Invalid restore target set")
    elif "single-user" in keys:
        raise error("Invalid restore target set")
    return keys


def target_set_hash(*, backup_id: str, manifest_sha256: str, runtime_mode: str, target_keys: tuple[str, ...] | list[str]) -> str:
    backup_id = _safe(backup_id, pattern=_BACKUP_ID)
    manifest_sha256 = _sha(manifest_sha256)
    runtime_mode = _safe(runtime_mode)
    if runtime_mode not in {"single_user", "multi_user"}:
        raise RestorePlanError("Invalid restore runtime mode")
    targets = _targets(target_keys, runtime_mode)
    payload = {"format_version": PLAN_FORMAT, "selected_backup_id": backup_id, "selected_backup_manifest_sha256": manifest_sha256, "runtime_mode": runtime_mode, "target_keys": list(targets)}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def confirmation_value(*, target_hash: str, expected_application_commit: str) -> str:
    target_hash = _sha(target_hash)
    commit = _safe(expected_application_commit)
    if commit != "unknown" and not _COMMIT.fullmatch(commit):
        raise RestorePlanError("Invalid restore application commit")
    return hashlib.sha256(canonical_json({"domain": CONFIRMATION_DOMAIN, "target_set_hash": target_hash, "expected_application_commit": commit})).hexdigest()


@dataclass(frozen=True)
class RestorePlan:
    format_version: str
    selected_backup_id: str
    selected_backup_manifest_sha256: str
    expected_application_commit: str
    runtime_mode: str
    target_keys: tuple[str, ...]
    target_set_hash: str
    confirmation_value: str
    created_at: str


def create_restore_plan(*, selected_backup_id: str, selected_backup_manifest_sha256: str, expected_application_commit: str, runtime_mode: str, target_keys: tuple[str, ...] | list[str], created_at: str | None = None) -> RestorePlan:
    backup_id = _safe(selected_backup_id, pattern=_BACKUP_ID)
    manifest_hash = _sha(selected_backup_manifest_sha256)
    mode = _safe(runtime_mode)
    if mode not in {"single_user", "multi_user"}:
        raise RestorePlanError("Invalid restore runtime mode")
    targets = _targets(target_keys, mode)
    created = created_at or _now(); _timestamp(created, RestorePlanError)
    commit = _safe(expected_application_commit)
    if commit != "unknown" and not _COMMIT.fullmatch(commit):
        raise RestorePlanError("Invalid restore application commit")
    target_hash = target_set_hash(backup_id=backup_id, manifest_sha256=manifest_hash, runtime_mode=mode, target_keys=targets)
    return RestorePlan(PLAN_FORMAT, backup_id, manifest_hash, commit, mode, targets, target_hash, confirmation_value(target_hash=target_hash, expected_application_commit=commit), created)


@dataclass(frozen=True)
class TargetJournalFact:
    target_key: str
    state: TargetRestoreState
    wal_present: bool = False
    shm_present: bool = False
    wal_removed: bool = False
    shm_removed: bool = False
    replacement_intent: bool = False
    replacement_completed: bool = False
    rollback_intent: bool = False
    rollback_completed: bool = False


@dataclass(frozen=True)
class RestoreJournal:
    format_version: str
    operation_id: str
    selected_backup_id: str
    selected_backup_manifest_sha256: str
    safety_backup_id: str | None
    expected_application_commit: str
    runtime_mode: str
    target_keys: tuple[str, ...]
    target_set_hash: str
    confirmation_value: str
    stage: RestoreStage
    targets: tuple[TargetJournalFact, ...]
    created_at: str
    updated_at: str
    final_result: FinalResult | None


def _validate_journal(journal: RestoreJournal) -> None:
    if journal.format_version != JOURNAL_FORMAT:
        raise RestoreJournalError("Invalid restore journal")
    _safe(journal.operation_id, pattern=_OPERATION_ID, error=RestoreJournalError)
    try:
        plan = create_restore_plan(selected_backup_id=journal.selected_backup_id, selected_backup_manifest_sha256=journal.selected_backup_manifest_sha256, expected_application_commit=journal.expected_application_commit, runtime_mode=journal.runtime_mode, target_keys=journal.target_keys, created_at=journal.created_at)
    except RestorePlanError as exc:
        raise RestoreJournalError("Invalid restore journal") from exc
    if journal.target_set_hash != plan.target_set_hash or journal.confirmation_value != plan.confirmation_value:
        raise RestoreJournalError("Invalid restore journal")
    created, updated = _timestamp(journal.created_at, RestoreJournalError), _timestamp(journal.updated_at, RestoreJournalError)
    if updated < created or len(journal.targets) != len(journal.target_keys) or tuple(f.target_key for f in journal.targets) != journal.target_keys:
        raise RestoreJournalError("Invalid restore journal")
    for f in journal.targets:
        if not isinstance(f.state, TargetRestoreState):
            raise RestoreJournalError("Invalid restore journal")
        if any(type(b) is not bool for b in (f.wal_present, f.shm_present, f.wal_removed, f.shm_removed, f.replacement_intent, f.replacement_completed, f.rollback_intent, f.rollback_completed)):
            raise RestoreJournalError("Invalid restore journal")
        if f.wal_removed and not f.wal_present:
            raise RestoreJournalError("Invalid restore journal")
        if f.shm_removed and not f.shm_present:
            raise RestoreJournalError("Invalid restore journal")
        if f.replacement_completed and not f.replacement_intent:
            raise RestoreJournalError("Invalid restore journal")
        if f.rollback_completed and not f.rollback_intent:
            raise RestoreJournalError("Invalid restore journal")
        if (f.rollback_intent or f.rollback_completed) and not f.replacement_intent:
            raise RestoreJournalError("Invalid restore journal")
        if f.state is TargetRestoreState.REPLACED and (not f.replacement_intent or not f.replacement_completed):
            raise RestoreJournalError("Invalid restore journal")
        if f.state is TargetRestoreState.ROLLED_BACK and (not f.rollback_intent or not f.rollback_completed):
            raise RestoreJournalError("Invalid restore journal")
    states = [f.state for f in journal.targets]
    if journal.safety_backup_id is not None:
        _safe(journal.safety_backup_id, pattern=_BACKUP_ID, error=RestoreJournalError)
    terminal = _TERMINAL_RESULTS.get(journal.stage)
    if (terminal is None) != (journal.final_result is None) or (terminal is not None and journal.final_result != terminal):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage in {RestoreStage.PRECHECK, RestoreStage.VERIFIED} and (journal.safety_backup_id is not None or any(s is not TargetRestoreState.PENDING for s in states)):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage is RestoreStage.CURRENT_SNAPSHOT_CREATED and (journal.safety_backup_id is None or any(s is not TargetRestoreState.PENDING for s in states)):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage is RestoreStage.RESTORE_STAGED and any(s not in {TargetRestoreState.PENDING, TargetRestoreState.STAGED} for s in states):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage is RestoreStage.STAGED_VERIFIED and any(s not in {TargetRestoreState.STAGED, TargetRestoreState.STAGED_VERIFIED} for s in states):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage is RestoreStage.REPLACEMENT_READY and any(s is not TargetRestoreState.STAGED_VERIFIED for s in states):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage is RestoreStage.REPLACING and any(s not in {TargetRestoreState.STAGED_VERIFIED, TargetRestoreState.REPLACED} for s in states):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage in {RestoreStage.REPLACED, RestoreStage.POSTCHECK_PASSED, RestoreStage.COMPLETED} and any(s is not TargetRestoreState.REPLACED for s in states):
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage in {RestoreStage.ROLLED_BACK, RestoreStage.FAILED_SAFE} and TargetRestoreState.REPLACED in states:
        raise RestoreJournalError("Invalid restore journal")
    if journal.stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        # Current facts intentionally retain the exact partial rollback point.
        # The terminal stage itself proves the only legal entry was from
        # ROLLBACK_REQUIRED; no invented historical normalization is allowed.
        if any(state not in {TargetRestoreState.STAGED_VERIFIED, TargetRestoreState.REPLACED, TargetRestoreState.ROLLED_BACK} for state in states):
            raise RestoreJournalError("Invalid restore journal")


def _operation_id() -> str:
    return "restore-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def _private(path: Path, directory: bool = False) -> None:
    try: os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt": raise RestoreJournalPersistenceError("Restore journal permissions could not be set")


def _fsync(path: Path, directory: bool = False) -> None:
    if os.name == "nt": return
    try:
        fd = os.open(path, os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if directory else 0))
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError: pass


def validate_restore_root(root: Path | str | None = None) -> Path:
    original = Path(root if root is not None else config.OPERATOR_RESTORE_ROOT).expanduser()
    if not original.is_absolute(): original = Path(config.PROJECT_ROOT) / original
    if original == original.anchor or has_symlink_component(original):
        raise RestoreJournalError("Restore root is unsafe")
    resolved = original.resolve(strict=False); project = Path(config.PROJECT_ROOT).resolve()
    try: resolved.relative_to(project)
    except ValueError as exc: raise RestoreJournalError("Restore root is unsafe") from exc
    if resolved == project: raise RestoreJournalError("Restore root is unsafe")
    protected = [Path(config.CONTROL_DB_PATH).resolve(), Path(config.DB_PATH).resolve(), Path(config.MULTI_USER_DATA_ROOT).resolve(), Path(config.OPERATOR_BACKUP_ROOT).resolve()]
    for item in protected:
        if resolved == item or resolved in item.parents or item in resolved.parents:
            raise RestoreJournalError("Restore root overlaps protected storage")
    return resolved


def _operation_directory(root: Path, operation_id: str) -> Path:
    _safe(operation_id, pattern=_OPERATION_ID, error=RestoreJournalError)
    return root / f"operation-{operation_id}"


def _journal_path(root: Path, operation_id: str) -> Path:
    return _operation_directory(root, operation_id) / "journal.json"


def _journal_payload(journal: RestoreJournal) -> dict[str, Any]:
    return {
        "format_version": journal.format_version,
        "operation_id": journal.operation_id,
        "selected_backup_id": journal.selected_backup_id,
        "selected_backup_manifest_sha256": journal.selected_backup_manifest_sha256,
        "safety_backup_id": journal.safety_backup_id,
        "expected_application_commit": journal.expected_application_commit,
        "runtime_mode": journal.runtime_mode,
        "target_keys": list(journal.target_keys),
        "target_set_hash": journal.target_set_hash,
        "confirmation_value": journal.confirmation_value,
        "stage": journal.stage.value,
        "targets": [
            {
                "target_key": fact.target_key,
                "state": fact.state.value,
                "wal_present": fact.wal_present,
                "shm_present": fact.shm_present,
                "wal_removed": fact.wal_removed,
                "shm_removed": fact.shm_removed,
                "replacement_intent": fact.replacement_intent,
                "replacement_completed": fact.replacement_completed,
                "rollback_intent": fact.rollback_intent,
                "rollback_completed": fact.rollback_completed,
            }
            for fact in journal.targets
        ],
        "created_at": journal.created_at,
        "updated_at": journal.updated_at,
        "final_result": journal.final_result.value if journal.final_result else None,
    }


def _write_journal(root: Path, journal: RestoreJournal) -> None:
    _validate_journal(journal); operation = _operation_directory(root, journal.operation_id); destination = _journal_path(root, journal.operation_id)
    if has_symlink_component(root) or has_symlink_component(operation) or (destination.exists() and destination.is_symlink()):
        raise RestoreJournalPersistenceError("Restore journal path is unsafe")
    temporary = operation / f".{journal.operation_id}.journal.tmp"; created = False
    try:
        if temporary.exists() or temporary.is_symlink(): raise RestoreJournalPersistenceError("Restore journal temporary path is unsafe")
        data = canonical_json(_journal_payload(journal))
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        _private(temporary)
        if destination.exists() and destination.is_symlink(): raise RestoreJournalPersistenceError("Restore journal path is unsafe")
        os.replace(temporary, destination); created = False; _private(destination); _fsync(operation, True)
    except RestoreJournalPersistenceError: raise
    except OSError as exc: raise RestoreJournalPersistenceError("Restore journal could not be persisted") from exc
    finally:
        if created:
            try: temporary.unlink()
            except OSError: pass


def create_restore_journal(plan: RestorePlan, *, root: Path | str | None = None, operation_id: str | None = None, now: str | None = None) -> RestoreJournal:
    if not isinstance(plan, RestorePlan): raise RestorePlanError("Invalid restore plan")
    canonical_plan = create_restore_plan(selected_backup_id=plan.selected_backup_id, selected_backup_manifest_sha256=plan.selected_backup_manifest_sha256, expected_application_commit=plan.expected_application_commit, runtime_mode=plan.runtime_mode, target_keys=plan.target_keys, created_at=plan.created_at)
    if plan != canonical_plan:
        raise RestorePlanError("Invalid restore plan")
    selected = validate_restore_root(root); identifier = operation_id or _operation_id(); _safe(identifier, pattern=_OPERATION_ID, error=RestoreJournalError)
    timestamp = now or _now(); _timestamp(timestamp, RestoreJournalError)
    operation = _operation_directory(selected, identifier)
    if selected.exists() and selected.is_symlink(): raise RestoreJournalPersistenceError("Restore root is unsafe")
    try:
        selected.mkdir(parents=True, exist_ok=True); _private(selected, True); operation.mkdir(mode=0o700, exist_ok=False); _private(operation, True)
    except OSError as exc: raise RestoreJournalPersistenceError("Restore journal directory could not be created") from exc
    journal = RestoreJournal(JOURNAL_FORMAT, identifier, plan.selected_backup_id, plan.selected_backup_manifest_sha256, None, plan.expected_application_commit, plan.runtime_mode, plan.target_keys, plan.target_set_hash, plan.confirmation_value, RestoreStage.PRECHECK, tuple(TargetJournalFact(key, TargetRestoreState.PENDING) for key in plan.target_keys), timestamp, timestamp, None)
    try: _write_journal(selected, journal)
    except Exception:
        # The operation directory may be empty or contain only this write's temporary file.
        try: operation.rmdir()
        except OSError: pass
        raise
    return journal


def _from_payload(payload: object) -> RestoreJournal:
    if not isinstance(payload, dict) or set(payload) != {"format_version", "operation_id", "selected_backup_id", "selected_backup_manifest_sha256", "safety_backup_id", "expected_application_commit", "runtime_mode", "target_keys", "target_set_hash", "confirmation_value", "stage", "targets", "created_at", "updated_at", "final_result"}:
        raise RestoreJournalError("Invalid restore journal")
    try:
        targets_list = []
        for item in payload["targets"]:
            if not isinstance(item, dict):
                raise RestoreJournalError("Invalid restore journal")
            key = _target_key(item["target_key"], RestoreJournalError)
            state = TargetRestoreState(item["state"])
            wal_pres = item.get("wal_present", False)
            shm_pres = item.get("shm_present", False)
            wal_rem = item.get("wal_removed", False)
            shm_rem = item.get("shm_removed", False)
            repl_int = item.get("replacement_intent", state in {TargetRestoreState.REPLACED, TargetRestoreState.ROLLED_BACK})
            repl_comp = item.get("replacement_completed", state is TargetRestoreState.REPLACED)
            roll_int = item.get("rollback_intent", state is TargetRestoreState.ROLLED_BACK)
            roll_comp = item.get("rollback_completed", state is TargetRestoreState.ROLLED_BACK)
            for b in (wal_pres, shm_pres, wal_rem, shm_rem, repl_int, repl_comp, roll_int, roll_comp):
                if type(b) is not bool:
                    raise RestoreJournalError("Invalid restore journal")
            targets_list.append(TargetJournalFact(
                target_key=key,
                state=state,
                wal_present=wal_pres,
                shm_present=shm_pres,
                wal_removed=wal_rem,
                shm_removed=shm_rem,
                replacement_intent=repl_int,
                replacement_completed=repl_comp,
                rollback_intent=roll_int,
                rollback_completed=roll_comp,
            ))
        final = None if payload["final_result"] is None else FinalResult(payload["final_result"])
        journal = RestoreJournal(payload["format_version"], payload["operation_id"], payload["selected_backup_id"], payload["selected_backup_manifest_sha256"], payload["safety_backup_id"], payload["expected_application_commit"], payload["runtime_mode"], tuple(payload["target_keys"]), payload["target_set_hash"], payload["confirmation_value"], RestoreStage(payload["stage"]), tuple(targets_list), payload["created_at"], payload["updated_at"], final)
    except (KeyError, TypeError, ValueError) as exc: raise RestoreJournalError("Invalid restore journal") from exc
    _validate_journal(journal); return journal


def _decode_journal(raw: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RestoreJournalError("Restore journal is invalid") from exc
    if canonical_json(payload) != raw:
        raise RestoreJournalError("Restore journal is invalid")
    return payload


def load_restore_journal(operation_id: str, *, root: Path | str | None = None) -> RestoreJournal:
    _safe(operation_id, pattern=_OPERATION_ID, error=RestoreJournalError)
    selected = validate_restore_root(root); destination = _journal_path(selected, operation_id)
    if has_symlink_component(selected) or has_symlink_component(destination) or destination.is_symlink(): raise RestoreJournalError("Restore journal path is unsafe")
    try:
        if not destination.is_file() or destination.stat().st_size > MAX_JOURNAL_BYTES: raise RestoreJournalError("Restore journal is invalid")
        raw = destination.read_bytes(); payload = _decode_journal(raw)
    except RestoreJournalError: raise
    except OSError as exc: raise RestoreJournalError("Restore journal is invalid") from exc
    journal = _from_payload(payload)
    if journal.operation_id != operation_id:
        raise RestoreJournalError("Restore journal identity is invalid")
    return journal


def update_restore_journal(
    operation_id: str,
    *,
    root: Path | str | None = None,
    stage: RestoreStage | None = None,
    target_key: str | None = None,
    target_state: TargetRestoreState | None = None,
    wal_present: bool | None = None,
    shm_present: bool | None = None,
    wal_removed: bool | None = None,
    shm_removed: bool | None = None,
    replacement_intent: bool | None = None,
    replacement_completed: bool | None = None,
    rollback_intent: bool | None = None,
    rollback_completed: bool | None = None,
    safety_backup_id: str | None = None,
    now: str | None = None,
) -> RestoreJournal:
    journal = load_restore_journal(operation_id, root=root)
    if (stage is None) and (target_key is None):
        raise RestoreTransitionError("Illegal restore journal transition")
    if target_key is not None and target_state is None and all(v is None for v in (wal_present, shm_present, wal_removed, shm_removed, replacement_intent, replacement_completed, rollback_intent, rollback_completed)):
        raise RestoreTransitionError("Illegal restore journal transition")
    if stage is not None and not isinstance(stage, RestoreStage):
        raise RestoreTransitionError("Illegal restore journal transition")
    if journal.stage in _TERMINAL_RESULTS:
        raise RestoreTransitionError("Illegal restore journal transition")
    facts = list(journal.targets)
    new_stage = journal.stage
    safety = journal.safety_backup_id
    if stage is not None:
        if target_key is not None or any(v is not None for v in (wal_present, shm_present, wal_removed, shm_removed, replacement_intent, replacement_completed, rollback_intent, rollback_completed)):
            raise RestoreTransitionError("Illegal restore journal transition")
        if stage not in _GLOBAL_TRANSITIONS[journal.stage]:
            raise RestoreTransitionError("Illegal restore journal transition")
        if safety_backup_id is not None:
            if stage is not RestoreStage.CURRENT_SNAPSHOT_CREATED or safety is not None:
                raise RestoreTransitionError("Illegal restore journal transition")
            safety = _safe(safety_backup_id, pattern=_BACKUP_ID, error=RestoreTransitionError)
        elif stage is RestoreStage.CURRENT_SNAPSHOT_CREATED:
            raise RestoreTransitionError("Illegal restore journal transition")
        new_stage = stage
    else:
        key = _target_key(target_key, RestoreTransitionError)
        index = next((i for i, fact in enumerate(facts) if fact.target_key == key), None)
        if index is None:
            raise RestoreTransitionError("Illegal restore journal transition")
        curr = facts[index]
        new_state = curr.state
        if target_state is not None:
            if not isinstance(target_state, TargetRestoreState) or (target_state != curr.state and target_state not in _TARGET_TRANSITIONS[curr.state]):
                raise RestoreTransitionError("Illegal restore journal transition")
            new_state = target_state
        new_repl_intent = curr.replacement_intent if replacement_intent is None else replacement_intent
        new_repl_comp = curr.replacement_completed if replacement_completed is None else replacement_completed
        new_roll_intent = curr.rollback_intent if rollback_intent is None else rollback_intent
        new_roll_comp = curr.rollback_completed if rollback_completed is None else rollback_completed
        if new_state is TargetRestoreState.REPLACED:
            if replacement_intent is None: new_repl_intent = True
            if replacement_completed is None: new_repl_comp = True
        elif new_state is TargetRestoreState.ROLLED_BACK:
            if rollback_intent is None: new_roll_intent = True
            if rollback_completed is None: new_roll_comp = True
        facts[index] = TargetJournalFact(
            target_key=key,
            state=new_state,
            wal_present=curr.wal_present if wal_present is None else wal_present,
            shm_present=curr.shm_present if shm_present is None else shm_present,
            wal_removed=curr.wal_removed if wal_removed is None else wal_removed,
            shm_removed=curr.shm_removed if shm_removed is None else shm_removed,
            replacement_intent=new_repl_intent,
            replacement_completed=new_repl_comp,
            rollback_intent=new_roll_intent,
            rollback_completed=new_roll_comp,
        )
    result = _TERMINAL_RESULTS.get(new_stage)
    timestamp = now or _now()
    if _timestamp(timestamp, RestoreJournalError) < _timestamp(journal.updated_at, RestoreJournalError):
        raise RestoreTransitionError("Illegal restore journal transition")
    updated = RestoreJournal(
        journal.format_version,
        journal.operation_id,
        journal.selected_backup_id,
        journal.selected_backup_manifest_sha256,
        safety,
        journal.expected_application_commit,
        journal.runtime_mode,
        journal.target_keys,
        journal.target_set_hash,
        journal.confirmation_value,
        new_stage,
        tuple(facts),
        journal.created_at,
        timestamp,
        result,
    )
    _validate_journal(updated)
    _write_journal(validate_restore_root(root), updated)
    return updated


class RestoreLock:
    """Explicit nonblocking dedicated restore lock; unrelated to app/backup locks."""
    def __init__(self, root: Path | str | None = None):
        try: self.root = validate_restore_root(root)
        except RestoreJournalError as exc: raise RestoreLockError("Restore lock path is unsafe") from exc
        self.path = self.root / ".garmincoach-restore.lock"; self.handle = None
    def __enter__(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True); _private(self.root, True)
            if self.path.is_symlink(): raise RestoreLockError("Restore lock path is unsafe")
            self.handle = self.path.open("a+b"); _private(self.path)
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1): self.handle.write(b"\0"); self.handle.flush()
                self.handle.seek(0); msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except RestoreLockError:
            if self.handle: self.handle.close(); self.handle = None
            raise
        except (OSError, RestoreJournalPersistenceError) as exc:
            if self.handle: self.handle.close(); self.handle = None
            raise RestoreLockError("Another guarded restore is active") from exc
        return self
    def __exit__(self, *_):
        if self.handle is None: return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0); msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally: self.handle.close(); self.handle = None
