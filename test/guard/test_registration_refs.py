# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A command-line handler named in ``set_defaults`` is registered, not called."""

from __future__ import annotations

import textwrap

from codenib.graph.registration_refs import registration_references
from codenib.guard.impact import ImpactGraph

CLI = textwrap.dedent(
    """\
    def build_parser(parser):
        index = parser.add_parser("index")
        index.set_defaults(handler=run_index)
        wiki = parser.add_parser("wiki")
        wiki.set_defaults(
            handler=run_wiki,
        )
        run_index(None)
        for name, handler in (("status", run_status), (make_name(), run_index)):
            parser.add_parser(name).set_defaults(handler=handler)


    def run_index(args):
        return 0


    def run_wiki(args):
        return 0


    def run_status(args):
        return 0


    def make_name():
        return "other"
    """
)
NODES = [
    ("cli.py", "file", None, None, None),
    ("cli.py:build_parser()", "function", "cli.py", 0, 9),
    ("cli.py:run_index()", "function", "cli.py", 12, 13),
    ("cli.py:run_wiki()", "function", "cli.py", 16, 17),
    ("cli.py:run_status()", "function", "cli.py", 20, 21),
    ("cli.py:make_name()", "function", "cli.py", 24, 25),
    ("tests/test_cli.py:test_parser()", "function", "tests/test_cli.py", 0, 1),
]
EDGES = [
    ("cli.py:build_parser()", "cli.py:run_index()", "reference", 2),
    ("cli.py:build_parser()", "cli.py:run_wiki()", "reference", 5),
    # A real call of the same function, on another line, stays a call.
    ("cli.py:build_parser()", "cli.py:run_index()", "reference", 7),
    # The loop's table names two handlers and calls one function.
    ("cli.py:build_parser()", "cli.py:run_status()", "reference", 8),
    ("cli.py:build_parser()", "cli.py:make_name()", "reference", 8),
    ("cli.py:build_parser()", "cli.py:run_index()", "reference", 8),
    ("tests/test_cli.py:test_parser()", "cli.py:build_parser()", "reference", 1),
]


def _graph(make_graph, tmp_path, write_files):
    write_files(tmp_path, {"cli.py": CLI})
    return make_graph(NODES, EDGES)


def test_references_inside_set_defaults_are_registrations(
    make_graph, tmp_path, write_files
):
    graph = _graph(make_graph, tmp_path, write_files)

    # A handler on a continuation line or in a loop's table counts; the calls
    # of ``run_index`` and ``make_name`` do not.
    assert registration_references(graph, tmp_path) == {0, 1, 3, 5}


def test_building_a_parser_does_not_reach_its_handlers(
    make_graph, tmp_path, write_files
):
    graph = ImpactGraph(_graph(make_graph, tmp_path, write_files), tmp_path)

    # ``run_wiki`` and ``run_status`` are only registered.
    assert graph.impact({"cli.py": {18}}).tests == []
    assert graph.impact({"cli.py": {22}}).tests == []
    # ``build_parser`` calls ``run_index`` and ``make_name`` itself.
    for line, call_line in ((14, 8), (26, 9)):
        impact = graph.impact({"cli.py": {line}})
        assert [(t.test_id, t.depth) for t in impact.tests] == [
            ("tests/test_cli.py::test_parser", 2)
        ]
        assert impact.tests[0].path[1].call_line == call_line


def test_a_graph_without_anchors_has_no_registrations(make_graph, tmp_path):
    graph = make_graph(NODES, EDGES)
    del graph.es["anchor_line"]

    assert registration_references(graph, tmp_path) == set()
