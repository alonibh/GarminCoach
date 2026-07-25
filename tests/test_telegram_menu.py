from coach.telegram_menu import (
    ask_coach_back_markup,
    ask_coach_retry_markup,
    main_menu_markup,
)


def test_main_menu_contains_complete_callback_catalog():
    callbacks = {
        button["callback_data"]
        for row in main_menu_markup()["inline_keyboard"]
        for button in row
    }
    assert callbacks == {
        "menu:recommendation",
        "menu:next_workout",
        "menu:find_time",
        "menu:schedule",
        "menu:reschedule",
        "menu:cancel",
        "menu:metrics",
        "menu:activities",
        "menu:program",
        "menu:sync_status",
        "menu:calendar",
        "menu:start_sync",
        "menu:ask_coach",
        "menu:privacy",
        "menu:unlink",
    }


def test_ask_coach_controls_are_namespace_limited():
    assert ask_coach_back_markup() == {
        "inline_keyboard": [[
            {"text": "Back to menu", "callback_data": "ask:exit"}
        ]]
    }
    callbacks = [
        row[0]["callback_data"]
        for row in ask_coach_retry_markup("nonce")["inline_keyboard"]
    ]
    assert callbacks == ["ask:retry:nonce", "ask:exit"]
