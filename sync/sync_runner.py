"""Single owner of sync run-state, shared by the web routes and the scheduler.

Keeping the lock + status here (not in app.py) avoids a circular import:
both app.py and scheduler.py import from this neutral module.
"""
from __future__ import annotations

import logging
import threading
import time

from sync.sync_service import run_sync

log = logging.getLogger(__name__)

# Shared status surfaced in the dashboard; lock makes start atomic.
status = {"running": False, "summary": None, "started_at": None}
_lock = threading.Lock()

# Max sync duration before it's considered stuck (10 minutes).
_MAX_SYNC_SECONDS = 600


def is_running() -> bool:
    """Check if a sync is running, auto-clearing if it has exceeded the
    timeout (protects against a hung Garmin API call)."""
    if not status["running"]:
        return False
    started = status.get("started_at")
    if started and (time.monotonic() - started) > _MAX_SYNC_SECONDS:
        log.warning("Sync exceeded %ds timeout — auto-clearing stuck state.", _MAX_SYNC_SECONDS)
        status["running"] = False
        status["started_at"] = None
        status["summary"] = {"errors": ["Sync timed out after 10 minutes. Try again."]}
        return False
    return True


def try_start_sync(full: bool) -> bool:
    """Start a background sync iff none is running. Returns True if started."""
    with _lock:
        if is_running():
            return False
        status["running"] = True
        status["started_at"] = time.monotonic()
    threading.Thread(target=_run, args=(full,), daemon=True).start()
    return True


def reset() -> None:
    """Escape hatch: force-clear a stuck 'running' state."""
    status["running"] = False
    status["started_at"] = None


def _run(full: bool) -> None:
    try:
        status["summary"] = run_sync(full=full)
    except Exception as e:
        log.exception("Sync failed with unhandled exception")
        status["summary"] = {"errors": [str(e)]}
    finally:
        status["running"] = False
        status["started_at"] = None

