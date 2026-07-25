# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from typing import Optional

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


def is_symbol_node(node_type):
    """Whether ``node_type`` is any symbol (class/function/method/generic)."""
    return node_type in SYMBOL_TYPES


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
