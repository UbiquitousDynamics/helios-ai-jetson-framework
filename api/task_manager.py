"""Provider-neutral, content-free lifecycle for bounded delegated work."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from api.streaming import CancellationController


class TaskState(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


TERMINAL = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.SUPERSEDED}
)
_NEXT = {
    TaskState.PENDING: {TaskState.EXECUTING, TaskState.CANCELLED, TaskState.SUPERSEDED},
    TaskState.EXECUTING: {TaskState.WAITING_FOR_USER, *TERMINAL},
    TaskState.WAITING_FOR_USER: {
        TaskState.EXECUTING,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.SUPERSEDED,
    },
}


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    state: TaskState
    revision: int
    parent_id: str | None = None
    confirmed: bool = False


class TaskManager:
    """Lock-linearized lifecycle; no transcript, tool payload, or backend result is retained."""

    def __init__(
        self,
        *,
        max_active: int = 8,
        max_retained: int = 32,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if isinstance(max_active, bool) or not isinstance(max_active, int) or max_active < 1:
            raise ValueError("max_active must be a positive integer")
        if isinstance(max_retained, bool) or not isinstance(max_retained, int) or max_retained < 0:
            raise ValueError("max_retained must be a nonnegative integer")
        self._lock = threading.RLock()
        self._active: dict[str, TaskSnapshot] = {}
        self._retained: dict[str, TaskSnapshot] = {}
        self._order: deque[str] = deque()
        self._tokens: dict[str, CancellationController] = {}
        self._max_active, self._max_retained = max_active, max_retained
        self._id_factory = id_factory

    def create(self, *, parent_id: str | None = None) -> TaskSnapshot:
        with self._lock:
            if len(self._active) >= self._max_active:
                raise RuntimeError("task capacity reached")
            if parent_id is not None and self.get(parent_id) is None:
                raise ValueError("unknown parent task")
            task_id = self._id_factory()
            if not isinstance(task_id, str) or not task_id or self.get(task_id) is not None:
                raise ValueError("invalid or duplicate task ID")
            snapshot = TaskSnapshot(task_id, TaskState.PENDING, 0, parent_id)
            self._active[task_id] = snapshot
            self._tokens[task_id] = CancellationController()
            return snapshot

    def get(self, task_id: str) -> TaskSnapshot | None:
        with self._lock:
            return self._active.get(task_id) or self._retained.get(task_id)

    def token(self, task_id: str) -> CancellationController:
        with self._lock:
            return self._tokens[task_id]

    def snapshots(self) -> tuple[TaskSnapshot, ...]:
        with self._lock:
            return (*self._active.values(), *self._retained.values())

    def transition(
        self, task_id: str, state: TaskState, *, confirmed: bool = False
    ) -> TaskSnapshot:
        if not isinstance(state, TaskState) or not isinstance(confirmed, bool):
            raise TypeError("invalid task transition")
        with self._lock:
            old = self._active.get(task_id)
            if old is None or state not in _NEXT.get(old.state, set()):
                raise ValueError("illegal task transition")
            if state is TaskState.COMPLETED and not confirmed:
                raise ValueError("completion requires backend confirmation")
            if state is not TaskState.COMPLETED and confirmed:
                raise ValueError("confirmation only applies to completion")
            new = TaskSnapshot(task_id, state, old.revision + 1, old.parent_id, confirmed)
            if state in {TaskState.CANCELLED, TaskState.SUPERSEDED}:
                self._tokens[task_id].cancel()
            if state in TERMINAL:
                del self._active[task_id]
                del self._tokens[task_id]
                if self._max_retained:
                    self._retained[task_id] = new
                    self._order.append(task_id)
                    while len(self._order) > self._max_retained:
                        del self._retained[self._order.popleft()]
            else:
                self._active[task_id] = new
            return new

    def cancel(self, task_id: str) -> TaskSnapshot:
        return self.transition(task_id, TaskState.CANCELLED)

    def cancel_all(self) -> None:
        with self._lock:
            for task_id in tuple(self._active):
                self.cancel(task_id)


class FakeTaskBackend:
    """Deterministic backend whose commit is explicit and cancellation-aware."""

    def commit(self, token: CancellationController, *, succeed: bool = True) -> bool:
        return token.commit_if_not_cancelled(lambda: succeed)
