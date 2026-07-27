from coach.telegram_menu import (
    ASK_COACH_BACK_LABEL,
    MAIN_MENU_ACTIONS,
    ask_coach_back_markup,
    ask_coach_retry_markup,
    main_menu_markup,
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


def test_ask_coach_has_a_small_reply_keyboard_and_inline_retry():
    assert ask_coach_back_markup()["keyboard"] == [[ASK_COACH_BACK_LABEL]]
    assert ask_coach_retry_markup("nonce") == {
        "inline_keyboard": [[{"text": "Try again", "callback_data": "ask:retry:nonce"}]]
    }
