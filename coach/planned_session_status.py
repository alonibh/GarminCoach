"""Neutral status rules shared by recovery, reminders, and reconciliation."""
from __future__ import annotations

# These are string values in the existing schema; introducing them needs no migration.
INACTIVE_ORIGINAL_SESSION_STATUSES = frozenset({
    "completed", "cancelled", "replaced_by_active_recovery", "rest_selected",
})


def is_current_original_status(status: str | None) -> bool:
    return (status or "").lower() not in INACTIVE_ORIGINAL_SESSION_STATUSES

