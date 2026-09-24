"""Local bounded-state and cooperative shutdown rehearsal for delegated tasks."""

import threading
import pytest

from api.task_delegation import TaskDelegator
from api.task_manager import TaskManager, TaskState
from api.metrics import SafeMetricsRecorder


def test_fifty_replacements_bound_state_and_reject_late_results():
    manager = TaskManager(max_active=2, max_retained=8)
    delegator = TaskDelegator(manager, workers=2)
    try:
        current = delegator.delegate(lambda token: True)
        delegator._futures[current.task_id].result(timeout=2)
        for _ in range(50):
            current = delegator.delegate(lambda token: True, parent_id=current.task_id)
            delegator._futures[current.task_id].result(timeout=2)
            assert manager.get(current.task_id).state is TaskState.COMPLETED
            assert len(manager.snapshots()) <= 8
    finally:
        delegator.close()
    assert not any(t.name.startswith("helios-task") and t.is_alive() for t in threading.enumerate())


def test_repeated_cancelled_work_does_not_commit():
    started, release = threading.Event(), threading.Event()
    delegator = TaskDelegator(workers=2)
    try:
        for _ in range(20):
            started.clear()
            task = delegator.delegate(lambda token: started.set() or release.wait(1))
            assert started.wait(2)
            delegator.cancel(task.task_id)
            release.set()
            delegator._futures[task.task_id].result(timeout=2)
            release.clear()
            assert delegator.manager.get(task.task_id).state is TaskState.CANCELLED
    finally:
        release.set()
        delegator.close()


def test_task_latency_metric_is_content_free_and_shutdown_is_bounded():
    recorder = SafeMetricsRecorder()
    started, release = threading.Event(), threading.Event()
    delegator = TaskDelegator(metrics=recorder)
    try:
        finished = delegator.delegate(lambda token: True)
        delegator._futures[finished.task_id].result(timeout=2)
        events = recorder.snapshot()
        assert any(
            event.event == "delegated_task_finished" and event.latency_ms >= 0 for event in events
        )
        hanging = delegator.delegate(lambda token: started.set() or release.wait(2))
        assert started.wait(2)
        with pytest.raises(TimeoutError):
            delegator.close(timeout=0)
        assert delegator.manager.get(hanging.task_id).state is TaskState.CANCELLED
    finally:
        release.set()
        if "hanging" in locals():
            delegator._futures[hanging.task_id].result(timeout=2)
        delegator.close()
