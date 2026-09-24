"""Executable entry point for the Helios voice assistant."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import threading
from pathlib import Path

import config
from assistant import AssistantShutdownTimeout, VoiceAssistant
from runtime_identity import read_identity

_INTERRUPTED_EXIT_CODE = 128 + 2
_FORCED_EXIT_NOTICE = b"Graceful shutdown deadline exceeded; forcing Helios to exit.\n"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


def _force_exit_after_repeated_interrupt() -> None:
    """Exit without Python teardown when graceful shutdown is interrupted again."""

    try:
        # Avoid logging locks here: another runtime thread may have been
        # interrupted while it owned one.  ``os.write`` also reaches stderr
        # when the Jetson launcher is piped through ``tee``.
        os.write(2, _FORCED_EXIT_NOTICE)
    except OSError:
        pass
    os._exit(_INTERRUPTED_EXIT_CODE)


class _TwoStageInterruptHandler:
    """First SIGINT raises normally; every later SIGINT exits immediately."""

    def __init__(self) -> None:
        self._count = 0

    def __call__(self, _signum: int, _frame: object) -> None:
        self._count += 1
        if self._count >= 2:
            _force_exit_after_repeated_interrupt()
            return
        raise KeyboardInterrupt


def configure_logging(settings: config.Settings = config.SETTINGS) -> None:
    handlers: list[logging.Handler]
    if settings.log_file:
        Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers = [
            RotatingFileHandler(
                settings.log_file,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        ]
    else:
        handlers = [logging.StreamHandler()]

    logging.basicConfig(
        level=settings.log_level,
        format=settings.log_format,
        handlers=handlers,
        force=True,
    )


def main() -> int:
    configure_logging()
    settings = config.SETTINGS
    identity = read_identity(settings.project_root)
    logging.getLogger(__name__).info(
        "event=helios_run_identity tree=%s commit=%s dirty=%s routing_config=%s "
        "kpi_enabled=%s kpi_store=%s log_destination=%s",
        settings.project_root.resolve(),
        identity[0] if identity else "unknown",
        identity[1] if identity else "unknown",
        settings.llm.routing_file.resolve() if settings.llm.routing_file else "none-local_only",
        settings.kpi.enabled,
        settings.kpi.storage_path.resolve(),
        settings.log_file.resolve() if settings.log_file else "stderr",
    )
    if identity is None:
        logging.getLogger(__name__).warning("event=helios_version_unknown")
    if (
        os.getenv("HELIOS_LLM_REMOTE_ENABLED", "").strip().lower() in {"1", "true", "yes"}
        and not os.getenv("HELIOS_LLM_CONFIG", "").strip()
    ):
        logging.getLogger(__name__).warning(
            "event=remote_routing_requested_without_config using=local_only"
        )
    previous_handler: object | None = None
    installed_handler = False
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _TwoStageInterruptHandler())
        installed_handler = True
    try:
        with VoiceAssistant() as assistant:
            try:
                assistant.run()
            except (KeyboardInterrupt, AssistantShutdownTimeout):
                # A worker that survives its deadline cannot be joined safely
                # by CPython's ThreadPoolExecutor atexit hook. A repeated signal
                # is handled immediately by the scoped handler above.
                _force_exit_after_repeated_interrupt()
                return _INTERRUPTED_EXIT_CODE
        return 0
    finally:
        if installed_handler:
            assert previous_handler is not None
            signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
