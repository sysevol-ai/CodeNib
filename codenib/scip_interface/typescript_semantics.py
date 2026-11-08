# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-aware TypeScript facts layered over decoded SCIP text."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from ..types import EDGE_TYPE_IMPORT, NODE_TYPE_FILE
from .scip_decode_view import (
    OccurrenceView,
    SymbolView,
    document_path_from_text,
    occurrence_view_from_text,
    symbol_view_from_text,
)
from .scip_indexer_base import extract_scip_blocks

if TYPE_CHECKING:
    from ..graph.code_graph import CodeGraph

# A document as the import-edge pass reads it: its path and occurrences.
DocumentViews = tuple[Optional[str], Sequence[OccurrenceView]]

_TS_SIGNATURE_RE = re.compile(r"^```(?:ts|typescript)\\n(class|interface|enum|type)\s")
_STATIC_MODULE_PARSERS = threading.local()


def _snake_case(value: str) -> str:
    value = value.removesuffix("Kind")
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return value.replace("-", "_").lower()


def normalize_scip_symbol_kind(value: str | None) -> str | None:
    """Normalize a TextFormat ``SymbolInformation.kind`` enum name."""

    if not value:
        return None
    normalized = _snake_case(value.strip())
    if normalized in {"", "unspecified", "unspecified_kind", "unknown"}:
        return None
    return normalized


def _kind_from_symbol_information(view: SymbolView) -> tuple[str | None, int]:
    if view.kind:
        normalized = normalize_scip_symbol_kind(view.kind)
        if normalized is not None:
            return normalized, 2

    signature = (
        _TS_SIGNATURE_RE.search(view.documentation) if view.documentation else None
    )
    if signature:
        value = signature.group(1)
        return ("type_alias" if value == "type" else value), 1
    return None, 0


def extract_typescript_symbol_kinds(decoded_content: str) -> dict[str, str]:
    """Extract semantic kinds with explicit provider metadata taking priority.

    `scip-typescript` 0.4.0 omits ``kind`` but emits a generated first fenced
    signature. The fallback is deliberately restricted to that exact provider
    shape; arbitrary documentation prose is never classified.
    """

    return typescript_symbol_kinds(
        symbol_view_from_text(block)
        for block in extract_scip_blocks(decoded_content, "symbols")
    )


def typescript_symbol_kinds(symbols: Iterable[SymbolView]) -> dict[str, str]:
    """``extract_typescript_symbol_kinds`` over symbol views, in index order."""

    ranked: dict[str, tuple[str, int]] = {}
    for view in symbols:
        symbol = view.symbol
        kind, priority = _kind_from_symbol_information(view)
        if not symbol or not kind:
            continue
        previous = ranked.get(symbol)
        if previous is None or priority > previous[1]:
            ranked[symbol] = (kind, priority)
    return {symbol: value for symbol, (value, _priority) in ranked.items()}


def _typescript_symbol_target(
    symbol: str,
) -> tuple[tuple[str, str], str] | None:
    """Return the package identity and path encoded in a SCIP symbol."""

    parts = symbol.split(" ", 4)
    if len(parts) < 5 or parts[0] != "scip-typescript":
        return None
    descriptor = parts[4]
    opening = descriptor.find("`")
    closing = descriptor.find("`", opening + 1) if opening >= 0 else -1
    if opening < 0 or closing <= opening:
        return None
    prefix = descriptor[:opening].rstrip("/")
    filename = descriptor[opening + 1 : closing]
    path = f"{prefix}/{filename}" if prefix else filename
    normalized = Path(path.replace("\\", "/")).as_posix().lstrip("/")
    if not normalized:
        return None
    return (parts[2], parts[3]), normalized


def _typescript_project_packages(
    documents: Sequence[DocumentViews],
) -> set[tuple[str, str]]:
    """Collect package identities backed by definitions in this SCIP index."""

    packages: set[tuple[str, str]] = set()
    for _path, occurrences in documents:
        for occurrence in occurrences:
            roles = occurrence.symbol_roles
            if roles is None or not (roles & 1):
                continue
            target = _typescript_symbol_target(occurrence.symbol or "")
            if target is None:
                continue
            package, _path = target
            if package[0] != "." and package[1] != ".":
                packages.add(package)
    return packages


def _point_in_span(
    point: tuple[int, int], span: tuple[tuple[int, int], tuple[int, int]]
) -> bool:
    return span[0] <= point < span[1]


def _static_module_parser(language_name: str):
    """Reuse one native parser per language and thread.

    Some language-pack builds corrupt native state when parsers for different
    grammars are repeatedly created and destroyed in the same decoder pass.
    Thread-local reuse avoids both that lifecycle bug and cross-thread parser
    sharing.
    """

    from tree_sitter_language_pack import get_parser

    parsers = getattr(_STATIC_MODULE_PARSERS, "parsers", None)
    if parsers is None:
        parsers = {}
        _STATIC_MODULE_PARSERS.parsers = parsers
    parser = parsers.get(language_name)
    if parser is None:
        # Use the language-pack parser directly. Constructing the separate
        # ``tree_sitter.Parser`` binding around language-pack capsules corrupts
        # native state after enough mixed TypeScript/JavaScript files on arm64.
        parser = get_parser(language_name)
        parsers[language_name] = parser
    return parser


def _static_module_nodes(source: bytes, suffix: str):
    normalized_suffix = suffix.lower()
    if normalized_suffix == ".tsx":
        language_name = "tsx"
    elif normalized_suffix in {".js", ".jsx"}:
        # JavaScript's grammar natively includes JSX. Parsing large generated
        # JavaScript (Babel's Makefile.js is one example) with the TypeScript
        # grammar can corrupt tree-sitter's native state rather than merely
        # returning error nodes.
        language_name = "javascript"
    else:
        language_name = "typescript"
    parser = _static_module_parser(language_name)
    tree = parser.parse_bytes(source)
    root_node = tree.root_node()
    nodes = []
    for index in range(root_node.named_child_count()):
        node = root_node.named_child(index)
        kind = node.kind()
        if kind == "import_statement" or (
            kind == "export_statement"
            and node.child_by_field_name("source") is not None
        ):
            nodes.append(node)
    # A tree-sitter ``Node`` borrows native memory owned by its ``Tree``. Keep
    # the tree alive while callers read node coordinates; returning bare nodes
    # can become a use-after-free once Python collects this local tree (large
    # workspaces made that surface as a decoder segfault).
    return source, parser, tree, nodes


def _static_module_anchors(source: bytes, suffix: str):
    _source, _parser, _tree, nodes = _static_module_nodes(source, suffix)
    return [
        (
            (
                (node.start_position().row, node.start_position().column),
                (node.end_position().row, node.end_position().column),
            ),
            node.start_position().row,
        )
        for node in nodes
    ]


def typescript_static_module_fingerprint(
    source: bytes, suffix: str
) -> tuple[tuple[object, ...], ...]:
    """Fingerprint static import/re-export text and its source coordinates."""

    _source, _parser, _tree, nodes = _static_module_nodes(source, suffix)
    return tuple(
        (
            node.kind(),
            node.start_position().row,
            node.start_position().column,
            node.end_position().row,
            node.end_position().column,
            source[node.start_byte() : node.end_byte()],
        )
        for node in nodes
    )


def _safe_source_path(project_root: Path, relative_path: str) -> Path | None:
    try:
        root = project_root.resolve(strict=True)
        candidate = (root / relative_path).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def enrich_typescript_import_edges(
    code_graph: "CodeGraph", decoded_content: str, project_root: str | Path | None
) -> int:
    """Add anchored file-to-file import edges from AST spans and SCIP targets.

    The AST decides whether an occurrence is part of a static import or a
    source-bearing re-export. SCIP, not the module string, supplies target file
    identity. Ordinary reference edges remain untouched.
    """

    documents = [
        (
            document_path_from_text(document),
            [
                occurrence_view_from_text(occurrence)
                for occurrence in extract_scip_blocks(document, "occurrences")
            ],
        )
        for document in extract_scip_blocks(decoded_content, "documents")
    ]
    return enrich_typescript_import_edges_from_views(
        code_graph, documents, project_root
    )


def enrich_typescript_import_edges_from_views(
    code_graph: "CodeGraph",
    documents: Sequence[DocumentViews],
    project_root: str | Path | None,
) -> int:
    """``enrich_typescript_import_edges`` over document views, in index order."""

    root = Path(project_root) if project_root is not None else None
    project_packages = _typescript_project_packages(documents)
    added = 0
    # A large decoded workspace already carries tens of thousands of reference
    # edges. Adding import edges to igraph one at a time rebuilds its adjacency
    # indexes after every insertion (quadratic work and, on Babel-scale graphs,
    # a native igraph crash). Buffer the complete enrichment pass and publish
    # it with one add_edges call.
    with code_graph.batch_edges():
        for document_path, occurrences in documents:
            if not document_path:
                continue
            source_file = Path(document_path).as_posix()
            if source_file not in code_graph.name_to_vertex:
                continue
            module_anchors = []
            source_path = (
                _safe_source_path(root, source_file) if root is not None else None
            )
            if source_path is not None:
                try:
                    module_anchors = _static_module_anchors(
                        source_path.read_bytes(), source_path.suffix
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    pass
            for occurrence in occurrences:
                values = occurrence.line_ranges
                if len(values) < 3:
                    continue
                roles = occurrence.symbol_roles or 0
                if roles & 1:
                    continue
                point = (values[0], values[1])
                statement_line = next(
                    (
                        anchor_line
                        for span, anchor_line in module_anchors
                        if _point_in_span(point, span)
                    ),
                    None,
                )
                if not (roles & 2) and statement_line is None:
                    continue
                target = _typescript_symbol_target(occurrence.symbol or "")
                if target is None:
                    continue
                target_package, target_file = target
                if target_package[0].startswith("@types/"):
                    continue
                if target_package[0] != "." and target_package not in project_packages:
                    continue
                if target_file == source_file:
                    continue
                target_id = code_graph.name_to_vertex.get(target_file)
                if target_id is None:
                    continue
                target_attrs = code_graph.graph.vs[target_id].attributes()
                if target_attrs.get("type") != NODE_TYPE_FILE:
                    continue
                before = len(code_graph._edge_index)
                code_graph._add_edge(
                    source_file,
                    target_file,
                    EDGE_TYPE_IMPORT,
                    anchor_file=source_file,
                    anchor_line=(
                        statement_line if statement_line is not None else values[0]
                    ),
                )
                added += int(len(code_graph._edge_index) > before)
    return added
