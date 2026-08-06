# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from codenib.web.repository_files import live_source_slice


def test_live_source_slice_bounds_explicit_line_ranges(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "".join(f"line_{line}\n" for line in range(1, 2_001)),
        encoding="utf-8",
    )

    result = live_source_slice(tmp_path, "large.py", start=1, end=2_000)

    assert result is not None
    assert result["start_line"] == 1
    assert result["end_line"] == 1_000
    assert len(result["content"].splitlines()) == 1_000


def test_live_source_slice_applies_default_window_from_start(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "".join(f"line_{line}\n" for line in range(1, 1_001)),
        encoding="utf-8",
    )

    result = live_source_slice(tmp_path, "large.py", start=501)

    assert result is not None
    assert result["start_line"] == 501
    assert result["end_line"] == 900
    assert result["content"].startswith("line_501\n")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_live_source_slice_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo)

    started = time.monotonic()
    result = live_source_slice(tmp_path, "source.fifo")

    assert result is None
    assert time.monotonic() - started < 1
