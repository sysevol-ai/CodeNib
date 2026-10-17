# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Bounded, source-derived function navigation for the ContextBench experiment."""

from __future__ import annotations

import hashlib
import json
import re

import tiktoken

from codenib.agent.runner import _MAX_RESULT_CHARS
from codenib.code_chunker import CodeChunker
from codenib.languages import extension_to_language_map
from codenib.types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from scripts.benchmark_context_agent import observed_nodes

MARKER = "\n[CodeNib file symbols: navigation metadata; read source to verify]\n"
DESCRIPTION = (
    "The first successful read of a file may append a compact function map: "
    "names and 1-based source ranges, without function bodies. Use these "
    "addresses to inspect relevant unread functions in that file. The map is "
    "navigation metadata; read actual source before relying on its behavior."
)


def file_symbols(repo, file):
    """Return physical callable addresses, rejecting oversized/ambiguous ranges.

    This is a parser-derived map, not compiler references or runtime edges.
    In particular, macro-wrapped C++ headers can produce a callable covering a
    whole namespace. A short declaration prefix must actually name the callable,
    and ranges over 500 lines are omitted rather than advertised as precise.
    """
    root = repo.resolve()
    path = (root / file).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("File map escapes the source checkout")
    if path.stat().st_size > 2 * 1024**2:
        return None
    language = extension_to_language_map("chunker").get(path.suffix.lower())
    if not language:
        return None
    data = path.read_bytes()
    if len(data) > 2 * 1024**2:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    chunks = CodeChunker(
        language=language,
        chunk_depth=2,
        l2_level_exclusive=False,
        max_lines_per_chunk=None,
    ).chunk_file(str(path), relative_path=file)
    symbols, rejected = set(), 0
    for chunk in chunks:
        if chunk.chunk_type not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
            continue
        if not isinstance(chunk.node_id, str) or not chunk.node_id:
            rejected += 1
            continue
        start, end = chunk.start_line, chunk.end_line
        name = chunk.node_id.removeprefix(file + ":")
        leaf = name.removesuffix("()").rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        prefix = "\n".join(lines[start : min(end + 1, start + 8)])
        if (
            not 0 <= start <= end < len(lines)
            or end - start + 1 > 500
            or not leaf
            or not re.search(r"(?<!\w)" + re.escape(leaf) + r"(?!\w)", prefix)
        ):
            rejected += 1
            continue
        symbols.add((name, chunk.chunk_type, start + 1, end + 1))
    if path.read_bytes() != data:
        raise ValueError("Source changed during file-map construction")
    return {
        "file": file,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "columns": ["name", "kind", "start_line", "end_line"],
        "symbols": sorted(symbols, key=lambda item: (item[2], item[3], item[0])),
        "rejected_ranges": rejected,
    }


def mapped_read(execute, *, repo, token_budget, observed_messages):
    """Append a map to first reads using only previously returned read receipts.

    No mutable cache, lock or per-file claim exists. Concurrent first reads may
    conservatively repeat metadata absent from their observation snapshots; they
    cannot hide source or fabricate delivery. SDK events retain the exact map.
    """
    if not 1 <= token_budget <= 4096:
        raise ValueError("File-map token budget must be in [1, 4096]")
    encoder = tiktoken.get_encoding("cl100k_base")

    def read(**arguments):
        snapshot = tuple(observed_messages())
        original = execute(**arguments)
        if original.lstrip().startswith("Error") or len(original) >= _MAX_RESULT_CHARS:
            return original
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "current-read",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "current-read", "content": original},
        ]
        current = observed_nodes(messages, repo)
        if not current:
            return original
        file = current[0].file
        calls = {
            call["id"]: call["function"]
            for message in snapshot
            for call in message.get("tool_calls", [])
        }
        reads = [
            message
            for message in snapshot
            if message.get("role") != "tool"
            or calls.get(message.get("tool_call_id"), {}).get("name") == "read"
        ]
        if any(node.file == file for node in observed_nodes(reads, repo)):
            return original
        try:
            metadata = file_symbols(repo, file)
        except (OSError, ValueError, RuntimeError):
            return original
        if not metadata or not metadata["symbols"]:
            return original
        symbols = metadata["symbols"]
        metadata = {**metadata, "symbols": [], "omitted_symbols": len(symbols)}
        suffix = ""
        for i, symbol in enumerate(symbols):
            proposal = {
                **metadata,
                "symbols": [*metadata["symbols"], symbol],
                "omitted_symbols": len(symbols) - i - 1,
            }
            candidate = MARKER + json.dumps(proposal, ensure_ascii=False)
            if (
                len(original + candidate) > _MAX_RESULT_CHARS
                or len(encoder.encode(candidate, disallowed_special=())) > token_budget
            ):
                break
            metadata, suffix = proposal, candidate
        return original + suffix

    return read
