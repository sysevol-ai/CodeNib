# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List

if TYPE_CHECKING:
    from ....ops.transform import TransformContext


def create_executor(context: "TransformContext") -> Callable[..., Dict[str, Any]]:
    """Create a code-to-query transform executor bound to the given TransformContext."""

    def execute(nodes: List[Any], **kwargs: Any) -> Dict[str, Any]:
        if not nodes:
            raise ValueError("Code-to-query transform received no code nodes.")

        max_snippets = int(kwargs.get("max_snippets", context.max_snippets))
        max_chars = int(kwargs.get("max_chars", context.max_chars))
        include_metadata = bool(kwargs.get("include_metadata", True))
        joiner = kwargs.get("joiner", "\n\n")

        snippets: List[str] = []
        sources: List[Dict[str, Any]] = []

        for node_info in nodes[:max_snippets]:
            content = (node_info.content or "").strip()
            if not content:
                continue
            if len(content) > max_chars:
                content = content[: max_chars - 3] + "..."

            snippet = content
            if include_metadata:
                label_parts = [
                    node_info.file or node_info.node_name,
                    (
                        f"{(node_info.start_line or 0) + 1}"
                        if node_info.start_line is not None
                        else None
                    ),
                ]
                label = ":".join([part for part in label_parts if part])
                snippet = f"{label}\n{content}" if label else content

            snippets.append(snippet)
            sources.append(
                {
                    "file": node_info.file,
                    "node_name": node_info.node_name,
                    "start_line": node_info.start_line,
                    "end_line": node_info.end_line,
                    "score": node_info.score,
                }
            )

        if not snippets:
            raise ValueError("No usable snippets found for code-to-query transform.")

        rewritten = joiner.join(snippets)

        return {
            "query": rewritten,
            "sources": sources,
        }

    return execute
