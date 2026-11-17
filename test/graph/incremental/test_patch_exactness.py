# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A patched graph has to be the graph a fresh build gives.

Each test is one way it was not, found by comparing the two on real commits.
"""

from __future__ import annotations

import subprocess

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.graph.incremental import change_mgr
from codenib.graph.incremental.patcher_base import IncrementalPatchRebuildRequired
from codenib.graph.incremental.patcher_python import PatcherPython
from codenib.types import EDGE_TYPE_REFERENCE


def _git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )


def _commit(root, files, message):
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        message,
    )


def _lines(count, changes=None):
    lines = [f"line{index}" for index in range(count)]
    for index, text in (changes or {}).items():
        lines[index] = text
    return "\n".join(lines) + "\n"


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_line_edits_give_the_offset_of_every_later_line(repo):
    _commit(repo, {"a.py": _lines(30)}, "base")
    # Two lines inserted before index 5, line 10 replaced by three, 20 removed.
    after = _lines(30).splitlines()
    del after[20]
    after[10:11] = ["x", "y", "z"]
    after[5:5] = ["new", "new"]
    _commit(repo, {"a.py": "\n".join(after) + "\n"}, "edit")

    edits = change_mgr.get_line_edits(str(repo), "a.py", "HEAD~1", "HEAD")

    assert edits == [(5, 5, 2), (10, 11, 2), (20, 21, -1)]
    shift = change_mgr.shift_of_range
    assert shift(edits, 0, 4) == 0
    assert shift(edits, 5, 9) == 2  # the insertion goes before line 5
    assert shift(edits, 11, 19) == 4
    assert shift(edits, 21, 29) == 3
    # A range holding an edit did not simply move.
    assert shift(edits, 8, 12) is None
    assert shift(edits, 3, 6) is None
    assert shift(edits, 20, 20) is None


def _graph(root, definitions, edges=()):
    """``definitions``: (name, unified_name, start, end). File ``a.py`` only."""

    graph = CodeGraph(str(root))
    graph.add_file_node("a.py")
    graph.add_file_node("b.py")
    graph._add_vertex(
        "b.py:target()",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 0,
            "end_line": 1,
            "unified_name": "b.py:target()",
        },
    )
    for name, unified, start, end in definitions:
        graph._add_vertex(
            name,
            {
                "type": "function",
                "file": "a.py",
                "start_line": start,
                "end_line": end,
                "selection_line": start,
                "unified_name": unified,
            },
        )
    for source, line in edges:
        graph._add_edge(
            source,
            "b.py:target()",
            EDGE_TYPE_REFERENCE,
            anchor_file="a.py",
            anchor_line=line,
        )
    graph.build_range_indexes()
    return graph


def _anchors(graph, name):
    vertex = graph.name_to_vertex[name]
    return sorted(
        graph.graph.es[edge]["anchor_line"]
        for edge in graph.graph.incident(vertex, mode="out")
        if graph.graph.es[edge]["type"] == EDGE_TYPE_REFERENCE
    )


def test_file_outside_the_build_is_not_patched_in(repo):
    # requests limits its analyser to src/requests, so tests/ has no vertex.
    # Patching a changed test file in added 1,177 vertices no build has.
    graph = _graph(repo, [("a.py:f()", "a.py:f()", 0, 3)])
    patcher = PatcherPython(str(repo), graph, lsp_client=object())

    kept = patcher._filter_changed_files(
        {
            "modified": ["a.py", "tests/test_a.py"],
            "added": ["new.py"],
            "deleted": ["b.py", "tests/old.py"],
            "renamed": [("a.py", "c.py"), ("tests/x.py", "tests/y.py")],
        }
    )

    assert kept == {
        "modified": ["a.py"],
        "added": ["new.py"],
        "deleted": ["b.py"],
        "renamed": [("a.py", "c.py")],
    }


def test_overloads_move_with_the_code_above_them(repo):
    # Two definitions share one unified name, so only one is tracked. The
    # other stayed at its old lines, with the anchors of its references.
    graph = _graph(
        repo,
        [
            ("a.py:read()#1", "a.py:read()", 10, 12),
            ("a.py:read()#2", "a.py:read()", 14, 18),
        ],
        edges=[("a.py:read()#1", 11), ("a.py:read()#2", 16)],
    )
    patcher = PatcherPython(str(repo), graph, lsp_client=object())
    patcher.build_indexes()
    tracked = {
        "a.py:read()": {
            "vertex_name": "a.py:read()#2",
            "start_line": 14,
            "end_line": 18,
        }
    }

    moved = patcher._shift_same_named_definitions("a.py", tracked, [(5, 5, 2)])

    assert moved == 1
    first = graph.graph.vs[graph.name_to_vertex["a.py:read()#1"]]
    assert (first["start_line"], first["end_line"], first["selection_line"]) == (
        12,
        14,
        12,
    )
    assert _anchors(graph, "a.py:read()#1") == [13]
    # The tracked definition is left to the normal path.
    assert _anchors(graph, "a.py:read()#2") == [16]


def test_edited_overload_is_handed_back_for_a_rebuild(repo):
    graph = _graph(
        repo,
        [
            ("a.py:read()#1", "a.py:read()", 10, 12),
            ("a.py:read()#2", "a.py:read()", 14, 18),
        ],
    )
    patcher = PatcherPython(str(repo), graph, lsp_client=object())
    patcher.build_indexes()
    tracked = {
        "a.py:read()": {
            "vertex_name": "a.py:read()#2",
            "start_line": 14,
            "end_line": 18,
        }
    }

    with pytest.raises(IncrementalPatchRebuildRequired, match="more than once"):
        patcher._shift_same_named_definitions("a.py", tracked, [(11, 12, 0)])


def test_untouched_references_of_a_changed_body_are_kept(repo):
    # The body grew by two lines at its line 20. References on untouched
    # lines keep what the builder found; only the edited line is dropped
    # for the language server to look up again.
    graph = _graph(
        repo,
        [("a.py:f()", "a.py:f()", 10, 30)],
        edges=[("a.py:f()", 12), ("a.py:f()", 20), ("a.py:f()", 25)],
    )
    patcher = PatcherPython(str(repo), graph, lsp_client=object())
    patcher.build_indexes()

    moved, dropped = patcher.rebase_outgoing_anchor_lines(
        "a.py:f()", "a.py", [(20, 21, 2)]
    )

    assert (moved, dropped) == (1, 1)
    assert _anchors(graph, "a.py:f()") == [12, 27]


def test_module_variables_and_top_level_references_move(repo):
    # ``documentSymbol`` lists neither a module variable nor the references
    # made by top-level code, so both stayed at their old lines.
    graph = _graph(repo, [("a.py:LIMIT", "a.py:LIMIT", 12, 12)])
    for line in (3, 8, 20, 25):
        graph._add_edge(
            "a.py",
            "b.py:target()",
            EDGE_TYPE_REFERENCE,
            anchor_file="a.py",
            anchor_line=line,
        )
    graph.build_range_indexes()
    patcher = PatcherPython(str(repo), graph, lsp_client=object())
    patcher.build_indexes()
    old = {
        "a.py:LIMIT": {"vertex_name": "a.py:LIMIT", "start_line": 12, "end_line": 12}
    }
    # Two lines inserted before 5; line 8 replaced by one line; line 20 by three.
    edits = [(5, 5, 2), (8, 9, 0), (20, 21, 2)]

    moved = patcher._move_unlisted_code("a.py", ["a.py:LIMIT"], old, edits)

    assert moved == 1
    variable = graph.graph.vs[graph.name_to_vertex["a.py:LIMIT"]]
    assert (variable["start_line"], variable["selection_line"]) == (14, 14)
    # Line 8 was replaced one for one, so its reference stays on that line;
    # line 20 changed length, so what it referred to is no longer known.
    assert _anchors(graph, "a.py") == [3, 10, 29]


def test_shift_of_line_keeps_a_line_replaced_one_for_one():
    edits = [(5, 5, 2), (8, 10, 0), (20, 21, 2), (30, 31, -1)]
    shift = change_mgr.shift_of_line

    assert [shift(edits, line) for line in (4, 5, 8, 9, 10, 19)] == [0, 2, 2, 2, 2, 2]
    assert shift(edits, 20) is None  # replaced by three lines
    assert shift(edits, 30) is None  # removed
    assert shift(edits, 31) == 3


def test_fact_projection_compares_facts_not_vertex_keys(repo):
    from codenib.compiler.verification import FactProjectionVerifier

    fresh = _graph(repo, [("a.py:f()", "a.py:f()", 10, 30)], edges=[("a.py:f()", 12)])
    # The patcher keys a moved definition by name and line.
    patched = _graph(
        repo, [("a.py:f():10", "a.py:f()", 10, 30)], edges=[("a.py:f():10", 12)]
    )
    verifier = FactProjectionVerifier()

    same = verifier.verify(index_type="symbol_graph", patched=patched, fresh=fresh)
    assert (same.verified, same.checked) == (True, True)

    stale = _graph(
        repo, [("a.py:f():10", "a.py:f()", 10, 30)], edges=[("a.py:f():10", 14)]
    )
    differs = verifier.verify(index_type="symbol_graph", patched=stale, fresh=fresh)
    assert (differs.verified, differs.checked) == (False, True)
    assert differs.details["edges_missing"] == 1
    assert differs.details["edges_extra"] == 1

    unchecked = verifier.verify(index_type="symbol_graph", patched=patched)
    assert (unchecked.verified, unchecked.checked) == (False, False)
