# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.web.source_definition import attach_definition

FILE = [
    "import os",
    "",
    "class Session:",
    "    def resolve_redirects(",
    "        self, resp, req",
    "    ):",
    '        """Follow redirects."""',
    "        hist = []",
    "        url = self.get_redirect_target(resp)",
    "        while url:",
    "            prepared = req.copy()",
    "            hist.append(resp)",
    "            url = None",
    "        return hist",
]


def node(start, end, kind):
    # Graph spans are 0-based and inclusive.
    return SimpleNamespace(start_line=start, end_line=end, kind=kind)


class Graph:
    def __init__(self, nodes):
        self.nodes, self.queries = nodes, []

    def query_range(self, file, start, end):
        self.queries.append((file, start, end))
        return SimpleNamespace(
            defined=[
                n for n in self.nodes if n.start_line <= end and n.end_line >= start
            ]
        )


def read_lines(first, last):
    return FILE[first - 1 : last]


def piece(start, end):
    return {
        "file": "sessions.py",
        "start_line": start,
        "end_line": end,
        "content": "".join(f"{line}\n" for line in FILE[start - 1 : end]),
    }


GRAPH = [node(0, 13, "file"), node(2, 13, "class"), node(3, 13, "method")]


def test_slice_inside_a_method_carries_its_whole_signature():
    graph = Graph(GRAPH)
    result = attach_definition(piece(11, 13), graph, read_lines)
    assert graph.queries == [("sessions.py", 10, 10)]
    assert result["start_line"] == 11
    assert result["definition"] == {
        "start_line": 4,
        "end_line": 6,
        "content": "    def resolve_redirects(\n        self, resp, req\n    ):\n",
    }


def test_slice_just_below_the_signature_is_widened_instead_of_a_tiny_gap():
    result = attach_definition(piece(8, 10), Graph(GRAPH), read_lines)
    assert "definition" not in result
    assert result["start_line"] == 4
    assert result["content"].startswith("    def resolve_redirects(\n")
    assert result["content"].endswith("        while url:\n")


def test_slice_at_a_definition_or_outside_one_is_unchanged():
    at_definition = piece(4, 8)
    assert attach_definition(at_definition, Graph(GRAPH), read_lines) is at_definition
    top = piece(1, 2)
    assert attach_definition(top, Graph(GRAPH), read_lines) is top
    assert attach_definition(piece(11, 13), None, read_lines) == piece(11, 13)
