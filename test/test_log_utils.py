# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import logging

import pytest

from codenib import log_utils
from codenib.log_utils import LoggingManager


def test_console_logs_use_stderr() -> None:
    manager = LoggingManager()

    assert manager.rich_handler.console.stderr is True
    assert manager.rich_handler.level == logging.INFO


def test_console_log_level_updates_console_handlers_only(tmp_path) -> None:
    manager = LoggingManager()
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    original_handler_levels = {
        handler: handler.level for handler in root_logger.handlers
    }
    console_handler = logging.StreamHandler()
    file_handler = logging.FileHandler(tmp_path / "diagnostic.log")
    console_handler.setLevel(logging.DEBUG)
    file_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    try:
        assert manager.set_console_log_level("ERROR") == logging.ERROR
        assert manager.rich_handler.level == logging.ERROR
        assert manager.console_log_level == logging.ERROR
        assert root_logger.level == logging.ERROR
        assert console_handler.level == logging.ERROR
        assert file_handler.level == logging.DEBUG
    finally:
        root_logger.removeHandler(console_handler)
        root_logger.removeHandler(file_handler)
        file_handler.close()
        root_logger.setLevel(original_root_level)
        for handler, level in original_handler_levels.items():
            handler.setLevel(level)


def test_console_log_level_rejects_unknown_name() -> None:
    manager = LoggingManager()

    with pytest.raises(ValueError, match="Invalid logging level"):
        manager.set_console_log_level("verbose")


def test_disabling_scip_debug_restores_selected_console_level(monkeypatch) -> None:
    manager = LoggingManager()
    logger_name = "test.log_utils.scip_restore"
    logger = logging.getLogger(logger_name)
    original_level = logger.level
    original_handlers = list(logger.handlers)
    original_propagate = logger.propagate
    monkeypatch.setattr(log_utils, "logging_manager", manager)

    try:
        manager.console_log_level = logging.WARNING
        manager.rich_handler.setLevel(logging.WARNING)
        log_utils.register_scip_logger(logger_name)
        log_utils.enable_scip_debug()
        assert manager.rich_handler.level == log_utils.SCIP_DEBUG
        assert manager.get_logger(logger_name).level == log_utils.SCIP_DEBUG

        log_utils.disable_scip_debug()
        assert manager.rich_handler.level == logging.WARNING
        assert manager.get_logger(logger_name).level == logging.DEBUG
    finally:
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
