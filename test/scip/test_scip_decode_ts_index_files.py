# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A call made inside an index file is a call, not a mention of the file."""

from __future__ import annotations

import json

from codenib.scip_interface.scip_decode_ts import SCIPTypeScriptGraphDecoder
from codenib.types import EDGE_TYPE_REFERENCE

PREFIX = "scip-typescript npm shop 1.0.0 "


def _occurrence(line, symbol, *, definition=None):
    """One decoded occurrence; ``definition`` is its (start, end) line range."""

    rows = [
        "  occurrences {",
        f"    range: {line}",
        "    range: 0",
        "    range: 5",
        f'    symbol: "{PREFIX}{symbol}"',
    ]
    if definition is not None:
        rows.append("    symbol_roles: 1")
        rows += [
            f"    enclosing_range: {definition[0]}",
            "    enclosing_range: 0",
            f"    enclosing_range: {definition[1]}",
            "    enclosing_range: 0",
        ]
    return "\n".join([*rows, "  }"])


def _document(path, *occurrences):
    return "\n".join(
        ["documents {", "  relative_path: " + json.dumps(path), *occurrences, "}"]
    )


INDEX = "\n".join(
    [
        "metadata {",
        "  tool_info {",
        '    name: "scip-typescript"',
        '    version: "0.4.0"',
        "  }",
        '  project_root: "file:///fixture/shop"',
        "  text_document_encoding: UTF8",
        "}",
        _document(
            "src/props.js",
            _occurrence(0, "src/`props.js`/setProperty().", definition=(0, 3)),
        ),
        # preact keeps its core in src/diff/index.js: real code, not a barrel.
        _document(
            "src/diff/index.js",
            # A top-level mention, as in ``export { setProperty } from ...``.
            _occurrence(0, "src/`props.js`/setProperty()."),
            _occurrence(2, "src/diff/`index.js`/diff().", definition=(2, 6)),
            # A call inside ``diff``.
            _occurrence(4, "src/`props.js`/setProperty()."),
        ),
    ]
)


def test_call_inside_an_index_file_reaches_the_function_it_calls(tmp_path):
    decoded = tmp_path / "index.decoded"
    decoded.write_text(INDEX + "\n")
    graph = SCIPTypeScriptGraphDecoder(
        str(decoded), project_root=str(tmp_path)
    ).decode()

    references = {
        (
            graph.graph.vs[edge.source]["name"],
            graph.graph.vs[edge.target]["name"],
            edge["anchor_line"],
        )
        for edge in graph.graph.es
        if edge["type"] == EDGE_TYPE_REFERENCE
    }

    # The call is an edge to ``setProperty``. It used to be an edge from
    # ``diff`` to its own file, and ``setProperty`` had no caller.
    assert references == {
        (
            "shop@1.0.0:src/diff/index.js:diff()",
            "shop@1.0.0:src/props.js:setProperty()",
            4,
        )
    }
