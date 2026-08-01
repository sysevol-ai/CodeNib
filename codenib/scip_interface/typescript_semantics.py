# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-aware TypeScript facts layered over decoded SCIP text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..types import EDGE_TYPE_IMPORT, NODE_TYPE_FILE
from .scip_indexer_base import extract_scip_blocks, extract_symbol

if TYPE_CHECKING:
    from ..graph.code_graph import CodeGraph


_KIND_FIELD_RE = re.compile(r"(?:^|\n)\s*kind:\s*([A-Za-z][A-Za-z0-9_]*)")
_DOCUMENTATION_RE = re.compile(r'(?:^|\n)\s*documentation:\s*"((?:\\.|[^"\\])*)"')
_TS_SIGNATURE_RE = re.compile(r"^```(?:ts|typescript)\\n(class|interface|enum|type)\s")
_ROLES_RE = re.compile(r"symbol_roles:\s*(\d+)")
_RANGE_RE = re.compile(r"(?:^|\n)\s*range:\s*(\d+)")


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


def _kind_from_symbol_information(block: str) -> tuple[str | None, int]:
    explicit = _KIND_FIELD_RE.search(block)
    if explicit:
        normalized = normalize_scip_symbol_kind(explicit.group(1))
        if normalized is not None:
            return normalized, 2

    first_documentation = _DOCUMENTATION_RE.search(block)
    signature = (
        _TS_SIGNATURE_RE.search(first_documentation.group(1))
        if first_documentation
        else None
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

    ranked: dict[str, tuple[str, int]] = {}
    for block in extract_scip_blocks(decoded_content, "symbols"):
        symbol = extract_symbol(block)
        kind, priority = _kind_from_symbol_information(block)
        if not symbol or not kind:
            continue
        previous = ranked.get(symbol)
        if previous is None or priority > previous[1]:
            ranked[symbol] = (kind, priority)
    return {symbol: value for symbol, (value, _priority) in ranked.items()}


def _typescript_module_path(symbol: str) -> str | None:
    """Return the provider-resolved repository path encoded in a SCIP symbol."""

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
    return normalized or None


def _point_in_span(
    point: tuple[int, int], span: tuple[tuple[int, int], tuple[int, int]]
) -> bool:
    return span[0] <= point < span[1]


def _static_module_nodes(source: bytes, suffix: str):
    from tree_sitter import Parser
    from tree_sitter_language_pack import get_language

    language_name = "tsx" if suffix.lower() in {".tsx", ".jsx"} else "typescript"
    language = get_language(language_name)
    try:
        parser = Parser(language)
    except TypeError:  # tree-sitter < 0.25 compatibility
        parser = Parser()
        parser.language = language
    root = parser.parse(source).root_node
    return [
        node
        for node in root.named_children
        if node.type == "import_statement"
        or (
            node.type == "export_statement"
            and node.child_by_field_name("source") is not None
        )
    ]


def _static_module_spans(source: bytes, suffix: str):
    return [
        (
            (node.start_point.row, node.start_point.column),
            (node.end_point.row, node.end_point.column),
        )
        for node in _static_module_nodes(source, suffix)
    ]


def typescript_static_module_fingerprint(
    source: bytes, suffix: str
) -> tuple[tuple[object, ...], ...]:
    """Fingerprint static import/re-export text and its source coordinates."""

    return tuple(
        (
            node.type,
            node.start_point.row,
            node.start_point.column,
            node.end_point.row,
            node.end_point.column,
            source[node.start_byte : node.end_byte],
        )
        for node in _static_module_nodes(source, suffix)
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

    if project_root is None:
        return 0
    root = Path(project_root)
    added = 0
    for document in extract_scip_blocks(decoded_content, "documents"):
        path_match = re.search(r'relative_path:\s*"([^"]+)"', document)
        if not path_match:
            continue
        source_file = Path(path_match.group(1)).as_posix()
        source_path = _safe_source_path(root, source_file)
        if source_path is None or source_file not in code_graph.name_to_vertex:
            continue
        try:
            spans = _static_module_spans(source_path.read_bytes(), source_path.suffix)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        for occurrence in extract_scip_blocks(document, "occurrences"):
            values = [int(value) for value in _RANGE_RE.findall(occurrence)]
            if len(values) < 3:
                continue
            roles_match = _ROLES_RE.search(occurrence)
            roles = int(roles_match.group(1)) if roles_match else 0
            if roles & 1:
                continue
            point = (values[0], values[1])
            if not (roles & 2) and not any(
                _point_in_span(point, span) for span in spans
            ):
                continue
            symbol = extract_symbol(occurrence)
            target_file = _typescript_module_path(symbol or "")
            if not target_file or target_file == source_file:
                continue
            target_id = code_graph.name_to_vertex.get(target_file)
            if target_id is None:
                continue
            target_attrs = code_graph.graph.vs[target_id].attributes()
            if target_attrs.get("type") != NODE_TYPE_FILE:
                continue
            before = code_graph.graph.ecount()
            code_graph._add_edge(
                source_file,
                target_file,
                EDGE_TYPE_IMPORT,
                anchor_file=source_file,
                anchor_line=values[0],
            )
            added += int(code_graph.graph.ecount() > before)
    return added
