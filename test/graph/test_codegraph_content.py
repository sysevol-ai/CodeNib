# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.graph.code_graph import CodeGraph
from codenib.types import NODE_TYPE_FUNCTION


def test_get_node_content_uses_zero_based_inclusive_source_range(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text(
        "first line\nsecond line\nthird line\nfourth line\n",
        encoding="utf-8",
    )

    graph = CodeGraph(project_root=str(tmp_path))
    graph._add_vertex(
        "sample.py:first_two",
        {
            "type": NODE_TYPE_FUNCTION,
            "file": "sample.py",
            "start_line": 0,
            "end_line": 1,
        },
    )
    graph._add_vertex(
        "sample.py:last_two",
        {
            "type": NODE_TYPE_FUNCTION,
            "file": "sample.py",
            "start_line": 2,
            "end_line": 3,
        },
    )

    assert graph.get_node_content(0) == "first line\nsecond line\n"
    assert graph.get_node_content(1) == "third line\nfourth line\n"
