# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for invoking rust-analyzer consistently."""

from __future__ import annotations

import os
import shutil

from ..toolchains import RUST_TOOLCHAIN

DEFAULT_RUST_TOOLCHAIN = RUST_TOOLCHAIN
RUST_ANALYZER_ENV = "CODENIB_RUST_ANALYZER"


def rust_toolchain() -> str:
    """Return the rustup toolchain used for rust-analyzer."""
    return os.environ.get("CODENIB_RUST_TOOLCHAIN") or DEFAULT_RUST_TOOLCHAIN


def rust_analyzer_command(*args: str) -> list[str]:
    """Build a rust-analyzer command.

    An explicit executable is useful when a repository triggers a bug in the
    managed release and an operator needs to validate a newer rust-analyzer
    without replacing the project-wide pin.

    Prefer ``rustup run`` so PATH entries for standalone rust-analyzer binaries
    cannot bypass the requested toolchain.
    """
    executable = os.environ.get(RUST_ANALYZER_ENV)
    if executable:
        return [os.path.expanduser(executable), *args]
    toolchain = rust_toolchain()
    if toolchain and shutil.which("rustup"):
        return ["rustup", "run", toolchain, "rust-analyzer", *args]
    return ["rust-analyzer", *args]
