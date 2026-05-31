# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``graph_expand`` skill executor and loader.

graph_expand is a 1-hop LSP-aligned range/symbol query built on
``CodeGraph.query_range`` / ``query_range_by_symbol``. The ``ranges`` input is
**1-based inclusive** (agent-facing); the executor converts to the graph's
0-based form internally, and converts ``start_line`` / ``end_line`` /
``anchor_line`` back to 1-based on output.

The internal builder helpers (``add_symbol_node`` / ``add_symbol_reference``)
operate on 0-based lines, so the fixtures below use 0-based spans while the
range *queries* use 1-based (0-based + 1).

- TestGraphExpandRangeInput: range-keyed queries (defined / callees / callers).
- TestGraphExpandSymbolInput: symbol-keyed queries with identity + unified fallback.
- TestGraphExpandCombinedInput: ranges + symbols union + dedup.
- TestGraphExpandFiltersAndCaps: filter_tests + top_k truncation.
- TestGraphExpandLineNumbering: explicit 1-based <-> 0-based boundary checks.
- TestGraphExpandForwardCompat: hops>1 clamp behaviour.
- TestGraphExpandLoader: smoke test that the real skill package loads.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

from codeminer.agent.skills.loader import SkillLoader
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.graph.code_graph import CodeGraph
from codeminer.ops.expand import ExpandContext


def _skill_dir() -> str:
    """Return the absolute path to the graph_expand skill package."""
    import codeminer.agent.skills as pkg

    return str(Path(pkg.__file__).parent / "graph_expand")


def _load_executor(context: Any):
    spec = importlib.util.spec_from_file_location(
        "codeminer.agent.skills.graph_expand.executor",
        os.path.join(_skill_dir(), "executor.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_executor(context)


# ---------------------------------------------------------------------------
# Fixture builders (lines are 0-based — that's what the builder API expects)
# ---------------------------------------------------------------------------


def _build_simple_graph() -> CodeGraph:
    """Two functions in foo.py with cross-references in both directions.

    foo.py:bar  defined 0-based lines 5-20, references baz at line 12
    foo.py:baz  defined 0-based lines 25-40, references bar at line 28
    """
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:bar", 10, 5, 20, "function")
    g.add_symbol_node("foo.py:baz", 30, 25, 40, "function")

    g.update_current_scope("foo.py:bar", 5, 20)
    g.add_symbol_reference("foo.py:baz", anchor_line=12)

    g.update_current_scope("foo.py:baz", 25, 40)
    g.add_symbol_reference("foo.py:bar", anchor_line=28)

    g.build_range_indexes()
    return g


def _build_graph_with_unified_collision() -> CodeGraph:
    """Two distinct identity names sharing the same unified_name (cross-file)."""
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g._add_vertex(
        "ident_a",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 0,
            "end_line": 4,
            "unified_name": "shared_name",
        },
    )
    g.add_file_node("b.py")
    g._add_vertex(
        "ident_b",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 9,
            "end_line": 13,
            "unified_name": "shared_name",
        },
    )
    g.build_range_indexes()
    return g


def _build_graph_with_test_caller() -> CodeGraph:
    """target.py:fn has one prod caller (caller.py) and one test caller (test_x.py)."""
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("target.py")
    g.add_symbol_node("target.py:fn", 5, 5, 10, "function")

    g.add_file_node("caller.py")
    g.add_symbol_node("caller.py:c", 1, 1, 20, "function")
    g.update_current_scope("caller.py:c", 1, 20)
    g.add_symbol_reference("target.py:fn", anchor_line=8)

    g.add_file_node("test_x.py")
    g.add_symbol_node("test_x.py:tc", 1, 1, 20, "function")
    g.update_current_scope("test_x.py:tc", 1, 20)
    g.add_symbol_reference("target.py:fn", anchor_line=15)

    g.build_range_indexes()
    return g


def _build_recursive_graph() -> CodeGraph:
    """foo.py:rec calls itself (self-edge); foo.py:boot also calls rec.

    Used to verify that under mode='all' the recursive vertex appears with
    BOTH defined and callees roles — dedup is keyed by (node_id, role), not
    by node_id alone. query_range excludes self-edges from `incoming`
    (invariant iii) so the caller-side surface comes from boot -> rec.
    """
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:rec", 10, 5, 20, "function")
    g.add_symbol_node("foo.py:boot", 30, 25, 30, "function")

    g.update_current_scope("foo.py:rec", 5, 20)
    g.add_symbol_reference("foo.py:rec", anchor_line=12)

    g.update_current_scope("foo.py:boot", 25, 30)
    g.add_symbol_reference("foo.py:rec", anchor_line=28)

    g.build_range_indexes()
    return g


def _build_hot_target_graph(n_callers: int) -> CodeGraph:
    """target.py:hot has many callers; used for top_k truncation tests."""
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("target.py")
    g.add_symbol_node("target.py:hot", 1, 1, 5, "function")
    for i in range(n_callers):
        f = f"caller_{i}.py"
        g.add_file_node(f)
        sym = f"{f}:c"
        g.add_symbol_node(sym, 1, 1, 5, "function")
        g.update_current_scope(sym, 1, 5)
        g.add_symbol_reference("target.py:hot", anchor_line=3)
    g.build_range_indexes()
    return g


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


class TestGraphExpandRangeInput:
    """Range-keyed query path: `ranges=[{file, start_line, end_line}, ...]` (1-based)."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    # --- error paths ---

    def test_raises_when_graph_is_none(self):
        execute = _load_executor(ExpandContext(code_graph=None))
        with pytest.raises(RuntimeError, match="Symbol graph not available"):
            execute(ranges=[{"file": "foo.py", "start_line": 1, "end_line": 10}])

    def test_raises_when_neither_ranges_nor_symbols(self):
        execute = self._execute(_build_simple_graph())
        with pytest.raises(ValueError, match="needs either"):
            execute()

    def test_raises_on_invalid_mode(self):
        execute = self._execute(_build_simple_graph())
        with pytest.raises(ValueError, match="mode must be one of"):
            execute(
                ranges=[{"file": "foo.py", "start_line": 1, "end_line": 6}],
                mode="bogus",
            )

    def test_raises_on_unbuilt_range_indexes(self):
        g = CodeGraph()
        g.add_file_node("foo.py")
        execute = self._execute(g)
        with pytest.raises(RuntimeError, match="Range indexes empty"):
            execute(ranges=[{"file": "foo.py", "start_line": 1, "end_line": 6}])

    # --- mode coverage ---

    def test_defined_returns_overlapping_symbol(self):
        execute = self._execute(_build_simple_graph())
        # 1-based query 9-16 overlaps bar's 0-based span 5-20.
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 9, "end_line": 16}],
            mode="defined",
        )
        names = {r.node_name for r in results}
        assert "foo.py:bar" in names
        assert all(r.role == "defined" for r in results)
        # defined results carry no edge metadata
        assert all(r.edge_kind is None and r.anchor_line is None for r in results)

    def test_callees_returns_target_of_outgoing_edge(self):
        execute = self._execute(_build_simple_graph())
        # 1-based 6-21 == bar's 0-based span 5-20.
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            mode="callees",
        )
        names = {r.node_name for r in results}
        assert "foo.py:baz" in names
        assert all(r.role == "callees" for r in results)
        baz = next(r for r in results if r.node_name == "foo.py:baz")
        # anchor 0-based 12 -> 1-based 13 on output.
        assert baz.anchor_line == 13
        assert baz.edge_kind is not None

    def test_callers_returns_source_of_incoming_edge(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            mode="callers",
        )
        names = {r.node_name for r in results}
        assert "foo.py:baz" in names
        assert all(r.role == "callers" for r in results)
        baz = next(r for r in results if r.node_name == "foo.py:baz")
        # baz calls bar at 0-based 28 -> 1-based 29.
        assert baz.anchor_line == 29

    def test_mode_all_concatenates_three_roles_ordered(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            mode="all",
        )
        roles = [r.role for r in results]
        assert roles.count("defined") >= 1
        assert roles.count("callees") >= 1
        assert roles.count("callers") >= 1
        # role ordering invariant: defined < callees < callers
        first_callees = roles.index("callees")
        first_callers = roles.index("callers")
        last_defined = len(roles) - 1 - roles[::-1].index("defined")
        assert last_defined < first_callees
        assert first_callees <= first_callers

    # --- input normalization ---

    def test_normalizes_dotslash_path_prefix(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "./foo.py", "start_line": 9, "end_line": 16}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)

    def test_swaps_inverted_line_range(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 16, "end_line": 9}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)

    def test_unknown_file_returns_empty(self):
        execute = self._execute(_build_simple_graph())
        assert (
            execute(ranges=[{"file": "missing.py", "start_line": 1, "end_line": 99}])
            == []
        )

    def test_skips_malformed_range_entries(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[
                {"file": "foo.py"},
                {"start_line": 1, "end_line": 6},
                {"file": "foo.py", "start_line": 9, "end_line": 16},
            ],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)

    def test_skips_non_integer_line_numbers(self):
        # LLM-generated JSON occasionally puts strings/floats/lists into
        # numeric fields. Drop those entries, don't crash, but keep valid ones.
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[
                {"file": "foo.py", "start_line": "abc", "end_line": 16},
                {"file": "foo.py", "start_line": 9, "end_line": [16]},
                {"file": "foo.py", "start_line": 9, "end_line": 16},  # valid
            ],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)

    # --- multi-range path ---

    def test_multiple_ranges_union_results(self):
        execute = self._execute(_build_simple_graph())
        # Two non-overlapping ranges, each defines a distinct symbol.
        results = execute(
            ranges=[
                {"file": "foo.py", "start_line": 6, "end_line": 11},
                {"file": "foo.py", "start_line": 26, "end_line": 31},
            ],
            mode="defined",
        )
        names = {r.node_name for r in results}
        assert names == {"foo.py:bar", "foo.py:baz"}


class TestGraphExpandSymbolInput:
    """Symbol-keyed query path: `symbols=[name, ...]` via query_range_by_symbol."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    def test_resolves_identity_directly(self):
        execute = self._execute(_build_simple_graph())
        results = execute(symbols=["foo.py:bar"], mode="defined")
        assert any(r.node_name == "foo.py:bar" for r in results)

    def test_merges_unified_name_collision_candidates(self):
        execute = self._execute(_build_graph_with_unified_collision())
        # `shared_name` maps to two identities (a.py and b.py); both must be
        # returned even though they share a display name.
        results = execute(symbols=["shared_name"], mode="defined")
        files = {r.file for r in results}
        assert files == {"a.py", "b.py"}, f"expected both files, got {files}"
        assert len(results) == 2

    def test_unknown_symbol_returns_empty(self):
        execute = self._execute(_build_simple_graph())
        assert execute(symbols=["not_a_real_symbol"]) == []

    def test_empty_string_symbol_silently_dropped(self):
        execute = self._execute(_build_simple_graph())
        assert execute(symbols=["", "   ", None]) == []

    def test_non_string_symbols_silently_filtered(self):
        # Defensive: LLM might pass mixed types via tool-call JSON. The
        # isinstance(s, str) gate must drop anything non-string without raising.
        execute = self._execute(_build_simple_graph())
        results = execute(
            symbols=[123, None, {}, ["nested"], "foo.py:bar"], mode="defined"
        )
        names = {r.node_name for r in results}
        assert names == {"foo.py:bar"}


class TestGraphExpandCombinedInput:
    """Both `ranges` and `symbols` provided — union, dedup by (node_id, role)."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    def test_ranges_and_symbols_union(self):
        execute = self._execute(_build_simple_graph())
        # Range hits bar; symbol hits baz directly.
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 11}],
            symbols=["foo.py:baz"],
            mode="defined",
        )
        names = {r.node_name for r in results}
        assert "foo.py:bar" in names
        assert "foo.py:baz" in names

    def test_dedups_when_range_and_symbol_overlap(self):
        execute = self._execute(_build_simple_graph())
        # Both inputs resolve to bar; should appear once.
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            symbols=["foo.py:bar"],
            mode="defined",
        )
        bar_count = sum(1 for r in results if r.node_name == "foo.py:bar")
        assert bar_count == 1

    def test_ranges_and_symbols_across_different_files(self):
        # ranges hit a symbol in file A; symbols pick one in file B. The union
        # must contain both; dedup shouldn't collapse symbols across files.
        execute = self._execute(_build_graph_with_test_caller())
        results = execute(
            ranges=[{"file": "target.py", "start_line": 6, "end_line": 11}],
            symbols=["caller.py:c"],
            mode="defined",
            filter_tests=False,
        )
        names = {r.node_name for r in results}
        assert "target.py:fn" in names
        assert "caller.py:c" in names

    def test_recursive_vertex_appears_in_both_roles(self):
        # Dedup key is (node_id, role), NOT node_id alone — a recursive
        # function (rec calls itself) must surface as BOTH defined and callee.
        execute = self._execute(_build_recursive_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            mode="all",
            filter_tests=False,
        )
        rec_entries = [r for r in results if r.node_name == "foo.py:rec"]
        roles_for_rec = {r.role for r in rec_entries}
        assert {"defined", "callees"}.issubset(
            roles_for_rec
        ), f"recursive rec missing expected roles: {roles_for_rec}"
        # boot shows up as a caller (incoming edge into rec from outside).
        assert any(
            r.node_name == "foo.py:boot" and r.role == "callers" for r in results
        )

    def test_dedup_preserves_first_seed_anchor(self):
        # When two seed ranges both surface the same callee, dedup keeps the
        # FIRST occurrence — the second seed's anchor is invisible (documented
        # in skill.md).
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[
                {"file": "foo.py", "start_line": 6, "end_line": 16},
                {"file": "foo.py", "start_line": 11, "end_line": 21},
            ],
            mode="callees",
            filter_tests=False,
        )
        baz_entries = [r for r in results if r.node_name == "foo.py:baz"]
        assert len(baz_entries) == 1
        # Anchor is the call-site line (0-based 12 -> 1-based 13).
        assert baz_entries[0].anchor_line == 13


class TestGraphExpandFiltersAndCaps:
    """`filter_tests` exclusion and `top_k` truncation."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    def test_filter_tests_excludes_test_callers_by_default(self):
        execute = self._execute(_build_graph_with_test_caller())
        results = execute(
            ranges=[{"file": "target.py", "start_line": 6, "end_line": 11}],
            mode="callers",
        )
        names = {r.node_name for r in results}
        assert "caller.py:c" in names
        assert "test_x.py:tc" not in names

    def test_filter_tests_false_keeps_test_callers(self):
        execute = self._execute(_build_graph_with_test_caller())
        results = execute(
            ranges=[{"file": "target.py", "start_line": 6, "end_line": 11}],
            mode="callers",
            filter_tests=False,
        )
        names = {r.node_name for r in results}
        assert "caller.py:c" in names
        assert "test_x.py:tc" in names

    def test_top_k_truncation_appends_sentinel(self):
        execute = self._execute(_build_hot_target_graph(n_callers=20))
        results = execute(
            ranges=[{"file": "target.py", "start_line": 2, "end_line": 6}],
            mode="callers",
            top_k=5,
            filter_tests=False,
        )
        assert len(results) == 6  # 5 real + 1 sentinel
        sentinel = results[-1]
        assert sentinel.node_name == "__truncated__"
        assert sentinel.type == "meta"
        assert "truncated" in (sentinel.content or "")

    def test_top_k_no_truncation_when_within_cap(self):
        execute = self._execute(_build_hot_target_graph(n_callers=3))
        results = execute(
            ranges=[{"file": "target.py", "start_line": 2, "end_line": 6}],
            mode="callers",
            top_k=10,
            filter_tests=False,
        )
        assert all(r.node_name != "__truncated__" for r in results)
        assert len(results) == 3


class TestGraphExpandLineNumbering:
    """Explicit checks for the 1-based (agent) <-> 0-based (graph) boundary."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    def test_defined_span_returned_one_based(self):
        # bar's 0-based span is 5-20 -> output start/end must be 6/21.
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 9, "end_line": 9}],
            mode="defined",
        )
        bar = next(r for r in results if r.node_name == "foo.py:bar")
        assert bar.start_line == 6
        assert bar.end_line == 21

    def test_one_based_query_excludes_off_by_one_neighbor(self):
        # bar occupies 0-based 5-20 (1-based 6-21). A 1-based query of just
        # line 5 (0-based 4) must NOT overlap bar.
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 5}],
            mode="defined",
        )
        assert all(r.node_name != "foo.py:bar" for r in results)
        # But the boundary line 6 (0-based 5) does overlap.
        results2 = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 6}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results2)

    def test_zero_or_negative_line_clamped_not_underflowed(self):
        # A stray 0 / negative agent line must clamp to the start, not produce
        # a negative 0-based query that silently misses everything.
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 0, "end_line": 11}],
            mode="defined",
        )
        # 0 -> clamp to 0-based 0; range 0..10 overlaps bar (5-20).
        assert any(r.node_name == "foo.py:bar" for r in results)


class TestGraphExpandForwardCompat:
    """`hops` is reserved for future multi-hop; v1 always behaves as 1."""

    @pytest.mark.parametrize("hops_value", [0, -1, 2, 5, 100])
    def test_hops_non_one_behaves_like_one(self, hops_value):
        execute = _load_executor(ExpandContext(code_graph=_build_simple_graph()))
        baseline = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            mode="defined",
            hops=1,
        )
        actual = execute(
            ranges=[{"file": "foo.py", "start_line": 6, "end_line": 21}],
            mode="defined",
            hops=hops_value,
        )
        assert [r.node_name for r in actual] == [r.node_name for r in baseline]


class TestGraphExpandLoader:
    """Smoke test: SkillLoader registers the real skill package."""

    def test_skill_package_loads(self):
        ctx = ExpandContext(code_graph=_build_simple_graph())
        skills_dir = str(Path(_skill_dir()).parent)
        loader = SkillLoader()
        loaded = loader.load_all(skills_dir, contexts={"expand": ctx})

        loaded_ids = {m.skill_id for m in loaded}
        assert "graph_expand" in loaded_ids
        assert "graph_locate" not in loaded_ids  # never landed / folded in

        meta = SkillRegistry().get("graph_expand")
        assert meta is not None
        assert meta.executor_fn is not None
        results = meta.executor_fn(
            ranges=[{"file": "foo.py", "start_line": 9, "end_line": 16}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)
