from __future__ import annotations

import main as entrypoint
import pytest


class FakeAssistant:
    def __init__(self, *, interrupt: bool = False) -> None:
        self.interrupt = interrupt
        self.entered = False
        self.exited = False
        self.ran = False

    def __enter__(self) -> FakeAssistant:
        self.entered = True
        return self

    def __exit__(self, *_args: object) -> None:
        self.exited = True

    def run(self) -> None:
        self.ran = True
        if self.interrupt:
            raise KeyboardInterrupt


def test_main_runs_and_closes_the_assistant(monkeypatch) -> None:
    assistant = FakeAssistant()
    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(entrypoint, "VoiceAssistant", lambda: assistant)

    assert entrypoint.main() == 0
    assert assistant.entered is True
    assert assistant.ran is True
    assert assistant.exited is True


def test_main_force_exits_when_runtime_shutdown_is_interrupted(monkeypatch) -> None:
    assistant = FakeAssistant(interrupt=True)
    forced: list[bool] = []
    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(entrypoint, "VoiceAssistant", lambda: assistant)
    monkeypatch.setattr(
        entrypoint,
        "_force_exit_after_repeated_interrupt",
        lambda: forced.append(True),
    )

    assert entrypoint.main() == 130
    assert forced == [True]
    assert assistant.exited is True


def test_main_force_exits_when_owned_worker_misses_shutdown_deadline(
    monkeypatch,
) -> None:
    class TimedOutAssistant(FakeAssistant):
        def run(self) -> None:
            self.ran = True
            raise entrypoint.AssistantShutdownTimeout("worker still running")

    assistant = TimedOutAssistant()
    forced: list[bool] = []
    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(entrypoint, "VoiceAssistant", lambda: assistant)
    monkeypatch.setattr(
        entrypoint,
        "_force_exit_after_repeated_interrupt",
        lambda: forced.append(True),
    )

    assert entrypoint.main() == 130
    assert forced == [True]
    assert assistant.exited is True


def test_forced_exit_uses_sigint_status_without_logging_locks(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    exits: list[int] = []
    monkeypatch.setattr(
        entrypoint.os,
        "write",
        lambda descriptor, value: writes.append((descriptor, value)),
    )
    monkeypatch.setattr(entrypoint.os, "_exit", lambda status: exits.append(status))

    entrypoint._force_exit_after_repeated_interrupt()

    assert writes == [(2, b"Graceful shutdown deadline exceeded; forcing Helios to exit.\n")]
    assert exits == [130]


def test_second_sigint_forces_exit_even_before_close_starts(monkeypatch) -> None:
    forced: list[bool] = []
    handler = entrypoint._TwoStageInterruptHandler()
    monkeypatch.setattr(
        entrypoint,
        "_force_exit_after_repeated_interrupt",
        lambda: forced.append(True),
    )

    with pytest.raises(KeyboardInterrupt):
        handler(2, None)
    handler(2, None)

    assert forced == [True]
