"""Telegram controls for GarminCoach's persistent and dynamic menus."""
from __future__ import annotations


# These labels are both the visible reply-keyboard buttons and the only accepted
# top-level text commands. Keep their mapping exact and in one place.
MAIN_MENU_ACTIONS = {
    "Today's recommendation": "menu:recommendation",
    "Next workout": "menu:next_workout",
    "Find a time": "menu:find_time",
    "Schedule": "menu:schedule",
    "Reschedule": "menu:reschedule",
    "Cancel workout": "menu:cancel",
    "Recovery metrics": "menu:metrics",
    "Recent activities": "menu:activities",
    "Training program": "menu:program",
    "Sync status": "menu:sync_status",
    "Calendar": "menu:calendar",
    "Start sync": "menu:start_sync",
    "Ask Coach": "menu:ask_coach",
    "Privacy & Ask Coach": "menu:privacy",
    "Unlink Telegram": "menu:unlink",
}

ASK_COACH_BACK_LABEL = "Back to menu"


def main_menu_markup() -> dict:
    return {
        "keyboard": [
            ["Today's recommendation", "Next workout"],
            ["Find a time", "Schedule"],
            ["Reschedule", "Cancel workout"],
            ["Recovery metrics", "Recent activities"],
            ["Training program", "Sync status"],
            ["Calendar", "Start sync"],
            ["Ask Coach"],
            ["Privacy & Ask Coach", "Unlink Telegram"],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
    }


def ask_coach_back_markup() -> dict:
    return {
        "keyboard": [[ASK_COACH_BACK_LABEL]],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
    }


def ask_coach_retry_markup(nonce: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Try again", "callback_data": f"ask:retry:{nonce}"}],
        ]
    }


def consent_disclosure_markup() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Continue", "callback_data": "ask:consent_agree"}],
            [{"text": "View details", "callback_data": "ask:consent_details"}],
            [{"text": "Cancel", "callback_data": "ask:consent_cancel"}],
        ]
    }


def privacy_markup(has_consent: bool) -> dict:
    rows = []
    if has_consent:
        rows.append(
            [{"text": "Revoke consent", "callback_data": "ask:consent_revoke"}]
        )
    rows.append([{"text": "Back to menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}
