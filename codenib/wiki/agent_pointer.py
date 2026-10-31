# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Explicit, idempotent discovery pointers to an exported CodeNib Wiki."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote

_BEGIN = "<!-- BEGIN CODENIB WIKI -->"
_END = "<!-- END CODENIB WIKI -->"


def _with_pointer(content: str, link: str) -> str:
    block = (
        f"{_BEGIN}\n"
        f"Repository context: [CodeNib Wiki]({link}).\n"
        "Read its index and relevant chapters for orientation, then check the "
        "captured source evidence. For current code, use the existing CodeNib "
        "MCP tools when configured, or inspect the repository.\n"
        "The bundle describes its recorded source snapshot; it may differ from "
        "the current checkout.\n"
        f"{_END}"
    )
    if content.count(_BEGIN) != content.count(_END) or content.count(_BEGIN) > 1:
        raise ValueError("malformed CodeNib Wiki management block")
    if _BEGIN in content:
        first, last = content.index(_BEGIN), content.index(_END)
        if last < first:
            raise ValueError("malformed CodeNib Wiki management block")
        return content[:first] + block + content[last + len(_END) :]
    return (
        content
        + ("\n\n" if content and not content.endswith("\n\n") else "")
        + block
        + "\n"
    )


def write_agent_pointers(bundle: Path, directory: Path) -> list[Path]:
    """Create AGENTS.md and update CLAUDE.md when present, preserving other text.

    All target blocks are validated before writing. Each file replacement
    linearizes at os.replace; the pair is deliberately not a transaction.
    No lock or recovery state outlives the call: if a later replacement fails,
    the caller can rerun this idempotent operation. Concurrent changes detected
    before replacement are refused; this is an operator edit, not a writer lock.
    """

    bundle, directory = (
        bundle.expanduser().absolute(),
        directory.expanduser().absolute(),
    )
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("agent directory must be an existing regular directory")
    resolved_bundle, resolved_directory = bundle.resolve(), directory.resolve()
    if (
        resolved_directory == resolved_bundle
        or resolved_bundle in resolved_directory.parents
    ):
        raise ValueError("agent instructions must be outside the OKF bundle")
    if (
        not (bundle / "codenib-okf.json").is_file()
        or not (bundle / "index.md").is_file()
    ):
        raise ValueError("agent pointer requires a completed CodeNib OKF bundle")
    link = quote(
        Path(os.path.relpath(bundle / "index.md", directory)).as_posix(), safe="/.-_"
    )
    plans = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = directory / name
        if path.is_symlink():
            raise ValueError(f"agent instructions must not be a symbolic link: {name}")
        if name == "CLAUDE.md" and not path.exists():
            continue
        original = path.read_bytes() if path.exists() else None
        updated = _with_pointer((original or b"").decode("utf-8"), link).encode("utf-8")
        if original != updated:
            plans.append((path, original, updated))
    changed = []
    for path, original, updated in plans:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(updated)
                stream.flush()
            os.chmod(
                temporary,
                stat.S_IMODE(path.stat().st_mode) if original is not None else 0o644,
            )
            if (
                path.is_symlink()
                or (path.read_bytes() if path.exists() else None) != original
            ):
                raise ValueError(
                    f"agent instructions changed during update: {path.name}"
                )
            os.replace(temporary, path)
            changed.append(path)
        finally:
            temporary.unlink(missing_ok=True)
    return changed
