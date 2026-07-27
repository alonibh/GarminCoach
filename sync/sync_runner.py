"""Single owner of sync run-state, shared by the web routes and the scheduler.

Keeping the lock + status here (not in app.py) avoids a circular import:
both app.py and scheduler.py import from this neutral module.
"""
from __future__ import annotations

import logging
import threading
import time
from contextvars import copy_context

import config
from sync.sync_service import run_priority_sync, run_sync
from db import get_session, SyncState

log = logging.getLogger(__name__)

_legacy_status = {"running": False, "summary": None, "started_at": None}
_user_status: dict[str, dict] = {}
_status_guard = threading.RLock()


def _status_dict() -> dict:
    if not config.MULTI_USER_ENABLED:
        return _legacy_status
    from tenant_context import current_tenant

    tenant = current_tenant()
    if tenant is None:
        return _legacy_status
    user_id = tenant.user_id
    with _status_guard:
        return _user_status.setdefault(
            user_id, {"running": False, "summary": None, "started_at": None}
        )


class _TenantStatusProxy:
    def __getitem__(self, key):
        return _status_dict()[key]

    def __setitem__(self, key, value) -> None:
        _status_dict()[key] = value


status = _TenantStatusProxy()

# Max sync duration before it's considered stuck (10 minutes).
_MAX_SYNC_SECONDS = 600


def _get_lock_ts() -> float:
    with get_session() as session:
        row = session.get(SyncState, "sync_lock_ts")
        if row and row.value:
            try:
                return float(row.value)
            except ValueError:
                pass
    return 0.0

def _set_lock_ts(ts: float) -> None:
    with get_session() as session:
        row = session.get(SyncState, "sync_lock_ts")
        if row:
            row.value = str(ts)
        else:
            session.add(SyncState(key="sync_lock_ts", value=str(ts)))

def is_running() -> bool:
    """Check if a sync is running via the DB lease."""
    lock_ts = _get_lock_ts()
    if lock_ts <= 0:
        if status["running"]:
            status["running"] = False
            status["started_at"] = None
        return False

    if (time.time() - lock_ts) > _MAX_SYNC_SECONDS:
        log.warning("Sync exceeded %ds timeout — auto-clearing stuck state.", _MAX_SYNC_SECONDS)
        _set_lock_ts(0.0)
        status["running"] = False
        status["started_at"] = None
        status["summary"] = {"errors": ["Sync timed out after 10 minutes. Try again."]}
        return False

    status["running"] = True
    status["started_at"] = lock_ts
    return True


def try_start_sync(full: bool, force: bool = False, allow_backfill: bool = False) -> bool:
    """Start a background sync iff none is running. Returns True if started."""
    if is_running():
        return False
        
    # Set lock in DB
    now = time.time()
    _set_lock_ts(now)
    status["running"] = True
    status["started_at"] = now
    
    context = copy_context()
    threading.Thread(target=context.run, args=(_run, full, force, allow_backfill), daemon=True).start()
    return True


def try_start_priority_sync() -> bool:
    """Start the overnight-only fetch under the same lease as every other sync."""
    if is_running():
        return False
    now = time.time()
    _set_lock_ts(now)
    status["running"] = True
    status["started_at"] = now
    context = copy_context()
    threading.Thread(target=context.run, args=(_run_priority,), daemon=True).start()
    return True


def reset() -> None:
    """Escape hatch: force-clear a stuck 'running' state."""
    _set_lock_ts(0.0)
    status["running"] = False
    status["started_at"] = None


def _checkpoint_current_tenant() -> None:
    if not config.MULTI_USER_ENABLED:
        return
    try:
        from sync.garmin_registry import get_garmin_registry
        from tenant_context import current_tenant

        tenant = current_tenant()
        if tenant:
            get_garmin_registry().checkpoint(tenant.user_id)
    except Exception:
        log.exception("Failed to checkpoint Garmin tokens")


def _run(full: bool, force: bool = False, allow_backfill: bool = False) -> None:
    try:
        status["summary"] = run_sync(full=full, force=force, allow_backfill=allow_backfill)
    except Exception as e:
        log.exception("Sync failed with unhandled exception")
        status["summary"] = {"errors": [str(e)]}
    finally:
        _checkpoint_current_tenant()
        _set_lock_ts(0.0)
        status["running"] = False
        status["started_at"] = None


def _run_priority() -> None:
    try:
        status["summary"] = run_priority_sync()
    except Exception as e:
        log.exception("Priority sync failed with unhandled exception")
        status["summary"] = {"priority": True, "errors": [str(e)]}
    finally:
        _checkpoint_current_tenant()
        _set_lock_ts(0.0)
        status["running"] = False
        status["started_at"] = None
    try:
        from notify.morning import priority_sync_finished
        priority_sync_finished()
    except Exception:
        log.exception("Morning flow failed after priority sync")
    # The briefing-critical facts are already committed; finish the slower
    # activity/history/dashboard work under a new acquisition of the same lock.
    try_start_sync(full=False)

