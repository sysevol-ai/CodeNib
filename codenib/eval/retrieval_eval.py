# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Utility functions for evaluating retrieval outputs against labeled targets."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..types import QueriedNode


def normalize_file_path(value: Optional[str]) -> Optional[str]:
    """Normalize file paths emitted by retrieval to canonical posix format."""
    if not value:
        return None
    normalized = str(Path(value).as_posix())
    return normalized.lstrip("./")


def normalize_symbol_identifier(value: Optional[str]) -> Optional[str]:
    """Normalize ``file:Symbol`` so the file portion matches the retrieved form."""
    if not value:
        return None
    if ":" not in value:
        return value
    file_part, symbol_part = value.split(":", 1)
    normalized_file = normalize_file_path(file_part)
    if normalized_file:
        return f"{normalized_file}:{symbol_part}"
    return value


def collect_targets(
    instance: Mapping[str, object], simplified_symbols: bool = True
) -> Tuple[List[str], List[str]]:
    """Aggregate and normalize file + symbol labels from a dataset instance.

    If ``simplified_symbols`` is True, use ``symbols_modified`` and
    ``symbols_deleted`` (excluding ``symbols_added``); otherwise include
    all three.
    """
    target_files = instance.get("target_files") or []

    if simplified_symbols:
        # Use symbols_modified and symbols_deleted, exclude symbols_added
        target_symbols_raw = list(instance.get("symbols_modified") or [])
        target_symbols_raw.extend(instance.get("symbols_deleted") or [])
        # Filter out class nodes (symbols without parentheses)
        target_symbols = [s for s in target_symbols_raw if "(" in s]
    else:
        target_symbols: List[str] = []
        for key in ("symbols_modified", "symbols_added", "symbols_deleted"):
            target_symbols.extend(instance.get(key) or [])

    normalized_files = [
        path for path in (normalize_file_path(value) for value in target_files) if path
    ]
    normalized_symbols = [
        symbol
        for symbol in (normalize_symbol_identifier(value) for value in target_symbols)
        if symbol
    ]
    return normalized_files, normalized_symbols


def build_symbol_prediction(node: QueriedNode) -> Optional[str]:
    """Format a retrieved node into `file:node_name` from node_id."""
    # Use node_id (format: "file.py:symbol" or "file.py" for headers)
    if node.node_id and ":" in node.node_id:
        return normalize_symbol_identifier(node.node_id)
    return None


def extract_predictions(
    nodes: Sequence[QueriedNode],
) -> Tuple[List[str], List[str]]:
    """Extract unique files and symbols from retrieved nodes."""
    seen_files = set()
    unique_files_ordered = []
    for node in nodes:
        if node.node_id:
            file_path = (
                node.node_id.split(":")[0] if ":" in node.node_id else node.node_id
            )
            normalized = normalize_file_path(file_path)
            if normalized and normalized not in seen_files:
                seen_files.add(normalized)
                unique_files_ordered.append(normalized)

    normalized_symbols = [
        value
        for value in (build_symbol_prediction(node) for node in nodes)
        if value is not None
    ]

    return unique_files_ordered, normalized_symbols


def compute_metrics(
    predictions: Sequence[str],
    targets: Sequence[str],
) -> Dict[str, float]:
    """Compute accuracy (hit@K), precision, recall, and hit counts for a single scope."""
    predicted_values = set(predictions)
    target_values = set(targets)
    hits = len(predicted_values.intersection(target_values))
    # Use issubset logic: accuracy=1.0 only if ALL targets are in predictions
    accuracy = (
        1.0 if target_values and target_values.issubset(predicted_values) else 0.0
    )
    precision = hits / max(len(predictions), 1)
    recall = hits / len(target_values) if target_values else 0.0
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "hits": hits,
    }


def evaluate_predictions(
    nodes: Sequence[QueriedNode],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    ks: Sequence[int],
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """Evaluate retrieved nodes against targets for multiple cutoffs."""
    unique_files_ordered, normalized_symbols = extract_predictions(nodes)

    metrics = {"files": {}, "symbols": {}}
    for k in ks:
        # File-level: evaluate against top-k unique files
        metrics["files"][k] = compute_metrics(unique_files_ordered[:k], target_files)
        # Symbol-level: evaluate against top-k nodes/symbols
        metrics["symbols"][k] = compute_metrics(normalized_symbols[:k], target_symbols)
    return metrics


def aggregate_metrics(
    aggregate: Dict[str, Dict[int, Dict[str, float]]],
    instance_metrics: Dict[str, Dict[int, Dict[str, float]]],
) -> None:
    """Accumulate per-instance metrics into a running aggregate."""
    for scope in ("files", "symbols"):
        for k, stats in instance_metrics[scope].items():
            scoped = aggregate.setdefault(scope, {}).setdefault(
                k, {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0.0}
            )
            scoped["accuracy"] += stats["accuracy"]
            scoped["precision"] += stats["precision"]
            scoped["recall"] += stats["recall"]
            scoped["hits"] += stats["hits"]


def average_metrics(
    aggregate: Dict[str, Dict[int, Dict[str, float]]], count: int
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """Convert accumulated metrics into dataset-level averages."""
    if count == 0:
        return aggregate
    averaged: Dict[str, Dict[int, Dict[str, float]]] = {}
    for scope, per_k in aggregate.items():
        averaged[scope] = {}
        for k, stats in per_k.items():
            averaged[scope][k] = {
                "accuracy": stats["accuracy"] / count,
                "precision": stats["precision"] / count,
                "recall": stats["recall"] / count,
                "avg_hits": stats["hits"] / count,
            }
    return averaged


def summarize_predictions(
    nodes: Sequence[QueriedNode],
    limit: Optional[int] = None,
) -> List[Dict[str, object]]:
    """Return a serializable view of ranked nodes for downstream inspection."""
    summary = []
    for rank, node in enumerate(nodes):
        if limit is not None and rank >= limit:
            break
        summary.append(
            {
                "rank": rank + 1,
                "node_name": node.node_name,
                "file": normalize_file_path(node.file),
                "score": node.score,
            }
        )
    return summary


# ---------------------------------------------------------------------------
# Agent-localization scoring
#
# An *agent* localizes through more than retrieval-skill output: it names files
# in its final answer and reads files directly. ``score_agent_localization``
# scores files@k / symbols@k from the union of (1) the answer's ``Files:`` line
# (or bare path tokens), (2) the paths the agent ``read``, and (3) the files
# referenced by retrieval-skill nodes — reflecting how the agent actually
# localized, not only what a retriever returned. Same output shape as
# :func:`evaluate_predictions`. Shared by the sweep runner and the offline
# ablations so files@k is defined once.
# ---------------------------------------------------------------------------

# Agents routinely decorate the answer contract with markdown — ``**Files:**``,
# ``- Symbols:``, ``### Locations``. A strict ``^\s*files?`` anchor silently
# missed all of those (a major cause of the symbols@k≈0 artifact), so tolerate
# leading/surrounding markdown emphasis (* _ ` # > -) around the label.
_MD = r"[\s>#*_`\-]*"
_FILES_LINE = re.compile(rf"(?im)^{_MD}files?{_MD}[:=]{_MD}\s*(.+)$")
_SYMBOLS_LINE = re.compile(rf"(?im)^{_MD}symbols?{_MD}[:=]{_MD}\s*(.+)$")
_PATH_TOKEN = re.compile(r"[\w./\\-]+\.[A-Za-z0-9_]+")


def _rel_norm(path: str, repo_path: str) -> Optional[str]:
    """Normalize a path to the repo-relative posix form used by the GT."""
    p = (path or "").strip().strip("`'\"")
    if not p:
        return None
    if repo_path and os.path.isabs(p):
        try:
            p = os.path.relpath(p, repo_path)
        except ValueError:
            # Path can't be made relative to repo_path (e.g. different drive on
            # Windows, or an unrelated root): keep the original absolute path and
            # let normalize_file_path handle it. Non-fatal — scoring tolerates it.
            pass
    return normalize_file_path(p)


def _dedup(seq: Sequence[Optional[str]]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _answer_files(answer: str) -> List[str]:
    """Files named in the answer: explicit ``Files:`` line, else path tokens."""
    explicit: List[str] = []
    for m in _FILES_LINE.finditer(answer or ""):
        explicit.extend(t for t in re.split(r"[,\s]+", m.group(1).strip()) if t)
    if explicit:
        return explicit
    return _PATH_TOKEN.findall(answer or "")  # soft fallback


def _answer_symbols(answer: str) -> List[str]:
    syms: List[str] = []
    for m in _SYMBOLS_LINE.finditer(answer or ""):
        syms.extend(t for t in re.split(r"[,\s]+", m.group(1).strip()) if t)
    return syms


def score_agent_localization(
    *,
    answer: str,
    file_read_paths: Sequence[str],
    nodes: Sequence[Any],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    ks: Sequence[int],
    repo_path: str,
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """files@k / symbols@k from the agent's answer + read paths + skill nodes.

    Predicted files are an ordered union of (1) the answer's ``Files:`` line /
    path tokens, (2) the paths the agent ``read``, and (3) retrieval-skill node
    files. Predicted symbols come from the answer's ``Symbols:`` line plus skill
    nodes. Returns the same ``{"files": {k: {...}}, "symbols": {...}}`` shape as
    :func:`evaluate_predictions`.
    """
    node_files, node_symbols = extract_predictions(nodes)
    answer = answer or ""
    pred_files = _dedup(
        [_rel_norm(f, repo_path) for f in _answer_files(answer)]
        + [_rel_norm(p, repo_path) for p in (file_read_paths or [])]
        + list(node_files)
    )
    pred_symbols = _dedup(
        [normalize_symbol_identifier(s) for s in _answer_symbols(answer)]
        + list(node_symbols)
    )
    metrics: Dict[str, Dict[int, Dict[str, float]]] = {"files": {}, "symbols": {}}
    for k in ks:
        metrics["files"][k] = compute_metrics(pred_files[:k], target_files)
        metrics["symbols"][k] = compute_metrics(pred_symbols[:k], target_symbols)
    return metrics


# ---------------------------------------------------------------------------
# Line-span localization scoring
#
# Symbol-name string matching is brittle: the GT carries ``file:Class.foo()``
# while the agent writes a bare ``foo`` in prose, so genuinely-located code can
# score zero (see the "named-but-missed" failure mode). Line spans sidestep this
# entirely — a prediction ``(file, start, end)`` HITS a ground-truth code block
# when the files match and the line ranges OVERLAP. Overlap (not exact equality)
# is what makes this robust to two real skews:
#   * origin: tree-sitter graph nodes are 0-based, ``gt_code_blocks`` are 1-based
#     (the project-wide line-origin gotcha) — callers pass ``base=0`` for nodes;
#   * boundary: a graph node spans the whole *definition* while a GT block is the
#     edited *hunk*, so their starts differ by a line or two but still overlap.
#
# Two scopes are scored from DIFFERENT prediction sources — neither bounds the
# other (an agent can name in its answer a region grep/reasoning found that the
# retriever never returned, so ``answer`` can exceed ``retrieval``; see the
# "answer > retrieval" unit test):
#   retrieval — the ranked spans the retrieval skills returned (nodes only).
#               This is the cross-system ALIGNMENT axis: it is defined
#               identically to a plain RAG pipeline's recall@k (node_id match ==
#               span overlap, verified), so RAG / pure-embedding / agent are
#               directly comparable on it. Measures RETRIEVAL quality.
#   answer    — the spans the agent committed to in its final answer
#               (``Locations:`` ranges + ``Symbols:`` resolved via the graph).
#               This is the agent's end-to-end DELIVERABLE (the headline);
#               precision-honest, so dumping context cannot inflate it.
# ("surfaced"/"ceiling" framing was dropped: it conflated nodes+reads and was not
# a true upper bound.)
# ---------------------------------------------------------------------------

# ``path:start-end`` / ``path:start`` / ``path#L12-L45`` location tokens.
# Markdown-tolerant label (see ``_FILES_LINE``): ``**Locations:**`` is common.
_LOCATIONS_LINE = re.compile(rf"(?im)^{_MD}locations?{_MD}[:=]{_MD}\s*(.+)$")
_LOC_TOKEN = re.compile(r"([\w./\\-]+\.[A-Za-z0-9_]+)[:#]L?(\d+)(?:\s*[-–]\s*L?(\d+))?")


def _make_span(
    file: Optional[str],
    start: Any,
    end: Any,
    *,
    base: int = 1,
    score: float = 0.0,
    source: str = "",
    repo_path: str = "",
) -> Optional[Dict[str, Any]]:
    """Normalize a raw ``(file, start, end)`` into a 1-based inclusive span dict.

    ``base=0`` shifts 0-based inputs (tree-sitter graph nodes) up to 1-based so
    they line up with ``gt_code_blocks``. Returns ``None`` for unusable input.
    """
    nf = _rel_norm(file, repo_path) if repo_path else normalize_file_path(file)
    if not nf or start is None or end is None:
        return None
    try:
        s, e = int(start), int(end)
    except (TypeError, ValueError):
        return None
    shift = 1 - base  # base=0 -> +1 ; base=1 -> +0
    s, e = s + shift, e + shift
    if e < s:
        s, e = e, s
    return {"file": nf, "start": s, "end": e, "score": float(score), "source": source}


def collect_target_blocks(instance: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Normalized 1-based ground-truth code blocks.

    Two sources, with DIFFERENT line origins (the project's line-origin gotcha):
      * ``gt_code_blocks`` / ``code_blocks`` (SWE-bench patch hunks) are 1-based.
      * ``gt_symbol_nodes`` (codenib-synthesis) are copied straight from the
        0-based tree-sitter graph attrs by the synthesis context_loader, so they
        are 0-based and must be shifted +1.
    Each block keeps ``symbol``/``change_type`` for downstream inspection.
    """
    raw = instance.get("gt_code_blocks")
    if raw is None:
        raw = instance.get("code_blocks")
    if raw is not None:
        base, symbol_key = 1, "symbol"
    else:  # synthesis rows expose 0-based symbol nodes instead
        raw = instance.get("gt_symbol_nodes") or []
        base, symbol_key = 0, "node_name"
    blocks: List[Dict[str, Any]] = []
    for blk in raw:
        sp = _make_span(
            blk.get("file_path") or blk.get("file"),
            blk.get("start_line"),
            blk.get("end_line"),
            base=base,
            source="gt",
        )
        if sp:
            sp["symbol"] = blk.get("symbol") or blk.get(symbol_key)
            sp["change_type"] = blk.get("change_type")
            blocks.append(sp)
    return blocks


def spans_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """True when two spans are in the same file and their line ranges overlap."""
    return a["file"] == b["file"] and a["start"] <= b["end"] and b["start"] <= a["end"]


def dedup_spans(spans: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop spans that overlap an already-kept span (first-wins, rank-order).

    Reading a function twice, or a retrieval node plus an answer line that point
    at the same region, should occupy ONE slot in the top-k, not several — else a
    single hit could be padded to fill the cutoff.
    """
    kept: List[Dict[str, Any]] = []
    for sp in spans:
        if any(spans_overlap(sp, k) for k in kept):
            continue
        kept.append(sp)
    return kept


def nodes_to_spans(
    nodes: Sequence[QueriedNode], *, sort_by_score: bool = True
) -> List[Dict[str, Any]]:
    """Spans from retrieval nodes.

    Two empirically-verified quirks of retrieval ``QueriedNode``s drive this:
      * ``node.file`` is the absolute *build-time* path baked into the index
        (``/.../.codenib/...``), which matches neither the GT nor the current
        repo. The ``node_id`` file part IS repo-relative, so prefer it.
      * raw ``NodeInfo`` / ``QueriedNode`` line spans are internal 0-based
        (the agent boundary shifts only the JSON shown to the LLM), so shift
        them +1 here to score against 1-based GT / answer spans.
    """
    out: List[Dict[str, Any]] = []
    for node in nodes:
        node_id = getattr(node, "node_id", None)
        file = node_id.split(":")[0] if node_id and ":" in node_id else None
        if not file:
            file = getattr(node, "file", None)
        sp = _make_span(
            file,
            getattr(node, "start_line", None),
            getattr(node, "end_line", None),
            base=0,
            score=getattr(node, "score", 0.0) or 0.0,
            source="node",
        )
        if sp:
            out.append(sp)
    if sort_by_score:
        out.sort(key=lambda s: s["score"], reverse=True)
    return out


def parse_answer_spans(answer: str, repo_path: str = "") -> List[Dict[str, Any]]:
    """Spans the agent *committed to* via a ``Locations:`` line.

    Only the structured line is trusted — scraping arbitrary prose line mentions
    is too noisy and would reward verbosity. Line numbers there are 1-based (the
    agent reads them straight from the 1-based ``read`` output).
    """
    payload = " ".join(m.group(1) for m in _LOCATIONS_LINE.finditer(answer or ""))
    if not payload:
        return []
    spans: List[Dict[str, Any]] = []
    for m in _LOC_TOKEN.finditer(payload):
        sp = _make_span(
            m.group(1),
            m.group(2),
            m.group(3) or m.group(2),
            base=1,
            source="answer",
            repo_path=repo_path,
        )
        if sp:
            spans.append(sp)
    return spans


def resolve_symbol_spans(
    answer: str,
    span_index: Mapping[Tuple[str, str], Tuple[int, int]],
    repo_path: str = "",
) -> List[Dict[str, Any]]:
    """Spans for symbols named in the agent's ``Symbols:`` line, resolved via a
    ``{(norm_file, leaf_name): (start, end)}`` 1-based index.

    Bridges the exact gap the span metric exists to close: the agent commits to a
    symbol name (``fixes.rs:remove_unused...``) but not an explicit line range —
    string matching scores it zero, but resolving the name to its span credits
    the genuine hit.
    """
    spans: List[Dict[str, Any]] = []
    for raw in _answer_symbols(answer):
        norm = normalize_symbol_identifier(raw)
        if not norm or ":" not in norm:
            continue
        file_part, sym = norm.split(":", 1)
        file = normalize_file_path(file_part)
        leaf = sym.strip().split("(")[0].split(".")[-1].split("/")[-1]
        rng = span_index.get((file, leaf)) if file else None
        if rng:
            sp = _make_span(file, rng[0], rng[1], base=1, source="answer_symbol")
            if sp:
                spans.append(sp)
    return spans


def compute_block_metrics(
    pred_spans: Sequence[Mapping[str, Any]],
    gt_blocks: Sequence[Mapping[str, Any]],
) -> Dict[str, float]:
    """Overlap-based accuracy / precision / recall for one top-k prediction set.

    ``pred_spans`` must already be ranked and truncated to k. A GT block is
    recalled if any prediction overlaps it; a prediction is a hit if it overlaps
    any GT block (precision = on-target fraction of the top-k).
    """
    if not gt_blocks:
        return {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0}
    recalled = sum(1 for g in gt_blocks if any(spans_overlap(p, g) for p in pred_spans))
    on_target = sum(
        1 for p in pred_spans if any(spans_overlap(p, g) for g in gt_blocks)
    )
    accuracy = 1.0 if recalled == len(gt_blocks) else 0.0
    precision = on_target / max(len(pred_spans), 1)
    recall = recalled / len(gt_blocks)
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "hits": recalled,
    }


def score_localization_spans(
    *,
    answer_spans: Sequence[Dict[str, Any]],
    retrieval_spans: Sequence[Dict[str, Any]],
    gt_blocks: Sequence[Mapping[str, Any]],
    ks: Sequence[int],
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """blocks@k for the ``answer`` and ``retrieval`` scopes (overlap-based).

    ``answer`` = what the agent committed to (its DELIVERABLE; the headline).
    ``retrieval`` = the retriever's ranked spans (the cross-system ALIGNMENT
    axis, == a RAG pipeline's recall@k). Neither bounds the other: the agent can
    commit to a region grep/reasoning found that retrieval never returned. Both
    lists are deduped by overlap before truncation so a region predicted several
    times cannot pad the top-k. Each scope reports accuracy / precision / recall;
    ``recall`` is the metric used for cross-system alignment.
    """
    answer = dedup_spans(list(answer_spans))
    retrieval = dedup_spans(list(retrieval_spans))
    out: Dict[str, Dict[int, Dict[str, float]]] = {
        "answer_blocks": {},
        "retrieval_blocks": {},
    }
    for k in ks:
        out["answer_blocks"][k] = compute_block_metrics(answer[:k], gt_blocks)
        out["retrieval_blocks"][k] = compute_block_metrics(retrieval[:k], gt_blocks)
    return out
