# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query-facing repository retrieval for interactive explanations."""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import TYPE_CHECKING, Callable, List

if TYPE_CHECKING:
    from ....types import QueriedNode
    from ..context import ComposerContexts

_SUPPORTING_DIRECTORIES = frozenset(
    {
        "benchmark",
        "benchmarks",
        "doc",
        "docs",
        "eval",
        "evaluation",
        "example",
        "examples",
        "experiment",
        "experiments",
        "sample",
        "samples",
    }
)
_DOCUMENT_SUFFIXES = frozenset({".adoc", ".md", ".rst"})
_MAX_TOP_K = 20
_CODE_IDENTIFIER_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b"
)
_MECHANISM_QUERY_TERMS = frozenset(
    {
        "check",
        "ensure",
        "enforce",
        "guarantee",
        "load",
        "prevent",
        "runtime",
        "serve",
        "served",
        "stale",
        "validate",
        "verify",
    }
)
_BOOLEAN_IDENTIFIER_TERMS = frozenset(
    {
        "can",
        "check",
        "ensure",
        "has",
        "is",
        "match",
        "matches",
        "should",
        "validate",
        "verify",
    }
)
_MAX_INFERRED_PREDICATES = 3
_ACTION_QUERY_ALIASES = {
    "persist": ("save", "serialize", "write"),
    "persisted": ("save", "serialize", "write"),
    "persistence": ("save", "serialize", "write"),
    "persists": ("save", "serialize", "write"),
    "save": ("persist", "serialize", "write"),
    "saved": ("persist", "serialize", "write"),
    "serialize": ("persist", "save", "write"),
    "serialized": ("persist", "save", "write"),
    "write": ("persist", "save", "serialize"),
    "writes": ("persist", "save", "serialize"),
    "written": ("persist", "save", "serialize"),
}
_TOTAL_CONTENT_BUDGET = 10_000
_MIN_CONTENT_BUDGET = 800
_MAX_CONTENT_BUDGET = 2_400
_QUERY_STOP_TERMS = frozenset(
    {
        "about",
        "built",
        "code",
        "does",
        "explain",
        "from",
        "have",
        "into",
        "repository",
        "source",
        "that",
        "their",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


def _is_supporting_evidence(node: "QueriedNode") -> bool:
    """Whether a result is useful as corroboration rather than implementation."""
    from ....utils import is_non_source_file, is_test_file

    raw_path = str(node.file or node.node_id or "")
    if not raw_path:
        return False
    if is_test_file(raw_path) or is_non_source_file(raw_path):
        return True

    path = PurePath(raw_path.replace("\\", "/"))
    parts = tuple(part.lower() for part in path.parts)
    if any(part in _SUPPORTING_DIRECTORIES for part in parts[:-1]):
        return True
    if path.suffix.lower() in _DOCUMENT_SUFFIXES:
        return True
    return "scripts" in parts[:-1]


def _bounded_top_k(value: int) -> int:
    try:
        top_k = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_k must be an integer between 1 and 20") from exc
    if not 1 <= top_k <= _MAX_TOP_K:
        raise ValueError("top_k must be between 1 and 20")
    return top_k


def _code_identifiers(query: str) -> List[str]:
    """Extract explicit code symbols that can be resolved to exact usages."""
    matches = _CODE_IDENTIFIER_RE.findall(query)
    if not matches:
        return []
    leaves: List[str] = []
    for match in matches:
        leaf = match.rsplit(".", 1)[-1]
        if leaf.startswith("__") and leaf.endswith("__"):
            continue
        if leaf not in leaves:
            leaves.append(leaf)
        if len(leaves) == 3:
            break
    return leaves


def _action_query_aliases(query: str) -> List[str]:
    ordered_terms = list(dict.fromkeys(re.findall(r"[a-z0-9_]+", query.casefold())))
    terms = set(ordered_terms)
    aliases: List[str] = []
    for term in ordered_terms:
        for alias in _ACTION_QUERY_ALIASES.get(term, ()):
            if alias not in terms and alias not in aliases:
                aliases.append(alias)
    return aliases


def _sparse_query(query: str) -> str:
    aliases = _action_query_aliases(query)
    return " ".join((query, *aliases)) if aliases else query


def _retrieved_action_identifiers(
    query: str,
    branches: List[List["QueriedNode"]],
) -> List[str]:
    """Find concrete action symbols surfaced by semantic query expansion."""

    query_terms = set(re.findall(r"[a-z0-9_]+", query.casefold()))
    candidates = set(_action_query_aliases(query))
    candidates.update(query_terms.intersection(_ACTION_QUERY_ALIASES))
    if not candidates:
        return []

    identifiers: List[str] = []
    for branch in branches:
        for node in branch:
            raw_name = str(node.node_name or node.node_id or "")
            symbol = re.sub(r"\([^)]*\)$", "", raw_name.rsplit(":", 1)[-1])
            identifier = symbol.rsplit(".", 1)[-1]
            if identifier.casefold() not in candidates or identifier in identifiers:
                continue
            identifiers.append(identifier)
            if len(identifiers) == _MAX_INFERRED_PREDICATES:
                return identifiers
    return identifiers


def _node_names_identifier(node: "QueriedNode", identifier: str) -> bool:
    raw_name = str(node.node_name or node.node_id or "")
    symbol = re.sub(r"\([^)]*\)$", "", raw_name.rsplit(":", 1)[-1])
    return identifier in {symbol, symbol.rsplit(".", 1)[-1]}


def _inferred_predicate_identifiers(
    query: str,
    branches: List[List["QueriedNode"]],
) -> List[str]:
    """Find returned predicates worth expanding to their enforcing call sites."""
    query_terms = set(re.findall(r"[a-z0-9]+", query.lower()))
    if not query_terms.intersection(_MECHANISM_QUERY_TERMS):
        return []

    expanded_terms = set(query_terms)
    if query_terms.intersection({"stale", "fresh", "current"}):
        expanded_terms.update({"current", "dirty", "fresh", "stale"})
    if query_terms.intersection({"semantic", "embedding", "vector"}):
        expanded_terms.update({"embedding", "semantic", "vector"})
    if query_terms.intersection({"load", "runtime", "serve", "served"}):
        expanded_terms.update({"load", "loader", "runtime", "serve"})

    candidates: dict[str, tuple[int, int, int]] = {}
    for branch_index, branch in enumerate(branches):
        for rank, node in enumerate(branch):
            raw_name = str(node.node_name or node.node_id or "")
            identifier = raw_name.rsplit(":", 1)[-1].removesuffix("()")
            identifier = identifier.rsplit(".", 1)[-1]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
                continue

            identifier_terms = set(re.findall(r"[a-z0-9]+", identifier.lower()))
            predicate_score = 0
            if identifier_terms.intersection({"can", "has", "is", "should"}):
                predicate_score = 4
            elif any(term.startswith("match") for term in identifier_terms):
                predicate_score = 4
            elif identifier_terms.intersection(_BOOLEAN_IDENTIFIER_TERMS):
                predicate_score = 3
            if not predicate_score:
                continue

            score = predicate_score + len(identifier_terms.intersection(expanded_terms))
            candidate = (score, -rank, -branch_index)
            if candidate > candidates.get(identifier, (-1, 0, 0)):
                candidates[identifier] = candidate

    ordered = sorted(
        candidates.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    return [identifier for identifier, _ in ordered[:_MAX_INFERRED_PREDICATES]]


def _render_line_selection(lines: List[str], selected: set[int]) -> str:
    rendered: List[str] = []
    previous = -1
    for index in sorted(selected):
        if previous >= 0 and index > previous + 1:
            rendered.append(f"... {index - previous - 1} source lines omitted ...")
        rendered.append(lines[index])
        previous = index
    if selected and previous < len(lines) - 1:
        rendered.append(f"... {len(lines) - previous - 1} source lines omitted ...")
    return "\n".join(rendered)


def _focus_content(content: str, query: str, budget: int) -> str:
    """Project a large source chunk into query-relevant, line-stable windows."""
    if len(content) <= budget:
        return content

    lines = content.splitlines()
    if not lines:
        return content[:budget]
    query_terms = {
        term
        for term in re.findall(r"[a-z0-9_]+", query.lower())
        if len(term) >= 3 and term not in _QUERY_STOP_TERMS
    }
    selected = set(range(min(5, len(lines))))
    ranked_lines = sorted(
        (
            (
                sum(term in line.lower() for term in query_terms),
                index,
            )
            for index, line in enumerate(lines)
        ),
        key=lambda item: (-item[0], item[1]),
    )

    for score, index in ranked_lines:
        if score <= 0:
            break
        candidate = selected.union(range(max(0, index - 2), min(len(lines), index + 3)))
        if len(_render_line_selection(lines, candidate)) <= budget:
            selected = candidate

    if len(selected) == min(5, len(lines)):
        for index in range(len(selected), len(lines)):
            candidate = selected.union({index})
            if len(_render_line_selection(lines, candidate)) > budget:
                break
            selected = candidate

    focused = _render_line_selection(lines, selected)
    if len(focused) <= budget:
        return focused
    suffix = "\n... source excerpt truncated ..."
    return focused[: max(0, budget - len(suffix))].rstrip() + suffix


def _project_content(
    nodes: List["QueriedNode"],
    *,
    query: str,
    requested_limit: int,
) -> List["QueriedNode"]:
    per_node_budget = max(
        _MIN_CONTENT_BUDGET,
        min(_MAX_CONTENT_BUDGET, _TOTAL_CONTENT_BUDGET // requested_limit),
    )
    projected: List["QueriedNode"] = []
    for node in nodes:
        if not node.content:
            projected.append(node)
            continue
        content = _focus_content(node.content, query, per_node_budget)
        if content == node.content:
            projected.append(node)
        elif hasattr(node, "model_copy"):
            projected.append(node.model_copy(update={"content": content}))
        else:
            projected.append(node.copy(update={"content": content}))
    return projected


def create_executor(
    contexts: "ComposerContexts",
) -> Callable[..., List["QueriedNode"]]:
    """Build a sparse-first retriever with optional dense RRF fusion."""
    from ....ops.retrieve import merge_hybrid, queried_node_key, to_queried_nodes

    retrieve = contexts.retrieve

    def execute(
        query: str,
        top_k: int = 10,
        include_supporting: bool = False,
    ) -> List["QueriedNode"]:
        if retrieve is None or (
            retrieve.bm25 is None and retrieve.vector_store is None
        ):
            raise RuntimeError("Repository search indexes are not available")
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("query must not be empty")

        limit = _bounded_top_k(top_k)
        candidate_limit = min(max(limit * 3, 20), 60)
        branches: List[List["QueriedNode"]] = []
        weights: List[float] = []
        explicit_identifiers = _code_identifiers(normalized_query)

        if retrieve.bm25 is not None:
            sparse = to_queried_nodes(
                retrieve.bm25.search(
                    query=_sparse_query(normalized_query),
                    top_k=candidate_limit,
                    return_code_content=True,
                    wrap_with_ln=True,
                    filter_test=False,
                )
            )
            branches.append(sparse)
            weights.append(1.0)

        if retrieve.vector_store is not None:
            dense = to_queried_nodes(
                retrieve.vector_store.search_with_content(
                    query=normalized_query,
                    top_k=candidate_limit,
                    level=retrieve.default_level,
                )
            )
            branches.append(dense)
            weights.append(1.0)

        occurrence_identifiers = list(explicit_identifiers)
        definition_identifiers = list(explicit_identifiers)
        for identifier in _retrieved_action_identifiers(
            normalized_query,
            branches,
        ):
            if identifier not in occurrence_identifiers:
                occurrence_identifiers.append(identifier)
            if identifier not in definition_identifiers:
                definition_identifiers.append(identifier)

        occurrence_search = (
            getattr(retrieve.bm25, "search_identifier_occurrences", None)
            if retrieve.bm25 is not None
            else None
        )
        if callable(occurrence_search):
            identifiers = occurrence_identifiers
            if not identifiers:
                identifiers = _inferred_predicate_identifiers(
                    normalized_query,
                    branches,
                )
            for identifier in identifiers:
                occurrences = to_queried_nodes(
                    occurrence_search(
                        identifier,
                        context_query=normalized_query,
                        top_k=candidate_limit,
                        wrap_with_ln=True,
                        filter_test=False,
                    )
                )
                if occurrences:
                    branches.append(occurrences)
                    # Exact source occurrences are stronger evidence than a
                    # single lexical or semantic ranking branch.
                    weights.append(2.0)

        if not include_supporting:
            filtered = [
                (
                    [node for node in branch if not _is_supporting_evidence(node)],
                    weight,
                )
                for branch, weight in zip(branches, weights, strict=True)
            ]
        else:
            filtered = list(zip(branches, weights, strict=True))
        filtered = [(branch, weight) for branch, weight in filtered if branch]
        branches = [branch for branch, _ in filtered]
        weights = [weight for _, weight in filtered]
        if not branches:
            return []

        fused = merge_hybrid(
            branches,
            weights=weights,
            fusion="rrf",
        )
        if len(branches) == 1:
            return _project_content(
                fused[:limit],
                query=normalized_query,
                requested_limit=limit,
            )

        # Keep the strongest fused evidence, then give each branch one
        # exclusive slot. Pure RRF can otherwise hide a strong sparse-only,
        # dense-only, or call-site result behind generic cross-view matches.
        fused_by_key = {queried_node_key(node): node for node in fused}
        branch_memberships = {}
        for branch in branches:
            for key in {queried_node_key(node) for node in branch}:
                branch_memberships[key] = branch_memberships.get(key, 0) + 1
        selected: List["QueriedNode"] = []
        selected_keys = set()

        def add(node: "QueriedNode") -> None:
            key = queried_node_key(node)
            if key in selected_keys or len(selected) >= limit:
                return
            selected_keys.add(key)
            selected.append(fused_by_key.get(key, node))

        # An explicitly named symbol asks for its definition before wrappers
        # or call sites that merely mention it.
        for identifier in definition_identifiers:
            for node in fused:
                if _node_names_identifier(node, identifier):
                    add(node)
                    break

        fused_head_count = max(1, limit // 2)
        for node in fused[:fused_head_count]:
            add(node)

        # Give every branch a chance to contribute evidence that no other
        # branch found before filling the remaining budget by rank.
        coverage_order = [
            branch
            for branch, _ in sorted(
                zip(branches, weights, strict=True),
                key=lambda item: item[1],
                reverse=True,
            )
        ]
        for branch in coverage_order:
            for node in branch:
                if branch_memberships[queried_node_key(node)] == 1:
                    add(node)
                    break
        for node in fused:
            add(node)
            if len(selected) >= limit:
                break
        return _project_content(
            selected,
            query=normalized_query,
            requested_limit=limit,
        )

    return execute
