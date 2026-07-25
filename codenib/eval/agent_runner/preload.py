# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pre-load context assembly for agent-runner evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from codenib.eval.agent_runner.orchestrator import query_is_specific
from codenib.eval.retrieval_eval import dedup_spans, nodes_to_spans

# Keep snippets short so a candidate list of around ten entries stays compact.
_SNIPPET_LINES = 4
_DEFAULT_TOP_K = 10
_DEFAULT_LEVEL = "l2"


@dataclass(frozen=True)
class PreloadQueryPlan:
    """Prepared query and audit data for a pre-load recipe."""

    query: str
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    scatter: bool = False
    gated_skip: bool = False


def graph_compose(
    contexts: Dict[str, Any], query: str, top_k: int, *, seeds: int = 5, hops: int = 1
) -> List[Any]:
    """Return graph-aware candidates via the codenib_context composer."""
    try:
        from codenib.agent.skills._graphnav import neighbors
        from codenib.agent.skills.codenib_context.executor import create_executor
        from codenib.agent.skills.context import ComposerContexts

        composer_contexts = ComposerContexts(
            retrieve=contexts.get("retrieve"), expand=contexts.get("expand")
        )
        if composer_contexts.retrieve is None or composer_contexts.expand is None:
            return []
        base = list(
            create_executor(composer_contexts)(query, max_results=top_k, seeds=seeds)
        )
        if hops <= 1:
            return base
        graph = getattr(composer_contexts.expand, "code_graph", None)
        if graph is None:
            return base
        extra: List[Any] = []
        for node in base:
            name = getattr(node, "node_name", None)
            if not name:
                continue
            try:
                extra += neighbors(graph, name, "both", top_k=4)
            except Exception:  # noqa: BLE001 - unresolved node, skip
                continue
        return base + extra
    except Exception:  # noqa: BLE001 - failing retrievers contribute nothing
        return []


def retrieve_candidates(
    contexts: Dict[str, Any],
    retriever: str,
    query: str,
    top_k: int,
    level: str,
    *,
    seeds: int = 5,
    hops: int = 1,
) -> List[Any]:
    """Run one retriever, returning ranked nodes best-effort."""
    if retriever == "graph":
        return graph_compose(contexts, query, top_k, seeds=seeds, hops=hops)
    retrieve_context = contexts.get("retrieve")
    try:
        if (
            retriever == "embedding"
            and getattr(retrieve_context, "vector_store", None) is not None
        ):
            return list(
                retrieve_context.vector_store.search_with_content(
                    query=query, top_k=top_k, level=level
                )
            )
        if retriever == "bm25" and getattr(retrieve_context, "bm25", None) is not None:
            return list(retrieve_context.bm25.search(query, top_k))
    except Exception:  # noqa: BLE001 - failing retrievers contribute nothing
        return []
    return []


def interleave_ranked(lists: Sequence[List[Any]]) -> List[Any]:
    """Round-robin merge rank-1 of each retriever, then rank-2, and so on."""
    out: List[Any] = []
    for index in range(max((len(items) for items in lists), default=0)):
        for items in lists:
            if index < len(items):
                out.append(items[index])
    return out


def snippet_for_node(node: Any) -> str:
    """Return a short indented content snippet for one retrieval node."""
    content = getattr(node, "content", None) or ""
    lines = [line for line in content.splitlines() if line.strip()][:_SNIPPET_LINES]
    return "\n".join("    " + line for line in lines)


def assemble_preload(
    contexts: Dict[str, Any],
    query: str,
    *,
    recipe: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Run a retrieve-only recipe into prompt text and candidate spans.

    ``recipe`` accepts ``retrievers``, ``top_k``, ``level``, ``mode``, ``seeds``,
    and ``hops``. Returned candidate spans are 1-based repo-relative locations
    suitable for prompt injection and evaluation attribution.
    """
    retrievers = recipe.get("retrievers") or ["embedding"]
    top_k = int(recipe.get("top_k", _DEFAULT_TOP_K))
    level = recipe.get("level", _DEFAULT_LEVEL)
    if contexts.get("retrieve") is None:
        return "", []

    seeds = int(recipe.get("seeds", 5))
    hops = int(recipe.get("hops", 1))
    per_retriever = [
        retrieve_candidates(
            contexts, retriever, query, top_k, level, seeds=seeds, hops=hops
        )
        for retriever in retrievers
    ]
    nodes = interleave_ranked(per_retriever)

    span_by_key: Dict[Tuple[str, int, int], Any] = {}
    ordered: List[Dict[str, Any]] = []
    for node in nodes:
        spans = nodes_to_spans([node])
        if spans:
            span = spans[0]
            ordered.append(span)
            span_by_key[(span["file"], span["start"], span["end"])] = node
    ordered = dedup_spans(ordered)[:top_k]
    if not ordered:
        return "", []

    if (recipe or {}).get("mode") in ("eager", "eager_gated", "eager_compact"):
        lines = [
            "# Candidate locations (ranked retrieval hits - the edit site is "
            "usually among the top ones)",
            "Read the most relevant 1-2 candidates FIRST. If one contains the code "
            "the task describes, give your final answer IMMEDIATELY and stop - do "
            "NOT keep grepping or exploring further. Fall back to your own search "
            "ONLY if none of the candidates fits after you read them.",
            "",
        ]
    else:
        lines = [
            "# Candidate locations (UNVERIFIED retrieval hints - often wrong/"
            "incomplete)",
            "A retriever guessed these; the code you must change may be in NONE of "
            "them (it could be a caller, a subclass override, or elsewhere "
            "entirely). They are unordered starting points, not answers. RULES:",
            "- Do NOT anchor on the first candidate. Check each against the actual "
            "problem.",
            "- Never put a location in your final answer unless you have READ it "
            "and confirmed it contains the behavior to change.",
            "- If, after reading, none of these is the real edit site, IGNORE this "
            "list entirely and locate the code with grep / your own search.",
            "",
        ]
    for index, span in enumerate(ordered, 1):
        lines.append(f"{index}. {span['file']}:{span['start']}-{span['end']}")
        node = span_by_key.get((span["file"], span["start"], span["end"]))
        snippet = snippet_for_node(node) if node is not None else ""
        if snippet:
            lines.append(snippet)
    preamble = "\n".join(lines)
    candidate_spans = [
        {"file": span["file"], "start": span["start"], "end": span["end"]}
        for span in ordered
    ]
    return preamble, candidate_spans


def prepare_preload_query(
    contexts: Dict[str, Any],
    query: str,
    *,
    recipe: Dict[str, Any],
    gate_call: Any = None,
) -> PreloadQueryPlan:
    """Prepare the query text for one optional pre-load recipe.

    The caller owns any gate-call accounting. This helper only applies the
    recipe: optional eager gate, candidate assembly, and scatter-vs-inline mode.
    """
    mode = (recipe or {}).get("mode")
    if mode == "eager_gated" and gate_call is not None:
        if not query_is_specific(gate_call, query):
            return PreloadQueryPlan(query=query, gated_skip=True)

    preamble, candidates = assemble_preload(contexts, query, recipe=recipe)
    if mode == "scatter":
        return PreloadQueryPlan(query=query, candidates=candidates, scatter=True)
    if preamble:
        return PreloadQueryPlan(
            query=f"{query}\n\n{preamble}",
            candidates=candidates,
        )
    return PreloadQueryPlan(query=query, candidates=candidates)
