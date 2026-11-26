# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Explain why a tree-sitter grammar could not be loaded.

``tree-sitter-language-pack`` downloads each grammar as a native library for
the machine's chip. An x86_64 Python running under Rosetta on Apple silicon
then cannot load the arm64 library, and the pack reports only that the
language was "not found". This module turns that into a message that says
what is wrong and what to do.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

# Mach-O CPU types (mach/machine.h).
_CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}
_MACHO_64 = b"\xcf\xfa\xed\xfe"  # MH_MAGIC_64, little-endian


class ParserUnavailableError(RuntimeError):
    """A language's tree-sitter grammar could not be loaded."""


def library_arch(path: Path) -> Optional[str]:
    """The architecture of a thin 64-bit Mach-O library, if it is one."""

    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return None
    if len(header) < 8 or header[:4] != _MACHO_64:
        return None
    return _CPU_TYPES.get(int.from_bytes(header[4:8], "little"))


def running_under_rosetta() -> bool:
    """True when this process is x86_64 code translated on Apple silicon."""

    if sys.platform != "darwin":
        return False
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip() == "1"


def _grammar_library(language: str) -> Optional[Path]:
    try:
        from tree_sitter_language_pack import cache_dir
    except ImportError:
        return None
    try:
        folder = Path(cache_dir())
    except Exception:  # noqa: BLE001 - the pack's cache lookup is best effort
        return None
    for suffix in (".dylib", ".so"):
        candidate = folder / f"libtree_sitter_{language}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def explain_parser_failure(
    language: str,
    error: BaseException,
    *,
    machine: Optional[str] = None,
    rosetta: Optional[Callable[[], bool]] = None,
    library: Optional[Path] = None,
) -> str:
    """A user-facing reason the ``language`` grammar did not load."""

    machine = machine or platform.machine()
    translated = (rosetta or running_under_rosetta)()
    library = library if library is not None else _grammar_library(language)
    built_for = library_arch(library) if library is not None else None
    python = sys.executable or "python"

    if translated or (built_for and built_for != machine):
        reason = (
            f"CodeNib could not load the {language} parser: this Python "
            f"({python}) runs as {machine}"
        )
        if translated:
            reason += " under Rosetta on Apple silicon"
        if built_for:
            reason += f", but the downloaded grammar is built for {built_for}"
        return (
            reason + ". Install CodeNib into an arm64 Python, for example "
            "`uv venv --python 3.12 && uv pip install codenib`, or run "
            "`arch -arm64 python3 -m venv .venv` with a native Python."
        )
    return (
        f"CodeNib could not load the {language} parser ({error}). "
        "Check your network connection: tree-sitter-language-pack downloads "
        "each grammar the first time it is used."
    )


__all__ = [
    "ParserUnavailableError",
    "explain_parser_failure",
    "library_arch",
    "running_under_rosetta",
]
