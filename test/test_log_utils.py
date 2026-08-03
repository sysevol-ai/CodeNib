# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.log_utils import LoggingManager


def test_console_logs_use_stderr() -> None:
    manager = LoggingManager()

    assert manager.rich_handler.console.stderr is True
