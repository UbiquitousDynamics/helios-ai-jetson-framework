from concurrent.futures import ThreadPoolExecutor

import pytest

from api.task_manager import FakeTaskBackend, TaskManager, TaskState


def test_lifecycle_and_verified_completion():
    manager = TaskManager(id_factory=lambda: "one")
    task = manager.create()
    token = manager.token(task.task_id)
    assert manager.transition(task.task_id, TaskState.EXECUTING).revision == 1
    assert manager.transition(task.task_id, TaskState.WAITING_FOR_USER).state is TaskState.WAITING_FOR_USER
    manager.transition(task.task_id, TaskState.EXECUTING)
    with pytest.raises(ValueError):
        manager.transition(task.task_id, TaskState.COMPLETED)
    assert FakeTaskBackend().commit(token)
    done = manager.transition(task.task_id, TaskState.COMPLETED, confirmed=True)
    assert done.confirmed and manager.get(task.task_id) == done
    with pytest.raises(ValueError):
        manager.cancel(task.task_id)


def test_capacity_retention_and_validation():
    manager = TaskManager(max_active=1, max_retained=1, id_factory=iter(["a", "b", "c"]).__next__)
    a = manager.create()
    with pytest.raises(RuntimeError):
        manager.create()
    manager.cancel(a.task_id)
    b = manager.create(parent_id=a.task_id)
    assert b.parent_id == a.task_id
    manager.cancel(b.task_id)
    assert manager.get(a.task_id) is None
    assert manager.get(b.task_id) is not None
    with pytest.raises(ValueError):
        manager.create(parent_id="missing")


def test_concurrent_terminal_race_has_one_winner():
    manager = TaskManager(id_factory=lambda: "one")
    task = manager.create()
    manager.transition(task.task_id, TaskState.EXECUTING)
    token = manager.token(task.task_id)

    def finish(state):
        try:
            return manager.transition(task.task_id, state, confirmed=state is TaskState.COMPLETED)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, (TaskState.COMPLETED, TaskState.CANCELLED)))
    assert sum(x is not None for x in outcomes) == 1
    assert manager.get(task.task_id).state in {TaskState.COMPLETED, TaskState.CANCELLED}
    assert token.cancelled == (manager.get(task.task_id).state is TaskState.CANCELLED)


@pytest.mark.parametrize("kwargs", [{"max_active": 0}, {"max_active": True}, {"max_retained": -1}])
def test_invalid_bounds(kwargs):
    with pytest.raises(ValueError):
        TaskManager(**kwargs)
