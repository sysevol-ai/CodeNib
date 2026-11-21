# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Progress lines for a walkthrough build: stderr, and a log file with --keep."""

from __future__ import annotations

import sys
from typing import TextIO

_sinks: list[TextIO] = []


def say(*parts: object) -> None:
    """Print one progress line to stderr and to any log file added."""

    text = " ".join(str(p) for p in parts)
    print(text, file=sys.stderr, flush=True)
    for sink in _sinks:
        print(text, file=sink, flush=True)


def add_log(sink: TextIO) -> None:
    _sinks.append(sink)


def remove_log(sink: TextIO) -> None:
    if sink in _sinks:
        _sinks.remove(sink)
