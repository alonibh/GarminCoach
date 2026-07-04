"""Single owner of sync run-state, shared by the web routes and the scheduler.

Keeping the lock + status here (not in app.py) avoids a circular import:
both app.py and scheduler.py import from this neutral module.
"""
from __future__ import annotations

import logging
import threading
import time

from sync.sync_service import run_sync
from db import get_session, SyncState

log = logging.getLogger(__name__)

# Shared status surfaced in the dashboard.
status = {"running": False, "summary": None, "started_at": None}

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


def try_start_sync(full: bool, force: bool = False) -> bool:
    """Start a background sync iff none is running. Returns True if started."""
    if is_running():
        return False
        
    # Set lock in DB
    now = time.time()
    _set_lock_ts(now)
    status["running"] = True
    status["started_at"] = now
    
    threading.Thread(target=_run, args=(full, force), daemon=True).start()
    return True


def reset() -> None:
    """Escape hatch: force-clear a stuck 'running' state."""
    _set_lock_ts(0.0)
    status["running"] = False
    status["started_at"] = None


def _run(full: bool, force: bool = False) -> None:
    try:
        status["summary"] = run_sync(full=full, force=force)
    except Exception as e:
        log.exception("Sync failed with unhandled exception")
        status["summary"] = {"errors": [str(e)]}
    finally:
        _set_lock_ts(0.0)
        status["running"] = False
        status["started_at"] = None

