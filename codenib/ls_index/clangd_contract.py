# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared clangd generation and source-position contract."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

CLANGD_DEFAULT_POSITION_ENCODING = "UTF16"
CLANGD_SUPPORTED_POSITION_ENCODINGS = frozenset({"UTF8", "UTF16", "UTF32"})
CLANGD_POSITION_ENCODING_ENV = "CODENIB_CLANGD_POSITION_ENCODING"


def normalize_clangd_position_encoding(value: str) -> str:
    """Normalize an LSP position encoding to the native contract spelling."""

    compact = "".join(
        character for character in str(value).upper() if character.isalnum()
    )
    if compact not in CLANGD_SUPPORTED_POSITION_ENCODINGS:
        supported = ", ".join(sorted(CLANGD_SUPPORTED_POSITION_ENCODINGS))
        raise ValueError(
            f"clangd position encoding must be one of {supported}; got {value!r}"
        )
    return compact


def clangd_position_encoding_flag(position_encoding: str) -> str:
    """Return clangd's CLI spelling for a normalized position encoding."""

    normalized = normalize_clangd_position_encoding(position_encoding)
    return f"--offset-encoding={normalized[:3].lower()}-{normalized[3:]}"


def configured_clangd_position_encoding(value: Optional[str] = None) -> str:
    """Return the shared generation/decode encoding, including its override."""

    raw = (
        os.environ.get(CLANGD_POSITION_ENCODING_ENV, CLANGD_DEFAULT_POSITION_ENCODING)
        if value is None
        else value
    )
    return normalize_clangd_position_encoding(str(raw))


def clangd_background_index_command(
    executable: str,
    compile_commands_dir: str | Path,
    *,
    position_encoding: Optional[str] = None,
) -> Sequence[str]:
    """Build the common full/incremental background-index command."""

    encoding = configured_clangd_position_encoding(position_encoding)
    return [
        executable,
        "--background-index",
        f"--compile-commands-dir={compile_commands_dir}",
        "--background-index-priority=normal",
        clangd_position_encoding_flag(encoding),
        "--log=error",
    ]
