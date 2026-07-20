# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for invoking rust-analyzer consistently."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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


def rust_analyzer_lsp_command(*args: str) -> list[str]:
    """Use a fixed analyzer binary without overriding the project toolchain.

    ``rustup run`` exports ``RUSTUP_TOOLCHAIN`` to the analyzer and its Cargo
    subprocesses. That bypasses a repository's ``rust-toolchain.toml`` and can
    make build scripts or proc macros unusable. Resolve the selected analyzer
    component to an absolute path instead; the LSP process can then let rustup
    choose the repository's own Cargo toolchain.
    """

    toolchain = rust_toolchain()
    rustup = shutil.which("rustup")
    if toolchain and rustup:
        try:
            result = subprocess.run(
                [rustup, "which", "--toolchain", toolchain, "rust-analyzer"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            binary = Path(result.stdout.strip())
            if binary.is_file():
                return [str(binary), *args]
        except (OSError, subprocess.SubprocessError):
            pass
    resolved = shutil.which("rust-analyzer")
    return [resolved or "rust-analyzer", *args]
