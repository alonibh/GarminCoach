"""Concurrency-safe, memory-only Ask Coach conversation state."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4

import config


class AcquireStatus(StrEnum):
    ACQUIRED = "acquired"
    NO_ACTIVE_SESSION = "no_active_session"
    BUSY = "busy"


@dataclass(frozen=True)
class AcquireResult:
    status: AcquireStatus
    generation_token: str | None = None


@dataclass(frozen=True)
class AskCoachSessionView:
    user_id: str
    chat_id: str
    history: tuple[dict[str, str], ...]
    last_activity_at: datetime
    in_flight_token: str | None
    pending_retry_question: str | None
    pending_retry_nonce: str | None


@dataclass
class AskCoachSession:
    user_id: str
    chat_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    last_activity_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    in_flight_token: str | None = None
    pending_retry_question: str | None = None
    pending_retry_nonce: str | None = None


class AskCoachSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, AskCoachSession] = {}
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def _expired(
        self, session: AskCoachSession, now: datetime | None = None
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        return current - session.last_activity_at >= timedelta(
            minutes=config.ASK_COACH_SESSION_IDLE_MINUTES
        )

    def _view(self, session: AskCoachSession) -> AskCoachSessionView:
        return AskCoachSessionView(
            user_id=session.user_id,
            chat_id=session.chat_id,
            history=tuple(dict(message) for message in session.history),
            last_activity_at=session.last_activity_at,
            in_flight_token=session.in_flight_token,
            pending_retry_question=session.pending_retry_question,
            pending_retry_nonce=session.pending_retry_nonce,
        )

    async def create_session(
        self, user_id: str, chat_id: str
    ) -> AskCoachSessionView:
        async with self._lock:
            session = AskCoachSession(user_id=user_id, chat_id=str(chat_id))
            self._sessions[user_id] = session
            return self._view(session)

    async def get_session(self, user_id: str) -> AskCoachSessionView | None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session and self._expired(session):
                del self._sessions[user_id]
                return None
            return self._view(session) if session else None

    async def has_active_session(
        self, user_id: str, chat_id: str | None = None
    ) -> bool:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session and self._expired(session):
                del self._sessions[user_id]
                return False
            return bool(
                session
                and (chat_id is None or session.chat_id == str(chat_id))
            )

    async def close_session(self, user_id: str) -> bool:
        async with self._lock:
            return self._sessions.pop(user_id, None) is not None

    async def clear_all(self) -> None:
        async with self._lock:
            self._sessions.clear()

    async def expire_idle_sessions(self) -> list[str]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            expired = [
                user_id
                for user_id, session in self._sessions.items()
                if self._expired(session, now)
            ]
            for user_id in expired:
                del self._sessions[user_id]
            return expired

    async def try_acquire_in_flight(self, user_id: str) -> AcquireResult:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None or self._expired(session):
                self._sessions.pop(user_id, None)
                return AcquireResult(AcquireStatus.NO_ACTIVE_SESSION)
            if session.in_flight_token is not None:
                return AcquireResult(AcquireStatus.BUSY)
            token = uuid4().hex
            session.in_flight_token = token
            session.last_activity_at = datetime.now(timezone.utc)
            return AcquireResult(AcquireStatus.ACQUIRED, token)

    async def clear_in_flight_if_matches(
        self, user_id: str, generation_token: str
    ) -> None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session and session.in_flight_token == generation_token:
                session.in_flight_token = None

    async def validate_session_for_delivery(
        self, user_id: str, chat_id: str, generation_token: str
    ) -> bool:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session and self._expired(session):
                del self._sessions[user_id]
                return False
            return bool(
                session
                and session.chat_id == str(chat_id)
                and session.in_flight_token == generation_token
            )

    async def history_for_generation(
        self, user_id: str, generation_token: str
    ) -> list[dict[str, str]] | None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if not session or session.in_flight_token != generation_token:
                return None
            return [dict(message) for message in session.history]

    def _trim_history(self, history: list[dict[str, str]]) -> None:
        maximum_messages = config.ASK_COACH_HISTORY_MAX_MESSAGES
        maximum_chars = config.ASK_COACH_HISTORY_MAX_CHARS
        while len(history) > maximum_messages:
            history.pop(0)
        while history and sum(
            len(message.get("content", "")) for message in history
        ) > maximum_chars:
            history.pop(0)

    async def record_successful_turn(
        self,
        user_id: str,
        generation_token: str,
        question: str,
        response: str,
    ) -> bool:
        async with self._lock:
            session = self._sessions.get(user_id)
            if not session or session.in_flight_token != generation_token:
                return False
            session.history.extend(
                (
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": response},
                )
            )
            self._trim_history(session.history)
            session.pending_retry_question = None
            session.pending_retry_nonce = None
            session.last_activity_at = datetime.now(timezone.utc)
            return True

    async def set_pending_retry(
        self, user_id: str, generation_token: str, question: str
    ) -> str | None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if not session or session.in_flight_token != generation_token:
                return None
            nonce = uuid4().hex[:16]
            session.pending_retry_question = question
            session.pending_retry_nonce = nonce
            session.last_activity_at = datetime.now(timezone.utc)
            return nonce

    async def pending_retry(
        self, user_id: str, chat_id: str, nonce: str
    ) -> str | None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session and self._expired(session):
                del self._sessions[user_id]
                return None
            if (
                not session
                or session.chat_id != str(chat_id)
                or session.pending_retry_nonce != nonce
            ):
                return None
            return session.pending_retry_question


session_manager = AskCoachSessionManager()
_cleanup_task: asyncio.Task | None = None


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        await session_manager.expire_idle_sessions()


def start_session_cleanup_task() -> asyncio.Task:
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(
            _cleanup_loop(), name="ask-coach-session-cleanup"
        )
    return _cleanup_task


async def cancel_session_cleanup_task() -> None:
    global _cleanup_task
    task = _cleanup_task
    _cleanup_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def clear_in_memory_state() -> None:
    await session_manager.clear_all()
