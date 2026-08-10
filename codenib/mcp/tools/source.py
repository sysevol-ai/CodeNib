# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded source reads for MCP clients."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from ..context import ServerContext
from ._validation import (
    MAX_LSP_POSITION,
    MAX_SOURCE_CONTENT_CHARS,
    MAX_SOURCE_PATH_CHARS,
    MAX_SOURCE_WINDOW_LINES,
    bounded_integer,
    required_text,
)

_MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024


def _repository_relative_source(ctx: ServerContext, value: str) -> str:
    path_text = required_text(value, name="file_path")
    if len(path_text) > MAX_SOURCE_PATH_CHARS:
        raise ValueError(
            f"file_path must not exceed {MAX_SOURCE_PATH_CHARS} characters."
        )
    if "\\" in path_text or "\x00" in path_text:
        raise ValueError("file_path must be a repository-relative POSIX path.")
    path = PurePosixPath(path_text)
    if (
        path.is_absolute()
        or path.as_posix() != path_text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("file_path must be a repository-relative POSIX path.")

    return path_text


def _bounded_source_content(payload: dict[str, Any]) -> dict[str, Any]:
    content = str(payload.get("content") or "")
    original_chars = len(content)
    if original_chars <= MAX_SOURCE_CONTENT_CHARS:
        payload["content_projection"] = {
            "truncated": False,
            "original_chars": original_chars,
            "returned_chars": original_chars,
            "strategy": "complete",
        }
        return payload

    lines = content.splitlines(keepends=True)
    selected: list[str] = []
    returned_chars = 0
    line_truncated = False
    for line in lines:
        available = MAX_SOURCE_CONTENT_CHARS - returned_chars
        if available <= 0:
            break
        if len(line) > available:
            if not selected:
                selected.append(line[:available])
                returned_chars += available
                line_truncated = True
            break
        selected.append(line)
        returned_chars += len(line)

    payload["content"] = "".join(selected)
    start_line = int(payload["start_line"])
    complete_lines = len(selected) - int(line_truncated)
    if selected:
        payload["end_line"] = start_line + len(selected) - 1
    projection = {
        "truncated": True,
        "original_chars": original_chars,
        "returned_chars": returned_chars,
        "strategy": "sequential_window",
    }
    if line_truncated:
        projection["line_truncated"] = True
    else:
        projection["next_start_line"] = start_line + complete_lines
    payload["content_projection"] = projection
    return payload


def read_source_impl(
    ctx: ServerContext,
    file_path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Read one content-authenticated repository-relative source window."""

    if getattr(ctx, "source_verified", False) is not True:
        detail = getattr(ctx, "source_error", None) or "source binding is unverified"
        raise RuntimeError(f"source reads are unavailable: {detail}")

    relative = _repository_relative_source(ctx, file_path)
    first = bounded_integer(
        start_line,
        name="start_line",
        maximum=MAX_LSP_POSITION,
    )
    if end_line is None:
        last = min(MAX_LSP_POSITION, first + MAX_SOURCE_WINDOW_LINES - 1)
    else:
        last = bounded_integer(
            end_line,
            name="end_line",
            maximum=MAX_LSP_POSITION,
        )
    if last < first:
        raise ValueError("end_line must be greater than or equal to start_line.")
    if last - first + 1 > MAX_SOURCE_WINDOW_LINES:
        raise ValueError(
            f"source windows may contain at most {MAX_SOURCE_WINDOW_LINES} lines."
        )

    try:
        source_bytes = ctx.read_source_bytes(
            relative,
            max_bytes=_MAX_SOURCE_FILE_BYTES,
        )
    except ValueError as exc:
        raise ValueError(
            "file_path is not a readable regular repository file."
        ) from exc
    all_lines = source_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
    selected = all_lines[first - 1 : last]
    payload = {
        "file": relative,
        "start_line": first,
        "end_line": first + len(selected) - 1,
        "content": "".join(selected),
    }
    if int(payload["end_line"]) < first:
        raise ValueError("start_line exceeds the source file length.")
    payload = _bounded_source_content(payload)
    artifact = ctx.artifact if isinstance(ctx.artifact, Mapping) else {}
    payload["source"] = {
        "repository": artifact.get("repository") or ctx.manifest.repo_path,
        "commit": ctx.manifest.commit,
        "source_fingerprint": ctx.manifest.source_fingerprint,
        "verified": True,
        "verification_scope": "content-bytes",
        "commit_verified": False,
        "checkout_state": "not-attested",
    }
    return payload


__all__ = ["read_source_impl"]
