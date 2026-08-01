# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from typing import Mapping, Optional

from pydantic import BaseModel

NODE_TYPE_DIRECTORY = "directory"
NODE_TYPE_FILE = "file"
NODE_TYPE_SYMBOL = "symbol"
NODE_TYPE_CLASS = "class"
NODE_TYPE_FUNCTION = "function"
NODE_TYPE_METHOD = "method"
NODE_TYPE_FIELD = "field"
EDGE_TYPE_CONTAIN = "contain"
EDGE_TYPE_REFERENCE = "reference"
EDGE_TYPE_IMPORT = "import"
EDGE_TYPE_TYPE_USE = "type-use"
ROOT_NODE = "."

GRAPH_LAYER_ALL = "all"
GRAPH_LAYER_CONTAINMENT = "containment"
GRAPH_LAYER_DEPENDENCY = "dependency"
GRAPH_LAYER_REFERENCE = "reference"
GRAPH_LAYER_IMPORT = "import"
GRAPH_LAYER_TYPE_USE = "type-use"

DEPENDENCY_EDGE_TYPES = frozenset(
    {
        EDGE_TYPE_REFERENCE,
        EDGE_TYPE_IMPORT,
        EDGE_TYPE_TYPE_USE,
    }
)

GRAPH_LAYER_EDGE_TYPES = {
    GRAPH_LAYER_ALL: None,
    GRAPH_LAYER_CONTAINMENT: frozenset({EDGE_TYPE_CONTAIN}),
    GRAPH_LAYER_DEPENDENCY: DEPENDENCY_EDGE_TYPES,
    GRAPH_LAYER_REFERENCE: frozenset({EDGE_TYPE_REFERENCE}),
    GRAPH_LAYER_IMPORT: frozenset({EDGE_TYPE_IMPORT}),
    GRAPH_LAYER_TYPE_USE: frozenset({EDGE_TYPE_TYPE_USE}),
}

# Symbol types - for compatibility, keep NODE_TYPE_SYMBOL but add specific types
SYMBOL_TYPES = {
    NODE_TYPE_SYMBOL,
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_FIELD,
}

# LSP 3.17 ``SymbolKind`` names, normalized to the persisted graph spelling.
# These are semantic labels; the existing coarse ``NODE_TYPE_*`` mapping
# remains the compatibility surface for traversal and ranking.
LSP_SYMBOL_KINDS = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}


def is_symbol_node(node_type):
    """Whether ``node_type`` is any symbol (class/function/method/generic)."""
    return node_type in SYMBOL_TYPES


def node_has_definition(attributes: Mapping) -> bool:
    """Whether a symbol vertex has an observed definition location.

    Schema-v5 graphs carry an explicit boolean. The range-based fallback keeps
    transient graphs built by older in-memory callers usable; persisted v4
    graphs are rejected by the schema-version guard before reaching consumers.
    """

    explicit = attributes.get("has_definition")
    if isinstance(explicit, bool):
        return explicit
    return (
        is_symbol_node(attributes.get("type"))
        and isinstance(attributes.get("file"), str)
        and isinstance(attributes.get("start_line"), int)
        and not isinstance(attributes.get("start_line"), bool)
        and isinstance(attributes.get("end_line"), int)
        and not isinstance(attributes.get("end_line"), bool)
    )


def node_is_reference_only(attributes: Mapping) -> bool:
    """Whether schema v5 explicitly marks a symbol as reference-only."""

    return attributes.get("has_definition") is False


def lsp_symbol_kind(kind: object) -> Optional[str]:
    """Return the normalized semantic label for an LSP ``SymbolKind``."""

    if isinstance(kind, bool) or not isinstance(kind, int):
        return None
    return LSP_SYMBOL_KINDS.get(kind)


def normalize_graph_layer(layer):
    """Normalize and validate a graph layer name."""
    normalized = (layer or "").strip().lower()
    if normalized not in GRAPH_LAYER_EDGE_TYPES:
        supported = ", ".join(sorted(GRAPH_LAYER_EDGE_TYPES))
        raise ValueError(f"Unknown graph layer {layer!r}; supported: {supported}")
    return normalized


def edge_types_for_graph_layer(layer):
    """Return edge types included by a graph layer.

    ``None`` means "all edge types currently present in the graph".
    """
    return GRAPH_LAYER_EDGE_TYPES[normalize_graph_layer(layer)]


def graph_layers_for_edge_type(edge_type):
    """Return every default layer that contains ``edge_type``."""
    layers = [GRAPH_LAYER_ALL]
    for layer, edge_types in GRAPH_LAYER_EDGE_TYPES.items():
        if layer == GRAPH_LAYER_ALL:
            continue
        if edge_types is not None and edge_type in edge_types:
            layers.append(layer)
    return layers


class NodeInfo(BaseModel):
    """Node attributes for graph nodes."""

    node_name: str = ""
    type: str = ""
    file: Optional[str] = None
    node_id: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    score: Optional[float] = None
    content: Optional[str] = None


class QueriedNode(NodeInfo):
    """Node attributes representing ranked nodes that keep optional content."""

    pass
