# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped surface/body delivery over existing CodeNib exploration.

This labs adapter keeps only a bounded map of issued locations. The existing
view lock serializes handle issuance and source reads; lock order stays view
then source reader. Handle identity includes the manifest commit/fingerprint.
There is no persistence, recovery owner, eviction or new archive protocol.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import tiktoken

from codenib.agent.tools.spec import ToolInputSpec, ToolSpec
from codenib.eval.benchmarks.contextbench import contextbench_prediction
from codenib.mcp.tools.explore import explore_context_impl
from codenib.mcp.tools.source import read_source_impl
from scripts.contextbench_codenib_context import make_context_tool, numbered_context

GUIDANCE = """Start repository investigation with explore_context(view='surface')
using your own focused question about the issue. CodeNib first returns locations,
declaration excerpts and stable handles, not source bodies. Select relevant handles
with view='body'; large bodies include an explicit continuation start line. Use
another surface query for a remaining information gap. Read/grep/glob remain
available for precise follow-up or missing index coverage. Surface metadata is
candidate evidence only: inspect actual source before relying on behavior. An
unavailable graph does not imply no references. Do not fetch unrelated bodies."""


@dataclass(frozen=True)
class Location:
    file: str
    start: int
    end: int
    symbol: str


class SurfaceContext:
    def __init__(self, ctx, *, observed_messages, token_budget=2048):
        # Retain the established experiment's backend/source admission checks.
        make_context_tool(ctx)
        self._initialize_source_session(ctx, observed_messages, token_budget)

    def _initialize_source_session(self, ctx, observed_messages, token_budget):
        if ctx.source_verified is not True:
            raise ValueError("Surface context requires authenticated repository source")
        if not 256 <= token_budget <= 4096:
            raise ValueError("Invalid surface token budget")
        self.ctx, self.observed_messages = ctx, observed_messages
        self.encoder = tiktoken.get_encoding("cl100k_base")
        self.token_budget = token_budget
        self.identity = (ctx.manifest.commit, ctx.manifest.source_fingerprint)
        self.locations = {}

    def _location_entry(self, location, declaration, origins):
        identity = json.dumps(
            [
                *self.identity,
                location.file,
                location.start,
                location.end,
                location.symbol,
            ],
            ensure_ascii=True,
        )
        return {
            "handle": "ctx:" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "file": location.file,
            "lines": [location.start, location.end],
            "symbol": location.symbol,
            "declaration_excerpt": declaration,
            "origins": origins,
        }

    def _check_identity(self):
        if self.identity != (
            self.ctx.manifest.commit,
            self.ctx.manifest.source_fingerprint,
        ):
            raise ValueError("Surface handles belong to another source snapshot")

    def execute(self, query="", view="surface", handle="", start_line=0):
        with self.ctx._view_lock:
            self._check_identity()
            if view == "surface":
                if handle or start_line:
                    raise ValueError("Surface queries do not accept a body cursor")
                return self._surface(query)
            if view == "body":
                if query:
                    raise ValueError("Body requests use a handle, not a new query")
                return self._body(handle, start_line)
            raise ValueError("view must be 'surface' or 'body'")

    def _surface(self, query):
        result = explore_context_impl(self.ctx, query=query, top_k=8, budget="fast")
        payload = {
            "kind": "codenib_surface_v1",
            "commit": self.identity[0],
            "source_fingerprint": self.identity[1],
            "graph_available": self.ctx.symbol_graph is not None,
            "entries": [],
            "diagnostics": [d["code"] for d in result["diagnostics"]],
            "more_candidates_omitted": False,
        }
        for group in result["files"]:
            for context in group["contexts"]:
                if context.get("verified") is not True:
                    continue
                for anchor in context["anchors"]:
                    location = Location(
                        group["file"],
                        anchor.get("start_line", context["start_line"]),
                        anchor.get("end_line", context["end_line"]),
                        anchor.get("symbol", ""),
                    )
                    # Authenticate the declaration independently of indexed text.
                    declaration = read_source_impl(
                        self.ctx, location.file, location.start, location.start
                    )["content"].strip()[:240]
                    entry = self._location_entry(
                        location, declaration, anchor["origins"]
                    )
                    handle = entry["handle"]
                    if any(x["handle"] == handle for x in payload["entries"]):
                        continue
                    candidate = {**payload, "entries": [*payload["entries"], entry]}
                    encoded = json.dumps([candidate], ensure_ascii=True)
                    if (
                        len(self.encoder.encode(encoded, disallowed_special=()))
                        > self.token_budget
                        or len(payload["entries"]) >= 8
                        or handle not in self.locations
                        and len(self.locations) >= 256
                    ):
                        payload["more_candidates_omitted"] = True
                        continue
                    if handle in self.locations and self.locations[handle] != location:
                        raise RuntimeError("Surface handle collision")
                    self.locations[handle] = location
                    payload = candidate
        return json.dumps([payload], ensure_ascii=True)

    def _body(self, handle, start_line):
        if not isinstance(handle, str) or handle not in self.locations:
            raise ValueError("Unknown handle; request a surface first")
        location = self.locations[handle]
        if type(start_line) is not int or start_line < 0:
            raise ValueError("Invalid body cursor")
        first = start_line or location.start
        if not location.start <= first <= location.end:
            raise ValueError("Body cursor lies outside the surfaced location")
        last = min(location.end, first + 199)
        source = read_source_impl(self.ctx, location.file, first, last)
        history = tuple(self.observed_messages())
        confirmed = [
            m
            for i, m in enumerate(history)
            if m.get("role") != "tool"
            or any(x.get("role") == "assistant" for x in history[i + 1 :])
        ]
        prediction = contextbench_prediction(
            instance_id="dedup",
            messages=confirmed,
            repo_path=self.ctx.manifest.repo_path,
        )
        seen = {
            n
            for r in prediction["traj_data"]["pred_spans"].get(location.file, [])
            for n in range(r["start"], r["end"] + 1)
        }
        numbered = numbered_context(
            self.ctx, location.file, {**source, "verified": True}
        )
        visible = [
            line
            for line in numbered.splitlines()
            if int(line.split("|", 1)[0]) not in seen
        ]
        previous_calls = [
            message["tool_call_id"]
            for message, step in zip(
                (m for m in confirmed if m.get("role") == "tool"),
                prediction["traj_data"]["pred_steps"],
                strict=True,
            )
            if any(
                r["start"] <= last and first <= r["end"]
                for r in step["spans"].get(location.file, [])
            )
        ]
        projection = source.get("content_projection") or {}
        next_line = projection.get("next_start_line", source["end_line"] + 1)
        if projection.get("line_truncated"):
            next_line = first + 1
        reply = {
            "kind": "codenib_body_v1",
            "handle": handle,
            "file": location.file,
            "source": source["source"],
            "content": "\n".join(visible),
            "previously_seen_lines_omitted": len(numbered.splitlines()) - len(visible),
            "previous_tool_calls": previous_calls[-8:],
            "next_start_line": next_line if next_line <= location.end else None,
            "partial_line_not_credited": bool(projection.get("line_truncated")),
            "unavailable_line": first if projection.get("line_truncated") else None,
        }
        return json.dumps([reply], ensure_ascii=True)

    def tool(self):
        return ToolSpec(
            tool_id="explore_context",
            executor_fn=self.execute,
            description=(
                "CodeNib on-demand context: query view=surface for declaration/location "
                "metadata, then view=body with a returned handle for authenticated "
                "source. Previously consumed lines are omitted; use read to revisit."
            ),
            inputs=[
                ToolInputSpec("query", "str", required=False, default=""),
                ToolInputSpec(
                    "view",
                    "str",
                    required=False,
                    default="surface",
                    enum=["surface", "body"],
                ),
                ToolInputSpec("handle", "str", required=False, default=""),
                ToolInputSpec("start_line", "int", required=False, default=0),
            ],
        )
