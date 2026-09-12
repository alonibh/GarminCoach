"""Telegram controls for GarminCoach's persistent and dynamic menus."""
from __future__ import annotations


# These labels are both the visible reply-keyboard buttons and the only accepted
# top-level text commands. Keep their mapping exact and in one place.
MAIN_MENU_ACTIONS = {
    "Today's recommendation": "menu:recommendation",
    "Next workout": "menu:next_workout",
    "Schedule": "menu:schedule",
    "Reschedule": "menu:reschedule",
    "Cancel workout": "menu:cancel",
    "Calendar": "menu:calendar",
    "Recovery metrics": "menu:metrics",
    "Recent activities": "menu:activities",
    "Training program": "menu:program",
    "Garmin sync": "menu:garmin_sync",
    "Ask Coach": "menu:ask_coach",
    "Settings": "menu:settings",
}

ASK_COACH_BACK_LABEL = "Back to menu"


def main_menu_markup() -> dict:
    return {
        "keyboard": [
            ["Today's recommendation", "Next workout"],
            ["Schedule", "Reschedule"],
            ["Cancel workout", "Calendar"],
            ["Recovery metrics", "Recent activities"],
            ["Training program", "Garmin sync"],
            ["Ask Coach", "Settings"],
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


DAILY_INSIGHT_DISCLOSURE = (
    "*Daily Coach Insight (AI)*\n\n"
    "Receive a proactive AI coaching insight every evening at 21:00 "
    "local time (Sunday–Friday).\n\n"
    "• *Schedule:* Sent Sun–Fri at 21:00 local time. Automatically skipped on Saturday (weekly summary).\n"
    "• *Data Shared:* Proactively sends a privacy-bounded daily advisory snapshot (recent training, recovery metrics such as HRV, sleep, resting HR, and upcoming schedule) to Google Gemini.\n"
    "• *Provider:* Google Gemini. GarminCoach sends each request with store=False, so the Interactions API does not store the interaction for server-side conversation state. Google's processing and data-use practices are governed by the Gemini API service tier and applicable terms.\n"
    "• *Advisory Only:* Insights are fitness guidance only and do not provide medical advice."
)


def daily_insight_markup(is_opted_in: bool) -> dict:
    toggle_text = "Disable Daily Insight" if is_opted_in else "Enable Daily Insight"
    toggle_data = "insight:disable" if is_opted_in else "insight:enable"
    return {
        "inline_keyboard": [
            [{"text": toggle_text, "callback_data": toggle_data}],
            [{"text": "Back to Settings", "callback_data": "menu:settings"}],
            [{"text": "Back to menu", "callback_data": "menu:home"}],
        ]
    }


def settings_markup() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Daily Coach Insight", "callback_data": "menu:daily_insight"}],
            [{"text": "Ask Coach privacy", "callback_data": "menu:privacy"}],
            [{"text": "Unlink Telegram", "callback_data": "menu:unlink"}],
            [{"text": "Back to menu", "callback_data": "menu:home"}],
        ]
    }
