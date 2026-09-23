from threading import Event

import pytest

from api.task_delegation import TaskDelegator
from api.task_manager import TaskState
from assistant import VoiceAssistant
import config
from test_assistant import FakeAPI, FakeRecognizer, FakeSoundPlayer, FakeTTS, ImmediateExecutor


def test_conversation_can_continue_while_work_runs_and_late_result_is_rejected():
    started, release = Event(), Event()
    delegator = TaskDelegator()
    def work(token):
        started.set()
        release.wait(2)
        return True
    try:
        task = delegator.delegate(work)
        assert started.wait(2)
        assert delegator.manager.get(task.task_id).state is TaskState.EXECUTING
        replacement = delegator.supersede(task.task_id, lambda token: True)
        release.set()
        delegator._futures[task.task_id].result(timeout=2)
        delegator._futures[replacement.task_id].result(timeout=2)
        assert delegator.manager.get(task.task_id).state is TaskState.SUPERSEDED
        assert delegator.manager.get(replacement.task_id).state is TaskState.COMPLETED
    finally:
        release.set()
        delegator.close()


def test_confirmation_is_required_immediately_before_commit():
    delegator = TaskDelegator()
    calls = []
    try:
        task = delegator.delegate(lambda token: calls.append(1), requires_confirmation=True)
        delegator._futures[task.task_id].result(timeout=2)
        assert delegator.manager.get(task.task_id).state is TaskState.WAITING_FOR_USER
        assert not calls
        delegator.confirm(task.task_id, True, lambda token: calls.append(1) or True)
        delegator._futures[task.task_id].result(timeout=2)
        assert calls == [1]
        assert delegator.manager.get(task.task_id).confirmed
        with pytest.raises(ValueError):
            delegator.confirm(task.task_id, True, lambda token: True)
    finally:
        delegator.close()


def test_denial_failure_and_cancel():
    delegator = TaskDelegator()
    try:
        denied = delegator.delegate(lambda token: True, requires_confirmation=True)
        delegator._futures[denied.task_id].result(timeout=2)
        assert delegator.confirm(denied.task_id, False, lambda token: True).state is TaskState.CANCELLED
        failed = delegator.delegate(lambda token: False)
        delegator._futures[failed.task_id].result(timeout=2)
        assert delegator.manager.get(failed.task_id).state is TaskState.FAILED
    finally:
        delegator.close()


def test_assistant_local_cancel_targets_task_only():
    started, release = Event(), Event()
    delegator = TaskDelegator()
    assistant = VoiceAssistant(
        settings=config.Settings(language="en", barge_in_enabled=False),
        tts=FakeTTS(), api_client=FakeAPI(), speech_recognizer=FakeRecognizer([]),
        sound_player=FakeSoundPlayer(), sound_executor=ImmediateExecutor(),
        task_delegator=delegator,
    )
    try:
        def work(token):
            started.set()
            release.wait(2)
            return True
        task = assistant.delegate_task(work)
        assert started.wait(2)
        assistant.process_command("cancel task")
        assert assistant.last_control_result[1]
        assert delegator.manager.get(task.task_id).state is TaskState.CANCELLED
        assert assistant.api_client.messages == []
        assert not assistant.api_client.cancelled
    finally:
        release.set()
        assistant.close()
        delegator.close()


def test_assistant_steering_keeps_causal_link_and_history_untouched():
    started, release = Event(), Event()
    delegator = TaskDelegator()
    assistant = VoiceAssistant(
        settings=config.Settings(language="en", barge_in_enabled=False),
        tts=FakeTTS(), api_client=FakeAPI(), speech_recognizer=FakeRecognizer([]),
        sound_player=FakeSoundPlayer(), sound_executor=ImmediateExecutor(),
        task_delegator=delegator,
    )
    try:
        old = assistant.delegate_task(lambda token: started.set() or release.wait(2))
        assert started.wait(2)
        new = assistant.steer_task(old.task_id, lambda token: True)
        release.set()
        delegator._futures[old.task_id].result(timeout=2)
        delegator._futures[new.task_id].result(timeout=2)
        assert new.parent_id == old.task_id
        assert delegator.status(old.task_id) == "Task replaced."
        assert delegator.status(new.task_id) == "Task completed and confirmed."
        assert assistant.api_client.messages == []
        assert assistant._active_delegated_task_id == new.task_id
    finally:
        release.set()
        assistant.close()
        delegator.close()
