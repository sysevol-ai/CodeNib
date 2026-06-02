# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for invoking rust-analyzer consistently."""

from __future__ import annotations

import os
import shutil

DEFAULT_RUST_TOOLCHAIN = "nightly"


def rust_toolchain() -> str:
    """Return the rustup toolchain used for rust-analyzer."""
    return os.environ.get("CODEMINER_RUST_TOOLCHAIN") or DEFAULT_RUST_TOOLCHAIN


def rust_analyzer_command(*args: str) -> list[str]:
    """Build a rust-analyzer command.

    Prefer ``rustup run`` so PATH entries for standalone rust-analyzer binaries
    cannot bypass the requested toolchain.
    """
    toolchain = rust_toolchain()
    if toolchain and shutil.which("rustup"):
        return ["rustup", "run", toolchain, "rust-analyzer", *args]
    return ["rust-analyzer", *args]
