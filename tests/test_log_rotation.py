from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import main
import pytest


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    # Keep basicConfig(force=True) from closing pytest's or other callers' handlers.
    for handler in original_handlers:
        root.removeHandler(handler)
    try:
        yield root
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_log_file_configures_one_rotating_handler(tmp_path, root_logger) -> None:
    log_file = tmp_path / "app.log"
    main.configure_logging(
        SimpleNamespace(log_file=log_file, log_level=logging.INFO, log_format="%(message)s")
    )

    assert len(root_logger.handlers) == 1
    handler = root_logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.baseFilename == str(log_file.resolve())
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    assert handler.encoding == "utf-8"


def test_writing_past_limit_rotates_log_file(tmp_path, monkeypatch, root_logger) -> None:
    log_file = tmp_path / "app.log"
    monkeypatch.setattr(main, "_LOG_MAX_BYTES", 32)
    main.configure_logging(
        SimpleNamespace(log_file=log_file, log_level=logging.INFO, log_format="%(message)s")
    )

    root_logger.info("primo messaggio: caffè")
    root_logger.info("secondo messaggio: perché")
    for handler in root_logger.handlers:
        handler.flush()

    assert (tmp_path / "app.log.1").read_bytes() == "primo messaggio: caffè\n".encode("utf-8")
    assert log_file.read_bytes() == "secondo messaggio: perché\n".encode("utf-8")


def test_no_log_file_configures_stream_handler(tmp_path, monkeypatch, root_logger, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    main.configure_logging(
        SimpleNamespace(log_file=None, log_level=logging.INFO, log_format="%(message)s")
    )

    assert len(root_logger.handlers) == 1
    assert type(root_logger.handlers[0]) is logging.StreamHandler
    root_logger.info("terminal message")
    assert capsys.readouterr().err == "terminal message\n"
    assert list(tmp_path.iterdir()) == []


def test_reconfiguring_replaces_root_handlers(tmp_path, root_logger) -> None:
    first_file = tmp_path / "first.log"
    second_file = tmp_path / "second.log"
    main.configure_logging(
        SimpleNamespace(log_file=first_file, log_level=logging.INFO, log_format="%(message)s")
    )
    first_handler = root_logger.handlers[0]

    main.configure_logging(
        SimpleNamespace(log_file=second_file, log_level=logging.INFO, log_format="%(message)s")
    )

    assert len(root_logger.handlers) == 1
    handler = root_logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler is not first_handler
    assert handler.baseFilename == str(second_file.resolve())
    root_logger.info("only in the second file")
    handler.flush()
    assert first_file.read_text(encoding="utf-8") == ""
    assert second_file.read_text(encoding="utf-8") == "only in the second file\n"
