"""Provider-neutral, in-process conversation session state."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from api.providers.contracts import ChatMessage, ContentOrigin, PrivacyLevel, Role

logger = logging.getLogger(__name__)


class ConversationTurnStatus(str, Enum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(slots=True)
class ConversationTurn:
    number: int
    user: ChatMessage
    privacy: PrivacyLevel = PrivacyLevel.LOCAL_ONLY
    status: ConversationTurnStatus = ConversationTurnStatus.PENDING
    assistant: ChatMessage | None = None


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    session_id: str
    turn_count: int
    retained_turn_count: int
    active_turn: int | None
    active_turn_status: ConversationTurnStatus | None
    last_reset_reason: str | None
    last_activity_at: float | None
    idle_timeout_seconds: float
    max_history_turns: int
    history_message_count: int
    provider_threads: tuple[tuple[str, str], ...]


def safe_conversation_identifier(value: str | None) -> str:
    """Return one stable, content-free correlation ID for cross-layer logs."""

    if value is None:
        return "none"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class ConversationSessionError(RuntimeError):
    """Raised when callers violate the serialized session lifecycle."""


class ConversationSession:
    """Own canonical user/assistant history independently of any provider.

    Finalized user turns are committed before dispatch. Assistant text is
    committed only after a successful terminal completion. Interrupted or
    failed assistant output is deliberately omitted from canonical history.
    """

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = 900.0,
        max_history_turns: int = 20,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if (
            isinstance(idle_timeout_seconds, bool)
            or not isinstance(idle_timeout_seconds, (int, float))
            or not math.isfinite(float(idle_timeout_seconds))
            or idle_timeout_seconds <= 0
        ):
            raise ValueError("idle_timeout_seconds must be positive")
        if (
            isinstance(max_history_turns, bool)
            or not isinstance(max_history_turns, int)
            or max_history_turns < 1
        ):
            raise ValueError("max_history_turns must be a positive integer")
        self._idle_timeout_seconds = float(idle_timeout_seconds)
        self._max_history_turns = max_history_turns
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.RLock()
        self._session_id = self._new_session_id()
        self._turns: list[ConversationTurn] = []
        self._next_turn_number = 1
        self._active_turn: ConversationTurn | None = None
        self._last_activity_at: float | None = None
        self._last_reset_reason: str | None = None
        self._provider_threads: dict[str, str] = {}
        logger.info(
            "conversation_session=%s event=session_started",
            safe_conversation_identifier(self._session_id),
        )

    def _new_session_id(self) -> str:
        value = str(self._id_factory()).strip()
        if not value:
            raise ValueError("id_factory must return a non-empty identifier")
        return value

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    def _reset_locked(self, reason: str) -> None:
        previous = self._session_id
        self._session_id = self._new_session_id()
        self._turns.clear()
        self._next_turn_number = 1
        self._active_turn = None
        self._last_activity_at = None
        self._last_reset_reason = reason
        self._provider_threads.clear()
        logger.info(
            "conversation_session=%s event=session_reset reason=%s next_session=%s",
            safe_conversation_identifier(previous),
            reason,
            safe_conversation_identifier(self._session_id),
        )

    def reset(self, *, reason: str = "explicit") -> str:
        reason = reason.strip()
        if not reason:
            raise ValueError("reset reason cannot be empty")
        with self._lock:
            if self._active_turn is not None:
                raise ConversationSessionError("cannot reset while a turn is active")
            self._reset_locked(reason)
            return self._session_id

    def _expire_if_idle_locked(self, now: float) -> None:
        if (
            self._active_turn is None
            and self._last_activity_at is not None
            and now - self._last_activity_at >= self._idle_timeout_seconds
        ):
            self._reset_locked("idle_timeout")

    def begin_turn(
        self,
        message: str,
        *,
        privacy: PrivacyLevel | str = PrivacyLevel.LOCAL_ONLY,
        redacted: bool = False,
    ) -> ConversationTurn:
        normalized = message.strip()
        if not normalized:
            raise ValueError("message cannot be empty")
        selected_privacy = PrivacyLevel(privacy)
        now = self._clock()
        with self._lock:
            self._expire_if_idle_locked(now)
            if self._active_turn is not None:
                raise ConversationSessionError("another conversation turn is already active")
            turn = ConversationTurn(
                number=self._next_turn_number,
                user=ChatMessage(
                    Role.USER,
                    normalized,
                    origin=ContentOrigin.RAW_TRANSCRIPT,
                    redacted=redacted,
                    remote_eligible=(
                        selected_privacy is PrivacyLevel.REMOTE_ALLOWED
                        or (
                            selected_privacy is PrivacyLevel.REMOTE_REDACTED
                            and redacted
                        )
                    ),
                ),
                privacy=selected_privacy,
            )
            self._next_turn_number += 1
            self._turns.append(turn)
            self._active_turn = turn
            self._last_activity_at = now
            logger.info(
                "conversation_session=%s turn=%s event=user_turn_finalized",
                safe_conversation_identifier(self._session_id),
                turn.number,
            )
            return turn

    def mark_streaming(self, turn: ConversationTurn) -> None:
        with self._lock:
            if self._active_turn is not turn or turn.status is not ConversationTurnStatus.PENDING:
                raise ConversationSessionError("turn cannot enter streaming state")
            turn.status = ConversationTurnStatus.STREAMING
            self._last_activity_at = self._clock()
            logger.info(
                "conversation_session=%s turn=%s event=assistant_turn_streaming",
                safe_conversation_identifier(self._session_id),
                turn.number,
            )

    def history_before(self, turn: ConversationTurn) -> tuple[ChatMessage, ...]:
        with self._lock:
            if self._active_turn is not turn:
                raise ConversationSessionError("turn is not active in this session")
            previous = self._turns[:-1]
            retained = previous[-self._max_history_turns :]
            messages: list[ChatMessage] = []
            for item in retained:
                messages.append(
                    ChatMessage(
                        Role.USER,
                        item.user.content,
                        origin=ContentOrigin.CONVERSATION_HISTORY,
                        redacted=item.user.redacted,
                        remote_eligible=item.user.remote_eligible,
                        source_origins=(
                            item.user.source_origins | {item.user.origin}
                        ),
                    )
                )
                if item.assistant is not None:
                    messages.append(
                        ChatMessage(
                            Role.ASSISTANT,
                            item.assistant.content,
                            origin=ContentOrigin.CONVERSATION_HISTORY,
                            redacted=item.assistant.redacted,
                            remote_eligible=item.assistant.remote_eligible,
                            source_origins=(
                                item.assistant.source_origins | {item.assistant.origin}
                            ),
                        )
                    )
            if len(previous) > len(retained):
                logger.info(
                    "conversation_session=%s turn=%s event=history_trimmed "
                    "retained_turns=%s omitted_turns=%s",
                    safe_conversation_identifier(self._session_id),
                    turn.number,
                    len(retained),
                    len(previous) - len(retained),
                )
            return tuple(messages)

    def complete_turn(
        self,
        turn: ConversationTurn,
        response: str,
        *,
        redacted: bool = False,
        remote_eligible: bool | None = None,
        source_origins: frozenset[ContentOrigin] = frozenset(),
    ) -> None:
        normalized = response.strip()
        if not normalized:
            raise ValueError("completed assistant response cannot be empty")
        with self._lock:
            if self._active_turn is not turn:
                raise ConversationSessionError("turn is not active in this session")
            turn.assistant = ChatMessage(
                Role.ASSISTANT,
                normalized,
                origin=ContentOrigin.CONVERSATION_HISTORY,
                redacted=redacted,
                remote_eligible=(
                    turn.user.remote_eligible
                    if remote_eligible is None
                    else remote_eligible
                ),
                source_origins=source_origins,
            )
            turn.status = ConversationTurnStatus.COMPLETED
            self._active_turn = None
            self._last_activity_at = self._clock()
            logger.info(
                "conversation_session=%s turn=%s event=assistant_turn_completed",
                safe_conversation_identifier(self._session_id),
                turn.number,
            )
            self._trim_locked()

    def fail_turn(self, turn: ConversationTurn, *, interrupted: bool) -> None:
        with self._lock:
            if self._active_turn is not turn:
                raise ConversationSessionError("turn is not active in this session")
            turn.status = (
                ConversationTurnStatus.INTERRUPTED
                if interrupted
                else ConversationTurnStatus.FAILED
            )
            self._active_turn = None
            self._last_activity_at = self._clock()
            logger.info(
                "conversation_session=%s turn=%s event=assistant_turn_%s",
                safe_conversation_identifier(self._session_id),
                turn.number,
                turn.status.value,
            )
            self._trim_locked()

    def _trim_locked(self) -> None:
        omitted = max(0, len(self._turns) - self._max_history_turns)
        if omitted == 0:
            return
        del self._turns[:omitted]
        logger.info(
            "conversation_session=%s event=canonical_history_trimmed "
            "retained_turns=%s omitted_turns=%s",
            safe_conversation_identifier(self._session_id),
            len(self._turns),
            omitted,
        )

    def bind_provider_thread(
        self,
        provider: str,
        thread_id: str,
        *,
        session_id: str,
    ) -> bool:
        """Mirror a provider's physical thread into the current logical session."""

        normalized_provider = provider.strip()
        normalized_thread = thread_id.strip()
        if not normalized_provider or not normalized_thread:
            raise ValueError("provider and thread_id must be non-empty")
        with self._lock:
            if session_id != self._session_id:
                return False
            self._provider_threads[normalized_provider] = normalized_thread
            logger.info(
                "conversation_session=%s provider=%s event=provider_thread_bound thread=%s",
                safe_conversation_identifier(self._session_id),
                normalized_provider,
                safe_conversation_identifier(normalized_thread),
            )
            return True

    def clear_provider_thread(
        self,
        provider: str,
        *,
        session_id: str,
        reason: str,
    ) -> bool:
        with self._lock:
            if session_id != self._session_id:
                return False
            removed = self._provider_threads.pop(provider, None)
            if removed is not None:
                logger.info(
                    "conversation_session=%s provider=%s event=provider_thread_cleared "
                    "thread=%s reason=%s",
                    safe_conversation_identifier(self._session_id),
                    provider,
                    safe_conversation_identifier(removed),
                    reason,
                )
            return removed is not None

    def snapshot(self) -> ConversationSnapshot:
        with self._lock:
            history_count = sum(
                1 + (1 if turn.assistant is not None else 0) for turn in self._turns
            )
            return ConversationSnapshot(
                session_id=self._session_id,
                turn_count=self._next_turn_number - 1,
                retained_turn_count=len(self._turns),
                active_turn=(self._active_turn.number if self._active_turn is not None else None),
                active_turn_status=(
                    self._active_turn.status if self._active_turn is not None else None
                ),
                last_reset_reason=self._last_reset_reason,
                last_activity_at=self._last_activity_at,
                idle_timeout_seconds=self._idle_timeout_seconds,
                max_history_turns=self._max_history_turns,
                history_message_count=history_count,
                provider_threads=tuple(sorted(self._provider_threads.items())),
            )


__all__ = [
    "ConversationSession",
    "ConversationSessionError",
    "ConversationSnapshot",
    "ConversationTurn",
    "ConversationTurnStatus",
    "safe_conversation_identifier",
]
