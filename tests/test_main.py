from __future__ import annotations

import logging
from types import SimpleNamespace

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


def test_main_injects_enabled_read_runtime_and_closes_it_after_assistant(monkeypatch):
    from contextlib import contextmanager
    from dataclasses import replace

    import automation.read_runtime as runtime
    from automation.settings import AutomationSettings, ServerSettings

    assistant = FakeAssistant()
    controller = object()
    lifecycle = []

    @contextmanager
    def factory(*args, **kwargs):
        lifecycle.append("runtime_open")
        yield controller
        assert assistant.exited
        lifecycle.append("runtime_closed")

    def assistant_factory(**kwargs):
        assert kwargs == {"automation_controller": controller}
        return assistant

    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(entrypoint, "VoiceAssistant", assistant_factory)
    monkeypatch.setattr(runtime, "read_only_runtime", factory)
    # The injected factory handles configuration; no real credentials/devices.
    enabled = AutomationSettings(
        enabled=True,
        servers=(
            ServerSettings(
                "homeassistant", "http://127.0.0.1:8123/api/mcp", "TOKEN", ("GetDateTime",), ()
            ),
        ),
    )
    monkeypatch.setattr(
        entrypoint.config, "SETTINGS", replace(entrypoint.config.SETTINGS, automation=enabled)
    )
    assert entrypoint.main() == 0
    assert lifecycle == ["runtime_open", "runtime_closed"]
    assert assistant.exited is True


def test_startup_identity_is_content_free_and_missing_route_is_visible(monkeypatch, caplog):
    assistant = FakeAssistant()
    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(entrypoint, "VoiceAssistant", lambda: assistant)
    monkeypatch.setattr(entrypoint, "read_identity", lambda root: ("a" * 40, True))
    monkeypatch.setenv("HELIOS_LLM_REMOTE_ENABLED", "true")
    monkeypatch.delenv("HELIOS_LLM_CONFIG", raising=False)
    with caplog.at_level("INFO"):
        assert entrypoint.main() == 0
    assert "event=helios_run_identity" in caplog.text
    assert "commit=" + "a" * 40 in caplog.text
    assert "event=remote_routing_requested_without_config" in caplog.text


def test_application_log_rotation_is_size_bounded(tmp_path, monkeypatch) -> None:
    configured = {}
    monkeypatch.setattr(entrypoint, "_LOG_MAX_BYTES", 128)
    monkeypatch.setattr(entrypoint, "_LOG_BACKUP_COUNT", 2)
    monkeypatch.setattr(
        entrypoint.logging, "basicConfig", lambda **kwargs: configured.update(kwargs)
    )
    entrypoint.configure_logging(
        SimpleNamespace(
            log_file=tmp_path / "app.log", log_level=logging.INFO, log_format="%(message)s"
        )
    )
    handler = configured["handlers"][0]
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for index in range(30):
            handler.emit(
                logging.LogRecord("helios", logging.INFO, __file__, 1, f"message-{index}", (), None)
            )
    finally:
        handler.close()
    files = list(tmp_path.glob("app.log*"))
    assert len(files) == 3
    assert all(path.stat().st_size <= 128 for path in files)


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
