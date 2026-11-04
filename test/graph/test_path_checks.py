# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for call-path rules over hand-built graphs."""

from __future__ import annotations

import re
import textwrap

import igraph as ig
import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.graph.path_checks import (
    PathRule,
    PathRuleError,
    check_paths,
    parse_changed_lines,
    parse_rules,
)

RULE = PathRule(id="guarded", to=("sink",), through=("guard",))


def _graph(nodes, edges):
    """``nodes``: (name, type, file, start, end); ``edges``: (source, target, line)."""

    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(len(nodes))
    gg.vs["name"] = [n[0] for n in nodes]
    gg.vs["unified_name"] = [n[0] for n in nodes]
    gg.vs["type"] = [n[1] for n in nodes]
    gg.vs["file"] = [n[2] for n in nodes]
    gg.vs["start_line"] = [n[3] for n in nodes]
    gg.vs["end_line"] = [n[4] for n in nodes]
    index = {n[0]: i for i, n in enumerate(nodes)}
    gg.add_edges([(index[s], index[t]) for s, t, _ in edges])
    gg.es["type"] = ["reference"] * len(edges)
    gg.es["anchor_file"] = [nodes[index[s]][2] for s, _, _ in edges]
    gg.es["anchor_line"] = [line for _, _, line in edges]
    g.graph = gg
    g.name_to_vertex = index
    return g


def _fn(name, file="a.py", start=None, end=None):
    return (f"{file}:{name}()", "function", file, start, end)


def _names(violation):
    return [hop.name.split(":")[-1] for hop in violation.path]


def _check(graph, tmp_path, rule=RULE, changed=None):
    report = check_paths(graph, [rule], tmp_path, changed)
    return report.rules[0]


def test_every_path_through_the_guard_passes(tmp_path):
    graph = _graph(
        [_fn("main"), _fn("guard"), _fn("sink")],
        [("a.py:main()", "a.py:guard()", 1), ("a.py:guard()", "a.py:sink()", 2)],
    )
    assert _check(graph, tmp_path).passed


def test_an_unguarded_entry_is_reported_with_its_shortest_path(tmp_path):
    graph = _graph(
        [_fn("api"), _fn("helper", start=4, end=9), _fn("guard"), _fn("sink")],
        [
            ("a.py:api()", "a.py:helper()", 1),
            ("a.py:helper()", "a.py:sink()", 6),
            ("a.py:guard()", "a.py:sink()", 12),
        ],
    )
    result = _check(graph, tmp_path)
    assert not result.passed
    (violation,) = result.violations
    assert violation.caller == "a.py:helper()" and violation.resolved
    assert (violation.file, violation.line) == ("a.py", 7)
    assert _names(violation) == ["api()", "helper()", "sink()"]


def test_a_caller_that_also_calls_a_sanitizer_counts_as_guarded(tmp_path):
    nodes = [_fn("api"), _fn("helper"), _fn("make_guard"), _fn("sink")]
    edges = [("a.py:api()", "a.py:helper()", 1), ("a.py:helper()", "a.py:sink()", 2)]
    rule = PathRule(id="sanitized", to=("sink",), or_calls=("make_guard",))
    assert not _check(_graph(nodes, edges), tmp_path, rule).passed
    edges.append(("a.py:helper()", "a.py:make_guard()", 2))
    assert _check(_graph(nodes, edges), tmp_path, rule).passed


def test_excluded_callers_leave_their_callees_as_entry_points(tmp_path):
    graph = _graph(
        [_fn("test_it", "tests/test_a.py"), _fn("helper"), _fn("guard"), _fn("sink")],
        [
            ("tests/test_a.py:test_it()", "a.py:helper()", 1),
            ("a.py:guard()", "a.py:helper()", 1),
            ("a.py:helper()", "a.py:sink()", 2),
        ],
    )
    assert _names(_check(graph, tmp_path).violations[0])[0] == "test_it()"
    rule = PathRule(id="guarded", to=("sink",), through=("guard",), exclude=("tests",))
    assert _check(graph, tmp_path, rule).passed  # only the guard calls helper now
    graph = _graph(
        [_fn("test_it", "tests/test_a.py"), _fn("helper"), _fn("guard"), _fn("sink")],
        [
            ("tests/test_a.py:test_it()", "a.py:helper()", 1),
            ("a.py:helper()", "a.py:sink()", 2),
        ],
    )
    assert _names(_check(graph, tmp_path, rule).violations[0]) == ["helper()", "sink()"]


SOURCE = textwrap.dedent(
    """\
    def guard():
        sink()

    def run(host):
        return host.registry.sink(
            1)

    def sink():
        pass
    """
)


def _source_graph(extra_edges=()):
    nodes = [
        ("a.py", "file", None, None, None),
        _fn("guard", start=0, end=1),
        _fn("run", start=3, end=5),
        _fn("sink", start=7, end=8),
        _fn("other.sink", "b.py", 0, 1),
    ]
    edges = [("a.py:guard()", "a.py:sink()", 1), *extra_edges]
    return _graph(nodes, edges)


def test_an_unresolved_python_call_to_the_target_is_matched_by_name(tmp_path):
    (tmp_path / "a.py").write_text(SOURCE)
    rule = PathRule(id="guarded", to=("a.py:sink",), through=("guard",))
    result = _check(_source_graph(), tmp_path, rule)
    (violation,) = result.violations
    assert violation.caller == "a.py:run()" and violation.resolved is False
    assert (violation.file, violation.line) == ("a.py", 5)  # the line of `.sink`
    assert [hop.resolved for hop in violation.path] == [True, False]


def test_a_call_resolved_to_another_symbol_is_not_matched_by_name(tmp_path):
    (tmp_path / "a.py").write_text(SOURCE)
    rule = PathRule(id="guarded", to=("a.py:sink",), through=("guard",))
    graph = _source_graph([("a.py:run()", "b.py:other.sink()", 4)])
    assert _check(graph, tmp_path, rule).passed


def test_unresolved_callers_are_followed_when_resolved_ones_all_pass(tmp_path):
    (tmp_path / "a.py").write_text(
        "def api():\n    hidden.helper()\n\ndef helper():\n    sink()\n"
    )
    graph = _graph(
        [
            _fn("api", start=0, end=1),
            _fn("helper", start=3, end=4),
            _fn("guard"),
            _fn("sink"),
        ],
        [("a.py:guard()", "a.py:helper()", 20), ("a.py:helper()", "a.py:sink()", 4)],
    )
    (violation,) = _check(graph, tmp_path).violations
    assert _names(violation) == ["api()", "helper()", "sink()"]
    assert [hop.resolved for hop in violation.path] == [True, False, True]


def test_a_diff_lists_only_the_violations_it_touches(tmp_path):
    graph = _graph(
        [
            _fn("api", start=0, end=2),
            _fn("helper", start=4, end=9),
            _fn("guard"),
            _fn("sink"),
        ],
        [("a.py:api()", "a.py:helper()", 1), ("a.py:helper()", "a.py:sink()", 6)],
    )
    result = _check(graph, tmp_path, changed={"a.py": {2}})
    assert result.violations[0].changed is True
    result = _check(graph, tmp_path, changed={"a.py": {30}, "b.py": {1}})
    assert result.passed and result.unchanged_violations == 1


def test_distinct_rule_targets_do_not_guard_each_other(tmp_path):
    graph = _graph(
        [_fn("execute"), _fn("perform"), _fn("guard")],
        [("a.py:execute()", "a.py:perform()", 1)],
    )
    rule = PathRule(id="multi", to=("execute", "perform"), through=("guard",))
    (violation,) = _check(graph, tmp_path, rule).violations
    assert _names(violation) == ["execute()", "perform()"]


@pytest.mark.parametrize("first_read", ["other.sink()", "callback = other.sink"])
def test_a_same_line_reference_cannot_hide_another_unresolved_call(
    tmp_path, first_read
):
    (tmp_path / "a.py").write_text(
        f"def run(other, host):\n    {first_read}; host.sink()\n\n"
        "def sink():\n    pass\n\ndef guard():\n    pass\n"
    )
    graph = _graph(
        [
            _fn("run", start=0, end=1),
            _fn("sink", start=3, end=4),
            _fn("guard", start=6, end=7),
            _fn("Other.sink", "b.py", 0, 1),
        ],
        [("a.py:run()", "b.py:Other.sink()", 1)],
    )
    rule = PathRule(id="same-line", to=("a.py:sink",), through=("guard",))
    (violation,) = _check(graph, tmp_path, rule).violations
    assert not violation.resolved and violation.caller == "a.py:run()"


def test_changed_region_includes_unresolved_callers_beyond_the_indexed_witness(
    tmp_path,
):
    (tmp_path / "a.py").write_text(
        "def old():\n    helper()\n\ndef fresh(host):\n    host.helper()\n\n"
        "def helper():\n    sink()\n\ndef guard():\n    pass\n\n"
        "def sink():\n    pass\n"
    )
    graph = _graph(
        [
            _fn("old", start=0, end=1),
            _fn("fresh", start=3, end=4),
            _fn("helper", start=6, end=7),
            _fn("guard", start=9, end=10),
            _fn("sink", start=12, end=13),
        ],
        [("a.py:old()", "a.py:helper()", 1), ("a.py:helper()", "a.py:sink()", 7)],
    )
    result = _check(graph, tmp_path, changed={"a.py": {5}})
    (violation,) = result.violations
    assert violation.changed is True and result.unchanged_violations == 0
    assert _names(violation) == ["old()", "helper()", "sink()"]


@pytest.mark.parametrize(
    "symbol, message",
    [("run", "found a.py:A.run(), a.py:B.run()"), ("missing", "found none")],
)
def test_rule_symbols_must_name_exactly_one_node(tmp_path, symbol, message):
    graph = _graph(
        [_fn("A.run"), _fn("B.run"), _fn("guard"), _fn("sink")],
        [("a.py:A.run()", "a.py:sink()", 1)],
    )
    rule = PathRule(id="guarded", to=("sink",), through=(symbol,))
    with pytest.raises(PathRuleError, match=re.escape(message)):
        _check(graph, tmp_path, rule)
    rule = PathRule(id="guarded", to=("sink",), through=("A.run",))
    assert _check(graph, tmp_path, rule).passed


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "exactly the keys"),
        ({"version": 2, "rules": []}, "unsupported rules version"),
        ({"version": 1, "rules": []}, "nonempty list"),
        ({"version": 1, "rules": [{"id": "Bad Id", "to": ["x"]}]}, "id must match"),
        (
            {"version": 1, "rules": [{"id": "r", "to": [], "through": ["g"]}]},
            "to needs",
        ),
        ({"version": 1, "rules": [{"id": "r", "to": ["x"]}]}, "give through"),
        ({"version": 1, "rules": [{"id": "r", "to": "x", "through": ["g"]}]}, "list"),
        ({"version": 1, "rules": [{"id": "r", "to": ["x"], "via": ["g"]}]}, "unknown"),
        (
            {"version": 1, "rules": [{"id": "r", "to": ["x"], "through": ["g"]}] * 2},
            "duplicate",
        ),
    ],
)
def test_invalid_rules_are_rejected(document, message):
    with pytest.raises(PathRuleError, match=message):
        parse_rules(document)


@pytest.mark.parametrize(
    "pattern, path, matches",
    [
        ("tests", "tests/a.py", True),
        ("tests/", "tests/sub/a.py", True),
        ("tests", "tests_x/a.py", False),
        ("**/test_*.py", "a/b/test_c.py", True),
        ("**/test_*.py", "test_c.py", True),
        ("src/*.py", "src/a/b.py", False),
        ("./examples", "examples/a.py", True),
    ],
)
def test_exclude_patterns(tmp_path, pattern, path, matches):
    graph = _graph(
        [_fn("caller", path), _fn("guard"), _fn("sink")],
        [(f"{path}:caller()", "a.py:sink()", 1)],
    )
    rule = PathRule(id="guarded", to=("sink",), through=("guard",), exclude=(pattern,))
    assert _check(graph, tmp_path, rule).passed is matches


def test_parse_changed_lines():
    diff = textwrap.dedent(
        """\
        diff --git a/a.py b/a.py
        --- a/a.py
        +++ b/a.py
        @@ -3 +3,2 @@ def f():
        @@ -10,2 +11,0 @@ def g():
        diff --git a/new.py b/new.py
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1,3 @@
        diff --git a/gone.py b/gone.py
        --- a/gone.py
        +++ /dev/null
        @@ -1,4 +0,0 @@
        """
    )
    assert parse_changed_lines(diff) == {"a.py": {3, 4, 11, 12}, "new.py": {1, 2, 3}}
