# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import sys
from typing import Dict, Optional

from rich.console import Console
from rich.logging import RichHandler
from rich.style import Style

# Add custom logging level for SCIP debugging
SCIP_DEBUG = 5  # Lower than DEBUG (10)
logging.addLevelName(SCIP_DEBUG, "SCIP_DEBUG")

# Add custom color for SCIP_DEBUG to Rich's LEVELS mapping
try:
    from rich._log_render import LEVELS

    LEVELS["SCIP_DEBUG"] = Style(color="bright_cyan", bold=True)
except ImportError:
    pass


def scip_debug(self, message, *args, **kwargs):
    if self.isEnabledFor(SCIP_DEBUG):
        self._log(SCIP_DEBUG, message, args, **kwargs)


# Add the method to Logger class
logging.Logger.scip_debug = scip_debug


class LoggingManager:
    def __init__(self):
        self.loggers: Dict[str, logging.Logger] = {}
        self.current_log_dir: str = ""
        self.unified_log_filename: str = "total.log"
        # ``stdout`` is the legacy name for console output. Rich writes that
        # output to stderr so machine-readable stdout (for example MCP stdio)
        # remains protocol-safe.
        # Modes: 'stdout' (console only), 'file' (unified file only),
        # 'both' (console + unified file).
        self.mode: str = "stdout"
        self.scip_debug_enabled: bool = False  # Track if SCIP debug is enabled
        self.scip_loggers: set = set()  # Explicitly registered SCIP loggers
        self.rich_handler: RichHandler = RichHandler(
            console=Console(stderr=True),
            show_time=bool(os.environ.get("LOG_TIME", False)),
            show_path=bool(os.environ.get("LOG_PATH", False)),
        )
        self.console_log_level = logging.INFO
        self.rich_handler.setLevel(self.console_log_level)

    def set_console_log_level(self, level: str | int) -> int:
        """Set the minimum severity emitted by console handlers.

        Managed CodeNib loggers do not propagate and therefore cannot be
        configured through ``logging.basicConfig``. Keep their Rich handler
        aligned with the root console logger while leaving file handlers at
        their existing diagnostic level.
        """
        if isinstance(level, str):
            normalized = level.upper()
            numeric_level = (
                SCIP_DEBUG
                if normalized == "SCIP_DEBUG"
                else getattr(logging, normalized, None)
            )
            if not isinstance(numeric_level, int):
                raise ValueError(f"Invalid logging level: {level!r}")
        elif isinstance(level, int):
            numeric_level = level
        else:
            raise TypeError("Logging level must be a name or integer")

        if numeric_level == SCIP_DEBUG:
            self._set_scip_debug_enabled(True)
        else:
            self.console_log_level = numeric_level
            self._set_scip_debug_enabled(False)
        self.rich_handler.setLevel(numeric_level)

        root_logger = logging.getLogger()
        if not root_logger.handlers:
            logging.basicConfig(
                level=numeric_level,
                format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                stream=sys.stderr,
            )
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(numeric_level)
        root_logger.setLevel(
            min(
                (handler.level for handler in root_logger.handlers),
                default=numeric_level,
            )
        )
        return numeric_level

    def _set_scip_debug_enabled(self, enabled: bool) -> None:
        """Align registered SCIP loggers and diagnostic file handlers."""
        self.scip_debug_enabled = enabled
        logger_level = SCIP_DEBUG if enabled else logging.DEBUG
        file_level = SCIP_DEBUG if enabled else logging.DEBUG

        for logger_name in self.scip_loggers:
            logger = self.loggers.get(logger_name)
            if logger is not None:
                logger.setLevel(logger_level)

        for logger in self.loggers.values():
            for handler in logger.handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.setLevel(file_level)

    def get_logger(self, name: str) -> logging.Logger:
        if name in self.loggers:
            return self.loggers[name]

        logger = logging.getLogger(name)
        # Set logger level based on SCIP debug settings
        if self.scip_debug_enabled and name in self.scip_loggers:
            logger.setLevel(SCIP_DEBUG)
        else:
            logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # Store the logger in our dictionary first
        self.loggers[name] = logger

        # Add appropriate handlers based on current mode
        if self.mode in ("stdout", "both"):
            logger.addHandler(self.rich_handler)
        if (
            self.mode in ("file", "both")
            and self.current_log_dir
            and os.path.isdir(self.current_log_dir)
        ):
            # Add unified log file handler (no per-logger files)
            formatter = logging.Formatter(
                "[%(asctime)s - %(name)s - %(levelname)s] %(message)s"
            )
            unified_log_file = os.path.join(
                self.current_log_dir, self.unified_log_filename
            )
            unified_file_handler = logging.FileHandler(unified_log_file)
            unified_file_handler.setLevel(logging.DEBUG)
            unified_file_handler.setFormatter(formatter)
            logger.addHandler(unified_file_handler)

        return logger

    def set_log_dir(self, new_dir: str) -> None:
        if self.current_log_dir == new_dir:
            return
        self.current_log_dir = new_dir

        # Ensure the new directory exists
        os.makedirs(self.current_log_dir, exist_ok=True)

        if self.mode in ("file", "both"):
            self._update_handlers()

    def set_unified_log_filename(self, filename: str) -> None:
        """Set the filename for the unified log file."""
        self.unified_log_filename = filename
        if self.mode in ("file", "both"):
            self._update_handlers()

    def switch_to_file(self) -> None:
        if self.mode == "file":
            return
        self.mode = "file"
        if self.current_log_dir:
            self._update_handlers()

    def switch_to_stdout(self) -> None:
        if self.mode == "stdout":
            return
        self.mode = "stdout"
        self._update_handlers()

    def switch_to_both(self) -> None:
        if self.mode == "both":
            return
        self.mode = "both"
        self._update_handlers()

    def _update_handlers(self) -> None:
        # Create unified file handler once if needed
        unified_file_handler = None
        if (
            self.mode in ("file", "both")
            and self.current_log_dir
            and os.path.isdir(self.current_log_dir)
        ):
            formatter = logging.Formatter(
                "[%(asctime)s - %(name)s - %(levelname)s] %(message)s"
            )
            unified_log_file = os.path.join(
                self.current_log_dir, self.unified_log_filename
            )
            if os.path.exists(unified_log_file):
                os.remove(unified_log_file)
            unified_file_handler = logging.FileHandler(unified_log_file)
            unified_file_handler.setLevel(logging.DEBUG)
            unified_file_handler.setFormatter(formatter)

        for _, logger in self.loggers.items():
            # Remove existing handlers
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)

            if self.mode in ("stdout", "both"):
                logger.addHandler(self.rich_handler)
            if self.mode in ("file", "both") and unified_file_handler:
                logger.addHandler(unified_file_handler)


# Global LoggingManager instance
logging_manager = LoggingManager()


# Convenience functions to match the original API
def get_logger(name: str) -> logging.Logger:
    return logging_manager.get_logger(name)


def set_console_log_level(level: str | int) -> int:
    return logging_manager.set_console_log_level(level)


def set_log_dir(new_dir: str) -> None:
    logging_manager.set_log_dir(new_dir)


def set_unified_log_filename(filename: str) -> None:
    logging_manager.set_unified_log_filename(filename)


def switch_log_to_file() -> None:
    logging_manager.switch_to_file()


def switch_log_to_stdout() -> None:
    logging_manager.switch_to_stdout()


def switch_log_to_both() -> None:
    logging_manager.switch_to_both()


def setup_detailed_logging(
    log_dir: str,
    run_name: str = None,
    level: str = "DEBUG",
    console_output: bool = False,
    mode: Optional[str] = None,
) -> str:
    """
    Set up detailed logging using the new LoggingManager.
    """
    import datetime

    # Generate run name if not provided
    if run_name is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"run_{timestamp}"

    # Set the log directory
    set_log_dir(log_dir)

    # Set the unified log filename to match the run name
    unified_log_filename = f"{run_name}.log"
    set_unified_log_filename(unified_log_filename)

    # Set default logging level for the logging manager
    if level.upper() == "SCIP_DEBUG":
        numeric_level = SCIP_DEBUG
    else:
        numeric_level = getattr(logging, level.upper(), logging.DEBUG)
    if numeric_level != SCIP_DEBUG:
        logging_manager.console_log_level = numeric_level
    logging_manager.rich_handler.setLevel(numeric_level)

    # Enable SCIP debug logging if requested
    if level.upper() == "SCIP_DEBUG":
        enable_scip_debug()

    # Select output mode
    valid_modes = {None, "stdout", "file", "both"}
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode {mode!r}. Expected one of: stdout, file, both")

    # Backward-compatible behavior if mode not provided
    if mode is None:
        if console_output:
            switch_log_to_stdout()
        else:
            switch_log_to_file()
    else:
        if mode == "stdout":
            switch_log_to_stdout()
        elif mode == "file":
            switch_log_to_file()
        elif mode == "both":
            switch_log_to_both()

    main_log_file = os.path.join(log_dir, unified_log_filename)
    return main_log_file


def register_scip_logger(logger_name: str) -> None:
    """Register a logger to receive SCIP debug level when enabled.

    Args:
        logger_name: Name of the logger to register for SCIP debugging
    """
    logging_manager.scip_loggers.add(logger_name)

    # If SCIP debug is already enabled, update this logger immediately
    if logging_manager.scip_debug_enabled and logger_name in logging_manager.loggers:
        logger = logging_manager.loggers[logger_name]
        logger.setLevel(SCIP_DEBUG)


def enable_scip_debug() -> None:
    """Enable SCIP debug logging for all registered SCIP loggers."""
    logging_manager._set_scip_debug_enabled(True)
    logging_manager.rich_handler.setLevel(SCIP_DEBUG)


def disable_scip_debug() -> None:
    """Disable SCIP debug logging for all registered SCIP loggers."""
    logging_manager._set_scip_debug_enabled(False)
    logging_manager.rich_handler.setLevel(logging_manager.console_log_level)
