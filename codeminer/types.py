# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
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
ROOT_NODE = "."

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
