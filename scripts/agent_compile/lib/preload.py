# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pre-load context assembly for the agent.

Retrieval value was being lost because the agent (a strong grep/read pretrain
prior) almost never *calls* the retrieval skills. So instead of offering
retrieval as an optional tool, we compute candidates UP FRONT and inject them
into the agent's opening prompt. The agent then reasons over them and confirms
with grep/read.

Retrieve-ONLY by design: no rerank (the LLM re-judges the candidates itself; a
rerank stage would just re-sort what the model re-sorts anyway — see
``.claude/design/preload-vs-skill-architecture.md``).

Multi-retriever recipes INTERLEAVE (round-robin) rather than concatenate-and-
sort-by-score, because BM25 (unbounded) and embedding (cosine) scores are not
comparable — the exact normalization trap that sank the ``hybrid_search`` skill.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from codeminer.eval.retrieval_eval import dedup_spans, nodes_to_spans

# Keep snippets short so a candidate list of ~10 does not blow up the context.
_SNIPPET_LINES = 4
_DEFAULT_TOP_K = 10
_DEFAULT_LEVEL = "l2"


def _retrieve(rc: Any, retriever: str, query: str, top_k: int, level: str) -> List[Any]:
    """Run a single retriever, returning its ranked nodes (best-effort)."""
    try:
        if retriever == "embedding" and getattr(rc, "vector_store", None) is not None:
            return list(
                rc.vector_store.search_with_content(
                    query=query, top_k=top_k, level=level
                )
            )
        if retriever == "bm25" and getattr(rc, "bm25", None) is not None:
            return list(rc.bm25.search(query, top_k))
    except Exception:  # noqa: BLE001 — a failing retriever just contributes nothing
        return []
    return []


def _interleave(lists: Sequence[List[Any]]) -> List[Any]:
    """Round-robin merge: rank-1 of each retriever, then rank-2, ... (avoids
    cross-retriever score-scale bias)."""
    out: List[Any] = []
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst):
                out.append(lst[i])
    return out


def _snippet(node: Any) -> str:
    content = getattr(node, "content", None) or ""
    lines = [ln for ln in content.splitlines() if ln.strip()][:_SNIPPET_LINES]
    return "\n".join("    " + ln for ln in lines)


def assemble_preload(
    contexts: Dict[str, Any],
    query: str,
    *,
    recipe: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Run a retrieve-only recipe -> (preamble_text, candidate_spans).

    ``recipe``: ``{retrievers: ["embedding"(,"bm25")], top_k: int, level: str}``.
    ``candidate_spans`` are 1-based ``{file,start,end,...}`` (for the prompt AND
    for pre-load contribution attribution in the scorer).
    """
    retrievers = recipe.get("retrievers") or ["embedding"]
    top_k = int(recipe.get("top_k", _DEFAULT_TOP_K))
    level = recipe.get("level", _DEFAULT_LEVEL)
    rc = contexts.get("retrieve")
    if rc is None:
        return "", []

    per_retriever = [_retrieve(rc, r, query, top_k, level) for r in retrievers]
    nodes = _interleave(per_retriever)
    # Map nodes -> spans (1-based, repo-relative via node_id), dedup overlapping
    # regions, cap to top_k. nodes_to_spans re-sorts by score, so derive spans
    # per node to PRESERVE the interleave order; keep a parallel node map for
    # snippet rendering.
    span_by_key: Dict[Tuple[str, int, int], Any] = {}
    ordered: List[Dict[str, Any]] = []
    for node in nodes:
        sp = nodes_to_spans([node])
        if sp:
            ordered.append(sp[0])
            span_by_key[(sp[0]["file"], sp[0]["start"], sp[0]["end"])] = node
    ordered = dedup_spans(ordered)[:top_k]
    if not ordered:
        return "", []

    lines = [
        "# Candidate locations (UNVERIFIED retrieval hints — often wrong/incomplete)",
        "A retriever guessed these; the code you must change may be in NONE of "
        "them (it could be a caller, a subclass override, or elsewhere entirely). "
        "They are unordered starting points, not answers. RULES:",
        "- Do NOT anchor on the first candidate. Check each against the actual "
        "problem.",
        "- Never put a location in your final answer unless you have READ it and "
        "confirmed it contains the behavior to change.",
        "- If, after reading, none of these is the real edit site, IGNORE this "
        "list entirely and locate the code with grep / your own search.",
        "",
    ]
    for i, sp in enumerate(ordered, 1):
        lines.append(f"{i}. {sp['file']}:{sp['start']}-{sp['end']}")
        node = span_by_key.get((sp["file"], sp["start"], sp["end"]))
        snip = _snippet(node) if node is not None else ""
        if snip:
            lines.append(snip)
    preamble = "\n".join(lines)
    # Strip provenance keys the scorer doesn't need in the persisted candidate.
    candidate_spans = [
        {"file": s["file"], "start": s["start"], "end": s["end"]} for s in ordered
    ]
    return preamble, candidate_spans
