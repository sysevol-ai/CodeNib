# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.graph.code_graph import CodeGraph
from codenib.graph.incremental.patcher_ts import PatcherTS
from codenib.types import NODE_TYPE_FIELD


def test_location_matcher_falls_back_to_vertex_line_attributes(tmp_path):
    graph = CodeGraph(str(tmp_path))
    graph.add_file_node("src/constants.js")
    graph._add_vertex(
        "pkg:src/constants.js:EMPTY_OBJ",
        {
            "type": NODE_TYPE_FIELD,
            "file": "src/constants.js",
            "start_line": 0,
            "end_line": 0,
            "unified_name": "src/constants.js:EMPTY_OBJ",
        },
    )
    graph._add_vertex(
        "pkg:src/constants.js:EMPTY_ARR",
        {
            "type": NODE_TYPE_FIELD,
            "file": "src/constants.js",
            "start_line": 1,
            "end_line": 1,
            "unified_name": "src/constants.js:EMPTY_ARR",
        },
    )

    patcher = PatcherTS(str(tmp_path), graph)
    patcher.build_indexes()

    assert "pkg:src/constants.js:EMPTY_OBJ" not in graph.symbol_ranges
    assert (
        patcher.match_location_to_vertex("src/constants.js", 0)
        == "pkg:src/constants.js:EMPTY_OBJ"
    )
    assert (
        patcher.match_location_to_scope("src/constants.js", 1)
        == "pkg:src/constants.js:EMPTY_ARR"
    )
