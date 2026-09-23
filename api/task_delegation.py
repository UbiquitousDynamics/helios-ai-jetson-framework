"""Bounded fake delegation with explicit confirmation and verified outcomes."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import RLock
from typing import Callable
import time

from api.metrics import record_safely
from api.task_manager import TaskManager, TaskSnapshot, TaskState, TERMINAL


class TaskDelegator:
    """Run injected work off the conversation thread; retain only state snapshots."""

    def __init__(self, manager: TaskManager | None = None, *, workers: int = 2,
                 metrics: object | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        self.manager = manager or TaskManager()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="helios-task")
        self._lock = RLock()
        self._futures: dict[str, Future[None]] = {}
        self._started: dict[str, float] = {}
        self._metrics, self._clock = metrics, clock
        self._closed = False

    def delegate(self, work: Callable, *, parent_id: str | None = None,
                 requires_confirmation: bool = False) -> TaskSnapshot:
        if not callable(work):
            raise TypeError("work must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("delegator closed")
            task = self.manager.create(parent_id=parent_id)
            self._started[task.task_id] = self._clock()
            self._prune_futures()
            token = self.manager.token(task.task_id)
            def run() -> None:
                try:
                    self.manager.transition(task.task_id, TaskState.EXECUTING)
                    if requires_confirmation:
                        self.manager.transition(task.task_id, TaskState.WAITING_FOR_USER)
                        return
                    success = bool(work(token))
                    self._finish(task.task_id, success)
                except Exception:
                    self._finish(task.task_id, False)
            try:
                self._futures[task.task_id] = self._executor.submit(run)
            except RuntimeError:
                self.manager.cancel(task.task_id)
                raise
            return task

    def confirm(self, task_id: str, allowed: bool, work: Callable) -> TaskSnapshot:
        if not isinstance(allowed, bool) or not callable(work):
            raise TypeError("invalid confirmation")
        with self._lock:
            current = self.manager.get(task_id)
            if current is None or current.state is not TaskState.WAITING_FOR_USER:
                raise ValueError("task is not waiting for confirmation")
            if not allowed:
                result = self.manager.transition(task_id, TaskState.CANCELLED)
                self._record_terminal(result)
                return result
            executing = self.manager.transition(task_id, TaskState.EXECUTING)
            token = self.manager.token(task_id)
            def run() -> None:
                try:
                    self._finish(task_id, bool(work(token)))
                except Exception:
                    self._finish(task_id, False)
            self._futures[task_id] = self._executor.submit(run)
            return executing

    def _finish(self, task_id: str, success: bool) -> None:
        with self._lock:
            current = self.manager.get(task_id)
            if current is not None and current.state is TaskState.EXECUTING:
                result = self.manager.transition(
                    task_id, TaskState.COMPLETED if success else TaskState.FAILED,
                    confirmed=success,
                )
                self._record_terminal(result)

    def _record_terminal(self, task: TaskSnapshot) -> None:
        started = self._started.pop(task.task_id, None)
        record_safely(self._metrics, "delegated_task_finished", outcome=task.state.value,
                      success=task.state is TaskState.COMPLETED,
                      latency_ms=max(0.0, (self._clock() - started) * 1000) if started is not None else None)

    def _prune_futures(self) -> None:
        for task_id, future in tuple(self._futures.items()):
            if future.done() and self.manager.get(task_id) is None:
                del self._futures[task_id]

    def cancel(self, task_id: str) -> TaskSnapshot:
        with self._lock:
            result = self.manager.cancel(task_id)
            self._record_terminal(result)
            return result

    def supersede(self, task_id: str, work: Callable) -> TaskSnapshot:
        with self._lock:
            current = self.manager.get(task_id)
            if current is None or current.state in TERMINAL:
                raise ValueError("cannot supersede terminal task")
            result = self.manager.transition(task_id, TaskState.SUPERSEDED)
            self._record_terminal(result)
            return self.delegate(work, parent_id=task_id)

    def status(self, task_id: str) -> str:
        """Return a sparse, content-free progress statement."""

        snapshot = self.manager.get(task_id)
        if snapshot is None:
            raise KeyError(task_id)
        return {
            TaskState.PENDING: "Task queued.",
            TaskState.EXECUTING: "Task in progress.",
            TaskState.WAITING_FOR_USER: "Task needs confirmation.",
            TaskState.COMPLETED: "Task completed and confirmed.",
            TaskState.FAILED: "Task failed.",
            TaskState.CANCELLED: "Task cancelled.",
            TaskState.SUPERSEDED: "Task replaced.",
        }[snapshot.state]

    def close(self, *, timeout: float = 2.0) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError("timeout must be nonnegative")
        with self._lock:
            self._closed = True
            for task in tuple(self.manager.snapshots()):
                if task.state not in TERMINAL:
                    self.cancel(task.task_id)
            futures = tuple(self._futures.values())
        self._executor.shutdown(wait=False, cancel_futures=True)
        _, pending = wait(futures, timeout=timeout)
        if pending:
            raise TimeoutError("delegated task shutdown timed out")
        self._executor.shutdown(wait=True, cancel_futures=True)
