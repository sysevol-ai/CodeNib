# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Append small compiler-reference hints after actual source reads in the pilot.

Hints expose possible missing context, not source bodies or runtime coverage.
The existing view lock protects graph/source access; lock order remains view
then source reader. No mutable delivery ledger or additional lock is retained.
Delivered history suppresses repeats; concurrent reads may repeat metadata.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

import tiktoken

from codenib.agent.runner import _MAX_RESULT_CHARS
from codenib.graph.traverse_graph import NeighborScanBudget, RepoDependencySearcher
from codenib.types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    is_symbol_node,
    node_has_definition,
)
from codenib.utils import is_test_file
from scripts.benchmark_context_agent import observed_nodes
from scripts.contextbench_graph_navigation import graph_definitions

MARKER = "\n[CodeNib indexed references: metadata only]\n"
DESCRIPTION = (
    "A read may append bounded static reference counts and symbol IDs from the "
    "verified CodeNib graph. These are possible missing dependencies/tests, not "
    "runtime calls or test coverage. Request explore_context with a symbol ID "
    "only for a relevant unresolved gap; the hint contains no extra source."
)
KIND = "codenib_reference_hint_v1"


def _relative(file):
    return (
        isinstance(file, str)
        and bool(file)
        and "\\" not in file
        and "\x00" not in file
        and not PurePosixPath(file).is_absolute()
        and bool(PurePosixPath(file).parts)
        and PurePosixPath(file).as_posix() == file
        and ".." not in PurePosixPath(file).parts
    )


class ContextHints:
    def __init__(self, ctx, *, repo, token_budget=256, max_symbols=2, edge_limit=128):
        if not 64 <= token_budget <= 512 or not 1 <= max_symbols <= 2:
            raise ValueError("Invalid context-hint delivery budget")
        if not 4 <= edge_limit <= 512:
            raise ValueError("Invalid context-hint graph scan budget")
        if not ctx.source_verified or (ctx.vector is None and ctx.symbol_graph is None):
            raise ValueError("Reference hints require authenticated indexed context")
        self.ctx, self.repo = ctx, repo
        self.token_budget, self.max_symbols = token_budget, max_symbols
        self.per_direction = max(1, edge_limit // (2 * max_symbols))
        self.definitions = graph_definitions(ctx.symbol_graph)
        self.encoder = tiktoken.get_encoding("cl100k_base")

    def _history(self, messages):
        reads = {
            c["id"]
            for m in messages
            for c in m.get("tool_calls", [])
            if c["function"]["name"] == "read" and not c["function"].get("unavailable")
        }
        seen, unavailable = set(), False
        for m in messages:
            if (
                m.get("role") != "tool"
                or m.get("tool_call_id") not in reads
                or m.get("is_error")
            ):
                continue
            _, marker, tail = str(m.get("content", "")).partition(MARKER)
            if not marker:
                continue
            try:
                data = json.loads(tail)
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("kind") != KIND:
                continue
            if (
                data.get("source_fingerprint") != self.ctx.manifest.source_fingerprint
                or data.get("commit") != self.ctx.manifest.commit
            ):
                continue
            seen.update(s["id"] for s in data.get("symbols", []))
            unavailable |= data.get("graph") == "unavailable"
        return seen, unavailable

    def _references(self, name, direction):
        graph = self.ctx.symbol_graph
        budget = NeighborScanBudget(self.per_direction)
        names, files = set(), set()
        for neighbor, _edge in RepoDependencySearcher(graph).iter_neighbors(
            name,
            direction=direction,
            etype_filter={EDGE_TYPE_REFERENCE},
            ignore_test_file=False,
            scan_budget=budget,
        ):
            attrs = graph.get_node_info_by_name(neighbor) or {}
            file = attrs.get("file")
            if (
                neighbor == name
                or not is_symbol_node(attrs.get("type"))
                or not node_has_definition(attrs)
                or not _relative(file)
            ):
                continue
            names.add(neighbor)
            files.add(file)
        result = {
            "symbols_seen": len(names),
            "files_seen": len(files),
            "scan_complete": not budget.exhausted,
        }
        if direction == "backward":
            result["test_files_seen"] = sum(is_test_file(file) for file in files)
        return result

    def wrap(self, execute, *, observed_messages):
        def read(**arguments):
            history = tuple(observed_messages())
            original = execute(**arguments)
            if original.startswith("Error") or len(original) >= _MAX_RESULT_CHARS:
                return original
            try:
                current = observed_nodes(
                    [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "hint-read",
                                    "function": {
                                        "name": "read",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "hint-read",
                            "content": original,
                        },
                    ],
                    self.repo,
                )
                if not current:
                    return original
                with self.ctx._view_lock:
                    return original + self._suffix(current, history, len(original))
            except (OSError, RuntimeError, ValueError):
                # Metadata must not discard a successful read or authorize
                # graph facts after the indexed source has changed.
                return original

        return read

    def _suffix(self, current, history, original_chars):
        seen, unavailable = self._history(history)
        graph = self.ctx.symbol_graph
        file = current[0].file
        source = self.ctx.read_source_bytes(file, max_bytes=64 * 1024 * 1024)
        line_count = len(source.decode("utf-8", errors="replace").splitlines())
        payload = {
            "kind": KIND,
            "commit": self.ctx.manifest.commit,
            "source_fingerprint": self.ctx.manifest.source_fingerprint,
            "graph": "available" if graph is not None else "unavailable",
            "symbols": [],
        }
        if graph is None:
            if unavailable:
                return ""
            payload["available_followup"] = "explore_context semantic query"
        else:

            def overlap(node):
                return sum(
                    max(
                        0,
                        min(node["end_line"], n.end_line)
                        - max(node["start_line"], n.start_line)
                        + 1,
                    )
                    for n in current
                )

            candidates = [
                n
                for n in self.definitions.get(file, [])
                if n["name"] not in seen and n["end_line"] < line_count and overlap(n)
            ]
            candidates.sort(
                key=lambda n: (
                    n["type"] not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD},
                    -overlap(n),
                    n["end_line"] - n["start_line"],
                    n["start_line"],
                    n["name"],
                )
            )
            for node in candidates[: self.max_symbols]:
                symbol = {
                    "id": node["name"],
                    "at": [file, node["start_line"] + 1, node["end_line"] + 1],
                    "referrers": self._references(node["name"], "backward"),
                    "referenced": self._references(node["name"], "forward"),
                }
                proposed = {**payload, "symbols": [*payload["symbols"], symbol]}
                if self._fits(proposed, original_chars):
                    payload = proposed
            if not payload["symbols"]:
                return ""
        return (
            MARKER + json.dumps(payload, ensure_ascii=True)
            if self._fits(payload, original_chars)
            else ""
        )

    def _fits(self, data, original_chars):
        suffix = MARKER + json.dumps(data, ensure_ascii=True)
        return (
            original_chars + len(suffix) <= _MAX_RESULT_CHARS
            and len(self.encoder.encode(suffix, disallowed_special=()))
            <= self.token_budget
        )
