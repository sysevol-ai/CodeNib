# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Expose the real CodeNib explore operation to the source-only SDK experiment.

This adapter changes wire formatting, not retrieval: the existing MCP operation
performs dense retrieval, navigation, dependency expansion and verified reads.
Its projected source is numbered against authenticated bytes so the existing
ContextBench exporter can credit exactly the lines delivered to the model.
Index-only fallbacks and omitted or partial lines receive no source credit.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from codenib.agent.tools.spec import ToolInputSpec, ToolSpec
from codenib.mcp.explore_bounds import MAX_EXPLORE_PAYLOAD_BYTES
from codenib.mcp.tools.explore import explore_context_impl

_OMITTED = re.compile(r"^\.\.\. (\d+) source lines omitted \.\.\.$")
CONTEXT_GUIDANCE = """Use explore_context for repository questions that need
semantic search, symbol navigation or dependency evidence. Start with a concrete
question about the issue, then request more context only for a remaining gap.
Use the returned source and provider diagnostics; an unavailable backend is not
evidence. Ordinary read, grep and glob remain available for precise follow-up.
Do not request unrelated source just to increase the amount of context."""


def numbered_context(ctx, file: str, context: dict) -> str:
    """Number only exact, complete lines present in an authenticated projection."""

    if context.get("verified") is not True:
        return ""
    path = PurePosixPath(file)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Context source must stay repository-relative")
    start, end = context.get("start_line"), context.get("end_line")
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise ValueError("Context source needs a positive inclusive line range")
    source = ctx.read_source_bytes(file, max_bytes=64 * 1024 * 1024)
    lines = source.decode("utf-8", errors="replace").splitlines()
    if end > len(lines):
        raise ValueError("Context source range no longer matches its file")
    position = start
    rendered = []
    for line in str(context.get("content") or "").splitlines():
        # A literal source line resembling an omission marker is still source.
        if position <= end and line == lines[position - 1]:
            rendered.append(f"{position:6d} | {line}")
            position += 1
            continue
        omitted = _OMITTED.fullmatch(line)
        if omitted:
            position += int(omitted.group(1))
            if position > end + 1:
                raise ValueError("Context omission exceeds its source range")
            continue
        # The source projector may end in a partial line or truncation notice.
        # Neither authorizes copying the remainder from the checkout.
        break
    return "\n".join(rendered)


def make_context_tool(ctx) -> ToolSpec:
    """Attach loaded core indexes; never silently substitute the basic tools.

    The existing ServerContext view lock serializes provider access. The lock
    order is view lock then source-reader internals, as in existing view access;
    this adapter retains no cross-call state or additional lock. Source binding
    owns byte authentication, including rejection after checkout changes.
    """

    if ctx.bm25 is not None or getattr(ctx, "grep_jev", None) is not None:
        raise ValueError("This experiment must not enable BM25 or grep/Jev")
    if ctx.vector is None and ctx.symbol_graph is None:
        raise ValueError("CodeNib context requires a dense or symbol-graph index")
    if not ctx.source_verified:
        raise ValueError("CodeNib context requires authenticated source")

    def execute(
        query: str,
        symbols: list[str] | None = None,
        top_k: int = 8,
        budget: str = "balanced",
        direction: str = "both",
    ) -> str:
        with ctx._view_lock:
            result = explore_context_impl(
                ctx,
                query=query,
                symbols=symbols or (),
                top_k=top_k,
                budget=budget,
                direction=direction,
                include_dependencies=True,
                filter_test=False,
            )
            nodes = [
                {
                    "kind": "codenib_explore_receipt",
                    "adapter": "numbered-verified-projection-v1",
                    **{
                        key: result[key]
                        for key in (
                            "plan",
                            "source",
                            "summary",
                            "relationships",
                            "diagnostics",
                        )
                    },
                }
            ]
            for group in result["files"]:
                for context in group["contexts"]:
                    node = {**context, "file": group["file"]}
                    node["content"] = numbered_context(ctx, group["file"], context)
                    nodes.append(node)
            reply = json.dumps(nodes, ensure_ascii=False)
            if len(reply.encode("utf-8")) > MAX_EXPLORE_PAYLOAD_BYTES:
                raise ValueError("Numbered context exceeds the MCP response budget")
            return reply

    return ToolSpec(
        tool_id="explore_context",
        executor_fn=execute,
        description=(
            "CodeNib indexed repository context: semantic retrieval, symbol routes, "
            "dependency expansion and verified source for a concrete information gap."
        ),
        inputs=[
            ToolInputSpec("query", "str", description="Concrete missing evidence."),
            ToolInputSpec("symbols", "List[str]", required=False, default=[]),
            ToolInputSpec("top_k", "int", required=False, default=8),
            ToolInputSpec(
                "budget",
                "str",
                required=False,
                default="balanced",
                enum=["fast", "balanced", "thorough"],
            ),
            ToolInputSpec(
                "direction",
                "str",
                required=False,
                default="both",
                enum=["impact", "dependencies", "both"],
            ),
        ],
    )
