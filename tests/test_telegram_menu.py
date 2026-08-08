from coach.telegram_menu import (
    ASK_COACH_BACK_LABEL,
    MAIN_MENU_ACTIONS,
    ask_coach_back_markup,
    ask_coach_retry_markup,
    main_menu_markup,
    settings_markup,
)


def test_main_menu_is_a_persistent_reply_keyboard_with_all_labels():
    markup = main_menu_markup()
    assert "inline_keyboard" not in markup
    assert markup["resize_keyboard"] is True
    assert markup["is_persistent"] is True
    assert markup["one_time_keyboard"] is False
    assert {label for row in markup["keyboard"] for label in row} == set(
        MAIN_MENU_ACTIONS
    )


def test_main_menu_has_exactly_12_buttons_in_6_rows_of_2():
    markup = main_menu_markup()
    buttons = [label for row in markup["keyboard"] for label in row]
    assert len(buttons) == 12
    assert len(markup["keyboard"]) == 6
    assert all(len(row) == 2 for row in markup["keyboard"])


def test_find_a_time_absent_from_main_menu():
    buttons = {label for row in main_menu_markup()["keyboard"] for label in row}
    assert "Find a time" not in buttons
    assert "Sync status" not in buttons
    assert "Start sync" not in buttons
    assert "Privacy & Ask Coach" not in buttons
    assert "Unlink Telegram" not in buttons


def test_garmin_sync_and_settings_present_in_main_menu():
    buttons = {label for row in main_menu_markup()["keyboard"] for label in row}
    assert "Garmin sync" in buttons
    assert "Settings" in buttons


def test_ask_coach_has_a_small_reply_keyboard_and_inline_retry():
    assert ask_coach_back_markup()["keyboard"] == [[ASK_COACH_BACK_LABEL]]
    assert ask_coach_retry_markup("nonce") == {
        "inline_keyboard": [[{"text": "Try again", "callback_data": "ask:retry:nonce"}]]
    }


def test_settings_markup_exposes_privacy_unlink_and_back():
    markup = settings_markup()
    assert "inline_keyboard" in markup
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "menu:privacy" in callbacks
    assert "menu:unlink" in callbacks
    assert "menu:home" in callbacks
