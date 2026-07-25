"""Fast Telegram webhook dispatch and Ask Coach background execution."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextvars import Context
from datetime import datetime, timedelta, timezone
from typing import Awaitable

import config
from coach.advisory_snapshot import (
    build_advisory_snapshot,
    serialize_advisory_snapshot,
)
from coach.ask_coach_llm import (
    AskCoachLLMError,
    generate_ask_coach_response,
)
from coach.ask_coach_session import (
    AcquireStatus,
    session_manager,
)
from coach.privacy_logger import log_sanitized_error
from coach.telegram_menu import (
    ask_coach_back_markup,
    ask_coach_retry_markup,
    consent_disclosure_markup,
    main_menu_markup,
    privacy_markup,
)
from control_db import (
    get_ask_coach_consent,
    is_consent_valid,
    record_ask_coach_consent,
    revoke_ask_coach_consent,
)
from db import PendingInteraction
from tenant_context import TenantIdentity, bind_tenant, reset_tenant
from tenant_store import get_user_session

OPERATIONAL_TEXT_GUIDANCE = (
    "Use the buttons below, or tap Ask Coach to ask a fitness or health question."
)
ASK_COACH_TEXT_ONLY = "Ask Coach currently supports text questions only."
ASK_COACH_ACTIVE = (
    "Ask Coach is active. Ask a fitness or health question in a text message."
)
DISCLOSURE = (
    "Ask Coach sends the coaching-relevant GarminCoach data listed in the "
    "privacy details, plus this session's conversation, to Google Gemini. "
    "Google interaction storage is disabled."
)

_active_tasks: set[asyncio.Task] = set()
_shutting_down = False


class UpdateDeduplicator:
    def __init__(self, maximum: int = 5000, ttl_hours: int = 24) -> None:
        self.maximum = maximum
        self.ttl = timedelta(hours=ttl_hours)
        self._accepted: OrderedDict[int, datetime] = OrderedDict()

    def accept(self, update_id: int | None) -> bool:
        if update_id is None:
            return True
        now = datetime.now(timezone.utc)
        cutoff = now - self.ttl
        while self._accepted:
            _, accepted_at = next(iter(self._accepted.items()))
            if accepted_at >= cutoff:
                break
            self._accepted.popitem(last=False)
        if update_id in self._accepted:
            return False
        self._accepted[update_id] = now
        self._accepted.move_to_end(update_id)
        while len(self._accepted) > self.maximum:
            self._accepted.popitem(last=False)
        return True

    def clear(self) -> None:
        self._accepted.clear()


update_deduplicator = UpdateDeduplicator()


def _consume_task_result(task: asyncio.Task) -> None:
    _active_tasks.discard(task)
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log_sanitized_error(type(exc).__name__)


def _register_task(coro: Awaitable) -> asyncio.Task | None:
    if _shutting_down:
        if hasattr(coro, "close"):
            coro.close()
        return None
    # Do not inherit the request's tenant binding. The task binds its immutable
    # identity explicitly and resets that binding in its own finally block.
    task = asyncio.create_task(coro, context=Context())
    _active_tasks.add(task)
    task.add_done_callback(_consume_task_result)
    return task


def set_shutting_down_flag() -> None:
    global _shutting_down
    _shutting_down = True


def clear_shutting_down_flag() -> None:
    global _shutting_down
    _shutting_down = False


async def cancel_active_gemini_tasks() -> None:
    tasks = list(_active_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _active_tasks.clear()


def _valid_consent(user_id: str) -> bool:
    return is_consent_valid(get_ask_coach_consent(user_id))


def _split_plain_text(text: str, maximum: int = 3800) -> list[str]:
    if len(text) <= maximum:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= maximum:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, maximum + 1)
        if boundary < maximum // 2:
            boundary = remaining.rfind(" ", 0, maximum + 1)
        if boundary < maximum // 2:
            boundary = maximum
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return chunks


async def _can_deliver(
    user_id: str, chat_id: str, generation_token: str
) -> bool:
    return (
        await session_manager.validate_session_for_delivery(
            user_id, chat_id, generation_token
        )
        and await asyncio.to_thread(_valid_consent, user_id)
    )


async def _send_plain(
    text: str,
    *,
    chat_id: str,
    reply_markup: dict | None = None,
) -> bool:
    from notify import telegram

    return await asyncio.to_thread(
        telegram.send_message,
        text,
        chat_id,
        reply_markup,
        parse_mode=None,
    )


async def _send_error(
    *,
    user_id: str,
    chat_id: str,
    generation_token: str,
    question: str,
    error: AskCoachLLMError,
) -> None:
    if not await _can_deliver(user_id, chat_id, generation_token):
        return
    if error.category == "configuration":
        await _send_plain(
            "Ask Coach is unavailable because its service configuration "
            "needs attention.",
            chat_id=chat_id,
            reply_markup=ask_coach_back_markup(),
        )
        return
    nonce = await session_manager.set_pending_retry(
        user_id, generation_token, question
    )
    if nonce is None:
        return
    if error.category == "rate_limited":
        text = "Ask Coach is temporarily rate-limited. Please try again."
    elif error.category == "timeout":
        text = "Ask Coach timed out. Please try again."
    else:
        text = "Ask Coach couldn't complete that response. Please try again."
    await _send_plain(
        text,
        chat_id=chat_id,
        reply_markup=ask_coach_retry_markup(nonce),
    )


async def run_ask_coach_question(
    *,
    identity: TenantIdentity,
    chat_id: str,
    generation_token: str,
    question: str,
) -> None:
    user_id = identity.user_id
    tenant_token = bind_tenant(identity)
    try:
        from notify import telegram

        await asyncio.to_thread(telegram.send_chat_action, chat_id, "typing")
        history = await session_manager.history_for_generation(
            user_id, generation_token
        )
        if history is None:
            return
        with get_user_session(user_id) as database:
            snapshot = build_advisory_snapshot(database)
            snapshot_json = serialize_advisory_snapshot(snapshot)
        try:
            result = await generate_ask_coach_response(
                user_id=user_id,
                snapshot_json=snapshot_json,
                history=history,
                question=question,
            )
        except AskCoachLLMError as exc:
            await _send_error(
                user_id=user_id,
                chat_id=chat_id,
                generation_token=generation_token,
                question=question,
                error=exc,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_sanitized_error(type(exc).__name__, user_id=user_id)
            await _send_error(
                user_id=user_id,
                chat_id=chat_id,
                generation_token=generation_token,
                question=question,
                error=AskCoachLLMError("service"),
            )
            return

        response_text = result.delivery_text()
        chunks = _split_plain_text(response_text)
        delivered_all = True
        for index, chunk in enumerate(chunks):
            if not await _can_deliver(user_id, chat_id, generation_token):
                delivered_all = False
                break
            markup = (
                ask_coach_back_markup()
                if index == len(chunks) - 1
                else None
            )
            if not await _send_plain(
                chunk, chat_id=chat_id, reply_markup=markup
            ):
                delivered_all = False
                break
        if delivered_all and result.response_type in {"answer", "clarification"}:
            await session_manager.record_successful_turn(
                user_id,
                generation_token,
                question,
                response_text,
            )
    finally:
        try:
            await session_manager.clear_in_flight_if_matches(
                user_id, generation_token
            )
        finally:
            reset_tenant(tenant_token)


def _privacy_text() -> str:
    categories = "\n".join(
        f"• {category}"
        for category in config.CURRENT_ASK_COACH_DATA_CATEGORIES
    )
    return (
        f"Provider: {config.ASK_COACH_PROVIDER}\n"
        f"Consent version: {config.ASK_COACH_CONSENT_VERSION}\n"
        f"Data categories version: "
        f"{config.ASK_COACH_DATA_CATEGORIES_VERSION}\n\n"
        f"Data categories:\n{categories}\n\n"
        "Gemini interaction storage is disabled."
    )


def _edit(
    text: str,
    chat_id: str,
    message_id: int,
    reply_markup: dict | None,
) -> bool:
    from notify import telegram

    return telegram.edit_message_text(
        text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=reply_markup,
        parse_mode=None,
    )


def _operational_callback(
    callback_data: str,
    *,
    identity: TenantIdentity,
    chat_id: str,
) -> tuple[str, dict | None]:
    from coach import renderers
    from coach.interactions import (
        advance_button_flow,
        apply_interaction,
        begin_alternate_time,
        begin_reschedule_flow,
        begin_schedule_flow,
        reject_interaction,
        stage_cancel_choices,
        stage_sync_confirmation,
    )

    with get_user_session(identity.user_id) as database:
        if callback_data.startswith("morning_synced_"):
            from notify.morning import start_priority_fetch

            started = start_priority_fetch()
            return (
                (
                    "Fetching the new Garmin data. The briefing will follow "
                    "automatically."
                    if started
                    else "A fetch is already running or today's briefing is complete."
                ),
                main_menu_markup(),
            )
        if callback_data.startswith("morning_anyway_"):
            from notify.morning import answer_anyway

            day_key = callback_data.rsplit("_", 1)[-1]
            accepted = answer_anyway(day_key)
            return (
                (
                    "Preparing the briefing without the missing data."
                    if accepted
                    else "This choice is no longer current."
                ),
                main_menu_markup(),
            )
        if callback_data.startswith(
            (
                "approve_workout_",
                "reject_workout_",
                "reschedule_workout_",
            )
        ):
            return (
                "This legacy workout action expired. Use the current menu.",
                main_menu_markup(),
            )
        if callback_data in {"menu:home", "menu:recommendation"}:
            text = (
                "GarminCoach menu"
                if callback_data == "menu:home"
                else renderers.render_recommendation(database)
            )
            return text, main_menu_markup()
        if callback_data == "menu:next_workout":
            return renderers.render_next_workout(database), main_menu_markup()
        if callback_data in {"menu:find_time", "menu:schedule"}:
            turn = begin_schedule_flow(database)
            return turn.text, turn.reply_markup or main_menu_markup()
        if callback_data == "menu:reschedule":
            turn = begin_reschedule_flow(database)
            return turn.text, turn.reply_markup or main_menu_markup()
        if callback_data == "menu:cancel":
            turn = stage_cancel_choices(database)
            return turn.text, turn.reply_markup or main_menu_markup()
        if callback_data == "menu:metrics":
            return renderers.render_metrics(database), main_menu_markup()
        if callback_data == "menu:activities":
            return renderers.render_activities(database), main_menu_markup()
        if callback_data == "menu:program":
            return renderers.render_program(database), main_menu_markup()
        if callback_data == "menu:sync_status":
            return renderers.render_sync_status(database), main_menu_markup()
        if callback_data == "menu:calendar":
            return renderers.render_calendar(database), main_menu_markup()
        if callback_data == "menu:start_sync":
            turn = stage_sync_confirmation(database)
            return turn.text, turn.reply_markup or main_menu_markup()
        if callback_data.startswith("decision_action_"):
            interaction_id = callback_data.removeprefix("decision_action_")
            row = database.get(PendingInteraction, interaction_id)
            if row and row.action_type == "request_reschedule":
                planned_id = row.target_id
                row.status = "superseded"
                turn = begin_reschedule_flow(database, planned_id)
                return turn.text, turn.reply_markup or main_menu_markup()
            _, text = apply_interaction(database, interaction_id)
            return text, main_menu_markup()
        if callback_data.startswith("decision_cancel_"):
            interaction_id = callback_data.removeprefix("decision_cancel_")
            return reject_interaction(database, interaction_id), main_menu_markup()
        if callback_data.startswith("decision_different_time_"):
            interaction_id = callback_data.removeprefix(
                "decision_different_time_"
            )
            turn = begin_alternate_time(database, interaction_id)
            return turn.text, turn.reply_markup or main_menu_markup()
        if callback_data.startswith("flow:"):
            turn = advance_button_flow(database, callback_data)
            return turn.text, turn.reply_markup or main_menu_markup()
        if callback_data.startswith("catalog_details_metric_"):
            return renderers.render_metrics(database), main_menu_markup()
    return "This button is no longer available.", main_menu_markup()


async def handle_telegram_update(data: dict) -> dict:
    if not update_deduplicator.accept(data.get("update_id")):
        return {"status": "ok"}
    callback = data.get("callback_query") or {}
    message = data.get("message") or callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id_raw = chat.get("id")
    chat_id = str(chat_id_raw) if chat_id_raw is not None else ""
    chat_type = chat.get("type")
    text = message.get("text") if not callback else None
    if chat_type != "private" or not chat_id:
        return {"status": "ok"}

    from notify import telegram
    from sync.scheduler import refresh_user_jobs
    from telegram_link import consume_link_code, resolve_chat_tenant, unlink_user

    parts = (text or "").strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].casefold() if parts else ""
    link_code = None
    if command == "/link" and len(parts) == 2:
        link_code = parts[1]
    elif (
        command == "/start"
        and len(parts) == 2
        and parts[1].startswith("link_")
    ):
        link_code = parts[1].removeprefix("link_")
    if link_code is not None:
        try:
            identity = consume_link_code(link_code, chat_id)
        except ValueError as exc:
            telegram.send_link_message(str(exc), chat_id)
            return {"status": "ok"}
        refresh_user_jobs(identity.user_id)
        telegram.send_link_message(
            "Telegram is linked to your GarminCoach account.", chat_id
        )
        return {"status": "ok"}

    identity = resolve_chat_tenant(chat_id)
    if identity is None:
        if command in {"/link", "/start"} or text:
            telegram.send_link_message(
                "This chat is not linked. Generate a link command in "
                "GarminCoach Account settings.",
                chat_id,
            )
        return {"status": "ok"}

    tenant_token = bind_tenant(identity)
    try:
        if command == "/unlink":
            await session_manager.close_session(identity.user_id)
            unlink_user(identity.user_id)
            refresh_user_jobs(identity.user_id)
            telegram.send_link_message(
                "Telegram was unlinked from GarminCoach.", chat_id
            )
            return {"status": "ok"}
        if command == "/start":
            await session_manager.close_session(identity.user_id)
            telegram.send_message(
                "GarminCoach menu",
                chat_id,
                main_menu_markup(),
                parse_mode=None,
            )
            return {"status": "ok"}

        if callback:
            callback_id = callback.get("id")
            callback_data = callback.get("data", "")
            message_id = callback.get("message", {}).get("message_id")
            telegram.answer_callback_query(callback_id)
            if message_id is None:
                return {"status": "ok"}
            active = await session_manager.has_active_session(
                identity.user_id, chat_id
            )
            if active and not callback_data.startswith("ask:"):
                _edit(
                    "Ask Coach is active. Return to the menu before using "
                    "other controls.",
                    chat_id,
                    message_id,
                    ask_coach_back_markup(),
                )
                return {"status": "ok"}

            if callback_data == "ask:exit":
                await session_manager.close_session(identity.user_id)
                _edit("GarminCoach menu", chat_id, message_id, main_menu_markup())
                return {"status": "ok"}
            if callback_data == "ask:consent_cancel":
                await session_manager.close_session(identity.user_id)
                _edit("GarminCoach menu", chat_id, message_id, main_menu_markup())
                return {"status": "ok"}
            if callback_data == "ask:consent_details":
                _edit(
                    _privacy_text(),
                    chat_id,
                    message_id,
                    consent_disclosure_markup(),
                )
                return {"status": "ok"}
            if callback_data == "ask:consent_agree":
                record_ask_coach_consent(
                    identity.user_id,
                    config.ASK_COACH_CONSENT_VERSION,
                    config.ASK_COACH_PROVIDER,
                    config.ASK_COACH_DATA_CATEGORIES_VERSION,
                    config.CURRENT_ASK_COACH_DATA_CATEGORIES,
                )
                await session_manager.create_session(identity.user_id, chat_id)
                _edit(
                    ASK_COACH_ACTIVE,
                    chat_id,
                    message_id,
                    ask_coach_back_markup(),
                )
                return {"status": "ok"}
            if callback_data == "ask:consent_revoke":
                revoke_ask_coach_consent(identity.user_id)
                await session_manager.close_session(identity.user_id)
                _edit(
                    "Ask Coach consent was revoked and its active session was "
                    "cleared.",
                    chat_id,
                    message_id,
                    main_menu_markup(),
                )
                return {"status": "ok"}
            if callback_data.startswith("ask:retry:"):
                nonce = callback_data.removeprefix("ask:retry:")
                question = await session_manager.pending_retry(
                    identity.user_id, chat_id, nonce
                )
                if question is None or not _valid_consent(identity.user_id):
                    _edit(
                        "This retry is no longer available.",
                        chat_id,
                        message_id,
                        ask_coach_back_markup(),
                    )
                    return {"status": "ok"}
                acquired = await session_manager.try_acquire_in_flight(
                    identity.user_id
                )
                if acquired.status != AcquireStatus.ACQUIRED:
                    _edit(
                        "Ask Coach is already working on a question.",
                        chat_id,
                        message_id,
                        ask_coach_back_markup(),
                    )
                    return {"status": "ok"}
                _register_task(
                    run_ask_coach_question(
                        identity=identity,
                        chat_id=chat_id,
                        generation_token=acquired.generation_token or "",
                        question=question,
                    )
                )
                return {"status": "ok"}
            if callback_data == "menu:ask_coach":
                if _valid_consent(identity.user_id):
                    await session_manager.create_session(
                        identity.user_id, chat_id
                    )
                    _edit(
                        ASK_COACH_ACTIVE,
                        chat_id,
                        message_id,
                        ask_coach_back_markup(),
                    )
                else:
                    _edit(
                        DISCLOSURE,
                        chat_id,
                        message_id,
                        consent_disclosure_markup(),
                    )
                return {"status": "ok"}
            if callback_data == "menu:privacy":
                consent = get_ask_coach_consent(identity.user_id)
                _edit(
                    _privacy_text(),
                    chat_id,
                    message_id,
                    privacy_markup(is_consent_valid(consent)),
                )
                return {"status": "ok"}
            if callback_data == "menu:unlink":
                _edit(
                    "Unlink this Telegram chat from GarminCoach?",
                    chat_id,
                    message_id,
                    {
                        "inline_keyboard": [
                            [{
                                "text": "Unlink",
                                "callback_data": "menu:unlink_confirm",
                            }],
                            [{
                                "text": "Cancel",
                                "callback_data": "menu:home",
                            }],
                        ]
                    },
                )
                return {"status": "ok"}
            if callback_data == "menu:unlink_confirm":
                await session_manager.close_session(identity.user_id)
                unlink_user(identity.user_id)
                refresh_user_jobs(identity.user_id)
                telegram.send_link_message(
                    "Telegram was unlinked from GarminCoach.", chat_id
                )
                return {"status": "ok"}
            text_out, markup = _operational_callback(
                callback_data, identity=identity, chat_id=chat_id
            )
            _edit(text_out, chat_id, message_id, markup)
            return {"status": "ok"}

        active = await session_manager.has_active_session(
            identity.user_id, chat_id
        )
        if active:
            if not text:
                telegram.send_message(
                    ASK_COACH_TEXT_ONLY,
                    chat_id,
                    ask_coach_back_markup(),
                    parse_mode=None,
                )
                return {"status": "ok"}
            if not _valid_consent(identity.user_id):
                await session_manager.close_session(identity.user_id)
                telegram.send_message(
                    DISCLOSURE,
                    chat_id,
                    consent_disclosure_markup(),
                    parse_mode=None,
                )
                return {"status": "ok"}
            acquired = await session_manager.try_acquire_in_flight(
                identity.user_id
            )
            if acquired.status == AcquireStatus.BUSY:
                telegram.send_message(
                    "Ask Coach is already working on your previous question.",
                    chat_id,
                    ask_coach_back_markup(),
                    parse_mode=None,
                )
                return {"status": "ok"}
            if acquired.status == AcquireStatus.NO_ACTIVE_SESSION:
                telegram.send_message(
                    OPERATIONAL_TEXT_GUIDANCE,
                    chat_id,
                    main_menu_markup(),
                    parse_mode=None,
                )
                return {"status": "ok"}
            _register_task(
                run_ask_coach_question(
                    identity=identity,
                    chat_id=chat_id,
                    generation_token=acquired.generation_token or "",
                    question=text,
                )
            )
            return {"status": "ok"}

        if text:
            telegram.send_message(
                OPERATIONAL_TEXT_GUIDANCE,
                chat_id,
                main_menu_markup(),
                parse_mode=None,
            )
        return {"status": "ok"}
    finally:
        reset_tenant(tenant_token)
