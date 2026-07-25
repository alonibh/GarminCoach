"""Inline Telegram controls for GarminCoach's button-only interface."""
from __future__ import annotations


def main_menu_markup() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Today's recommendation", "callback_data": "menu:recommendation"},
                {"text": "Next workout", "callback_data": "menu:next_workout"},
            ],
            [
                {"text": "Find a time", "callback_data": "menu:find_time"},
                {"text": "Schedule", "callback_data": "menu:schedule"},
            ],
            [
                {"text": "Reschedule", "callback_data": "menu:reschedule"},
                {"text": "Cancel workout", "callback_data": "menu:cancel"},
            ],
            [
                {"text": "Recovery metrics", "callback_data": "menu:metrics"},
                {"text": "Recent activities", "callback_data": "menu:activities"},
            ],
            [
                {"text": "Training program", "callback_data": "menu:program"},
                {"text": "Sync status", "callback_data": "menu:sync_status"},
            ],
            [
                {"text": "Calendar", "callback_data": "menu:calendar"},
                {"text": "Start sync", "callback_data": "menu:start_sync"},
            ],
            [{"text": "Ask Coach", "callback_data": "menu:ask_coach"}],
            [
                {"text": "Privacy & Ask Coach", "callback_data": "menu:privacy"},
                {"text": "Unlink Telegram", "callback_data": "menu:unlink"},
            ],
        ]
    }


def ask_coach_back_markup() -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Back to menu", "callback_data": "ask:exit"}
        ]]
    }


def ask_coach_retry_markup(nonce: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Try again", "callback_data": f"ask:retry:{nonce}"}],
            [{"text": "Back to menu", "callback_data": "ask:exit"}],
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
