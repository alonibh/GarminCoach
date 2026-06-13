"""Single owner of sync run-state, shared by the web routes and the scheduler.

Keeping the lock + status here (not in app.py) avoids a circular import:
both app.py and scheduler.py import from this neutral module.
"""
from __future__ import annotations

import threading

from sync.sync_service import run_sync

# Shared status surfaced in the dashboard; lock makes start atomic.
status = {"running": False, "summary": None}
_lock = threading.Lock()


def is_running() -> bool:
    return status["running"]


def try_start_sync(full: bool) -> bool:
    """Start a background sync iff none is running. Returns True if started."""
    with _lock:
        if status["running"]:
            return False
        status["running"] = True
    threading.Thread(target=_run, args=(full,), daemon=True).start()
    return True


def reset() -> None:
    """Escape hatch: force-clear a stuck 'running' state."""
    status["running"] = False


def _run(full: bool) -> None:
    try:
        status["summary"] = run_sync(full=full)
    except Exception as e:
        status["summary"] = {"errors": [str(e)]}
    finally:
        status["running"] = False
