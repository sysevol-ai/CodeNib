# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Callable, Iterable, List, Optional, Sequence

from ..types import QueriedNode, is_symbol_node

CandidatePredicate = Callable[[QueriedNode], bool]


def filter_queried_nodes(
    nodes: Sequence[QueriedNode],
    predicates: Iterable[CandidatePredicate],
    *,
    limit: Optional[int] = None,
) -> List[QueriedNode]:
    """Filter retrieval candidates while preserving their existing order."""
    active = list(predicates)
    if not active:
        return list(nodes[:limit] if limit is not None else nodes)

    out: List[QueriedNode] = []
    for node in nodes:
        if all(predicate(node) for predicate in active):
            out.append(node)
            if limit is not None and len(out) >= limit:
                break
    return out


def has_span(node: QueriedNode) -> bool:
    """Return true when a node has file and line-span coordinates."""
    return (
        node.file is not None
        and node.start_line is not None
        and node.end_line is not None
    )


def has_content(node: QueriedNode) -> bool:
    """Return true when a node carries non-empty code/content text."""
    return bool((node.content or "").strip())


def is_symbol_candidate(node: QueriedNode) -> bool:
    """Return true when a node type is a CodeMiner symbol type."""
    return is_symbol_node(node.type)


def include_extensions(extensions: Iterable[str]) -> CandidatePredicate:
    """Build a predicate that keeps nodes whose file suffix is in *extensions*."""
    allowed = {
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in extensions
        if ext
    }

    def predicate(node: QueriedNode) -> bool:
        if not allowed:
            return True
        if not node.file:
            return False
        return PurePosixPath(_normalize_path(node.file)).suffix.lower() in allowed

    return predicate


def exclude_paths(patterns: Iterable[str]) -> CandidatePredicate:
    """Build a predicate that drops nodes whose path matches any glob pattern."""
    globs = [pattern for pattern in patterns if pattern]

    def predicate(node: QueriedNode) -> bool:
        if not globs:
            return True
        path = _normalize_path(node.file or "")
        if not path:
            return True
        return not any(fnmatch(path, pattern) for pattern in globs)

    return predicate


def drop_test_files(node: QueriedNode) -> bool:
    """Return false for common test/spec file paths."""
    return not is_test_path(node.file)


def is_test_path(path: Optional[str]) -> bool:
    """Heuristic path classifier for test/spec files.

    This is intentionally only a generic filesystem heuristic. Ground-truth or
    benchmark-specific notions of "actionable" should stay in evaluation code,
    not in retrieval filtering.
    """
    if not path:
        return False
    normalized = _normalize_path(path)
    lower = normalized.lower()
    parts = [part for part in lower.split("/") if part]
    if any(
        part in {"test", "tests", "testing", "spec", "specs", "__tests__"}
        for part in parts
    ):
        return True
    filename = parts[-1] if parts else lower
    raw_filename = normalized.split("/")[-1] if normalized else ""
    stem = filename.rsplit(".", 1)[0]
    raw_stem = raw_filename.rsplit(".", 1)[0]
    return (
        filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.endswith("_tests.py")
        or ".test." in filename
        or ".spec." in filename
        or stem.endswith("_test")
        or stem.endswith("_tests")
        or stem.endswith("_spec")
        or stem.endswith("-test")
        or stem.endswith("-tests")
        or stem.endswith("-spec")
        or raw_stem.endswith("Test")
        or raw_stem.endswith("Tests")
        or raw_stem.endswith("Spec")
    )


def build_candidate_filters(
    *,
    require_span: bool = False,
    require_content: bool = False,
    symbol_only: bool = False,
    drop_tests: bool = False,
    include_exts: Optional[Iterable[str]] = None,
    exclude_globs: Optional[Iterable[str]] = None,
) -> List[CandidatePredicate]:
    """Construct a filter list from explicit retrieval-pipeline options."""
    predicates: List[CandidatePredicate] = []
    if require_span:
        predicates.append(has_span)
    if require_content:
        predicates.append(has_content)
    if symbol_only:
        predicates.append(is_symbol_candidate)
    if drop_tests:
        predicates.append(drop_test_files)
    if include_exts:
        predicates.append(include_extensions(include_exts))
    if exclude_globs:
        predicates.append(exclude_paths(exclude_globs))
    return predicates


def _normalize_path(path: str) -> str:
    return str(path).replace("\\", "/")
