# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``graph_expand`` skill executor and loader.

- TestGraphExpandRangeInput: range-keyed queries (defined / callees / callers).
- TestGraphExpandSymbolInput: symbol-keyed queries with identity + unified fallback.
- TestGraphExpandCombinedInput: ranges + symbols union + dedup.
- TestGraphExpandForwardCompat: hops>1 clamp behaviour.
- TestGraphExpandLoader: smoke test that the real skill package loads.
- TestGraphExpandLanguageMatrix: integration-tier smoke against real prebuilt
  v3 graphs in ``/mnt/data/codeminer`` for all 5 target languages.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Dict, Set

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
# Fixture builders
# ---------------------------------------------------------------------------


def _build_simple_graph() -> CodeGraph:
    """Two functions in foo.py with cross-references in both directions.

    foo.py:bar  defined lines 5-20, references baz at line 12
    foo.py:baz  defined lines 25-40, references bar at line 28
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
            "start_line": 1,
            "end_line": 5,
            "unified_name": "shared_name",
        },
    )
    g.add_file_node("b.py")
    g._add_vertex(
        "ident_b",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 10,
            "end_line": 14,
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
    """Range-keyed query path: `ranges=[{file, start_line, end_line}, ...]`."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    # --- error paths ---

    def test_raises_when_graph_is_none(self):
        execute = _load_executor(ExpandContext(code_graph=None))
        with pytest.raises(RuntimeError, match="Symbol graph not available"):
            execute(ranges=[{"file": "foo.py", "start_line": 0, "end_line": 10}])

    def test_raises_when_neither_ranges_nor_symbols(self):
        execute = self._execute(_build_simple_graph())
        with pytest.raises(ValueError, match="needs either"):
            execute()

    def test_raises_on_invalid_mode(self):
        execute = self._execute(_build_simple_graph())
        with pytest.raises(ValueError, match="mode must be one of"):
            execute(
                ranges=[{"file": "foo.py", "start_line": 0, "end_line": 5}],
                mode="bogus",
            )

    def test_raises_on_unbuilt_range_indexes(self):
        g = CodeGraph()
        g.add_file_node("foo.py")
        execute = self._execute(g)
        with pytest.raises(RuntimeError, match="Range indexes empty"):
            execute(ranges=[{"file": "foo.py", "start_line": 0, "end_line": 5}])

    # --- mode coverage ---

    def test_defined_returns_overlapping_symbol(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 8, "end_line": 15}],
            mode="defined",
        )
        names = {r.node_name for r in results}
        assert "foo.py:bar" in names
        assert all(r.role == "defined" for r in results)
        # defined results carry no edge metadata
        assert all(r.edge_kind is None and r.anchor_line is None for r in results)

    def test_callees_returns_target_of_outgoing_edge(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 20}],
            mode="callees",
        )
        names = {r.node_name for r in results}
        assert "foo.py:baz" in names
        assert all(r.role == "callees" for r in results)
        # callees carry the call-site anchor (in source range)
        baz = next(r for r in results if r.node_name == "foo.py:baz")
        assert baz.anchor_line == 12
        assert baz.edge_kind is not None

    def test_callers_returns_source_of_incoming_edge(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 20}],
            mode="callers",
        )
        names = {r.node_name for r in results}
        assert "foo.py:baz" in names
        assert all(r.role == "callers" for r in results)
        # callers carry the caller's anchor_line (where it called into bar)
        baz = next(r for r in results if r.node_name == "foo.py:baz")
        assert baz.anchor_line == 28

    def test_mode_all_concatenates_three_roles_ordered(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 20}],
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
            ranges=[{"file": "./foo.py", "start_line": 8, "end_line": 15}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)

    def test_swaps_inverted_line_range(self):
        execute = self._execute(_build_simple_graph())
        results = execute(
            ranges=[{"file": "foo.py", "start_line": 15, "end_line": 8}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)

    def test_unknown_file_returns_empty(self):
        execute = self._execute(_build_simple_graph())
        assert (
            execute(ranges=[{"file": "missing.py", "start_line": 0, "end_line": 99}])
            == []
        )

    def test_skips_malformed_range_entries(self):
        execute = self._execute(_build_simple_graph())
        # Mix of malformed (missing lines / file) and valid entries.
        results = execute(
            ranges=[
                {"file": "foo.py"},
                {"start_line": 0, "end_line": 5},
                {"file": "foo.py", "start_line": 8, "end_line": 15},
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
                {"file": "foo.py", "start_line": 5, "end_line": 10},
                {"file": "foo.py", "start_line": 25, "end_line": 30},
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
        # isinstance(s, str) gate in _parse should drop anything non-string
        # without raising.
        execute = self._execute(_build_simple_graph())
        # Only "foo.py:bar" is a valid string symbol; everything else dropped.
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
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 10}],
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
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 20}],
            symbols=["foo.py:bar"],
            mode="defined",
        )
        bar_count = sum(1 for r in results if r.node_name == "foo.py:bar")
        assert bar_count == 1

    def test_ranges_and_symbols_across_different_files(self):
        # T2: ranges hit a symbol in file A; symbols pick one in file B.
        # The union must contain both; dedup shouldn't accidentally collapse
        # symbols from different files.
        execute = self._execute(_build_graph_with_test_caller())
        results = execute(
            ranges=[{"file": "target.py", "start_line": 5, "end_line": 10}],
            symbols=["caller.py:c"],
            mode="defined",
            filter_tests=False,
        )
        names = {r.node_name for r in results}
        assert "target.py:fn" in names
        assert "caller.py:c" in names

    def test_dedup_preserves_first_seed_anchor(self):
        # T3: When two seed ranges both surface the same callee, dedup keeps
        # the FIRST occurrence — the second seed's anchor is invisible.
        # Documented as expected behaviour in skill.md.
        execute = self._execute(_build_simple_graph())
        # Two ranges both inside bar; both should query the same outgoing
        # edge (bar -> baz at anchor=12). The result must surface baz once.
        results = execute(
            ranges=[
                {"file": "foo.py", "start_line": 5, "end_line": 15},
                {"file": "foo.py", "start_line": 10, "end_line": 20},
            ],
            mode="callees",
            filter_tests=False,
        )
        baz_entries = [r for r in results if r.node_name == "foo.py:baz"]
        assert len(baz_entries) == 1
        # Anchor is the call-site line (12), preserved from the first seed.
        assert baz_entries[0].anchor_line == 12


class TestGraphExpandFiltersAndCaps:
    """`filter_tests` exclusion and `top_k` truncation."""

    def _execute(self, graph: CodeGraph):
        return _load_executor(ExpandContext(code_graph=graph))

    def test_filter_tests_excludes_test_callers_by_default(self):
        execute = self._execute(_build_graph_with_test_caller())
        results = execute(
            ranges=[{"file": "target.py", "start_line": 5, "end_line": 10}],
            mode="callers",
        )
        names = {r.node_name for r in results}
        assert "caller.py:c" in names
        assert "test_x.py:tc" not in names

    def test_filter_tests_false_keeps_test_callers(self):
        execute = self._execute(_build_graph_with_test_caller())
        results = execute(
            ranges=[{"file": "target.py", "start_line": 5, "end_line": 10}],
            mode="callers",
            filter_tests=False,
        )
        names = {r.node_name for r in results}
        assert "caller.py:c" in names
        assert "test_x.py:tc" in names

    def test_top_k_truncation_appends_sentinel(self):
        execute = self._execute(_build_hot_target_graph(n_callers=20))
        results = execute(
            ranges=[{"file": "target.py", "start_line": 1, "end_line": 5}],
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
            ranges=[{"file": "target.py", "start_line": 1, "end_line": 5}],
            mode="callers",
            top_k=10,
            filter_tests=False,
        )
        assert all(r.node_name != "__truncated__" for r in results)
        assert len(results) == 3


class TestGraphExpandForwardCompat:
    """`hops` parameter is reserved for future multi-hop; v1 always behaves as 1."""

    @pytest.mark.parametrize("hops_value", [0, -1, 2, 5, 100])
    def test_hops_non_one_behaves_like_one(self, hops_value):
        # Any non-1 value should produce the same result as hops=1, never raise.
        execute = _load_executor(ExpandContext(code_graph=_build_simple_graph()))
        baseline = execute(
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 20}],
            mode="defined",
            hops=1,
        )
        actual = execute(
            ranges=[{"file": "foo.py", "start_line": 5, "end_line": 20}],
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
        assert "graph_locate" not in loaded_ids  # removed

        meta = SkillRegistry().get("graph_expand")
        assert meta is not None
        assert meta.executor_fn is not None
        results = meta.executor_fn(
            ranges=[{"file": "foo.py", "start_line": 8, "end_line": 15}],
            mode="defined",
        )
        assert any(r.node_name == "foo.py:bar" for r in results)


# ---------------------------------------------------------------------------
# Multi-language smoke tests against prebuilt v3 graphs
# ---------------------------------------------------------------------------

# Cached graphs from the self-hosted runner under /mnt/data/codeminer. Each
# was rebuilt post-#127 so it carries schema_version=3 + populated
# `file_edge_anchors`. Auto-skip per language if the path is missing or the
# graph is pre-v3 (no anchored edges) — non-self-hosted devs aren't blocked.
_PREBUILT_GRAPHS = {
    "python": "/mnt/data/codeminer/pydata__xarray-3151/graph.pkl",
    "ts": "/mnt/data/codeminer/axios__axios-4731/graph.pkl",
    "cpp": "/mnt/data/codeminer/fmtlib__fmt-2317/graph.pkl",
    "rust": "/mnt/data/codeminer/sharkdp__bat-2201/graph.pkl",
    "go": "/mnt/data/codeminer/gin-gonic__gin-1805/graph.pkl",
}


def _load_prebuilt(lang: str) -> CodeGraph:
    path = _PREBUILT_GRAPHS[lang]
    if not os.path.exists(path):
        pytest.skip(f"prebuilt {lang} graph not available at {path}")
    try:
        g = CodeGraph.load_graph(path)
    except ValueError as exc:
        pytest.skip(f"{lang} graph at {path} unloadable: {exc}")
    n_anchors = sum(len(v) for v in (g._file_edge_anchors or {}).values())
    if n_anchors == 0:
        pytest.skip(
            f"{lang} graph at {path} has no anchored edges "
            "(pre-v3 build or build_range_indexes() never ran)"
        )
    return g


# Per-language probes — (file, range) chosen by inspecting each prebuilt graph
# for a code region with cross-file callees + same-file callers. Expected sets
# are subsets that MUST appear; upstream commits are pinned so symbols are stable.
# When the graph version changes, re-probe via /tmp/test_probe_graphs.py and
# update these constants.
_LANGUAGE_PROBES = {
    "python": {
        "file": "xarray/core/dataset.py",
        "start_line": 1882,
        "end_line": 1982,
        "expected_defined": {
            "xarray/core/dataset.py:Dataset",
            "xarray/core/dataset.py:Dataset.isel_points()",
            "xarray/core/dataset.py:Dataset.sel_points()",
        },
        "expected_callees": {
            "xarray/core/utils.py:is_scalar()",
            "xarray/core/variable.py:as_variable()",
            "xarray/core/dataset.py:Dataset.transpose()",
            "xarray/core/dataset.py:Dataset.variables()",
            "xarray/core/variable.py:Variable.dims()",
        },
        "expected_callers": {
            # inter-file (cross-module)
            "xarray/core/dataarray.py:DataArray.sel_points()",
            "xarray/core/dataarray.py:DataArray.isel_points()",
            # inter-file from tests
            "xarray/tests/test_dataset.py:TestDataset.test_sel_points()",
            "xarray/tests/test_dataset.py:TestDataset.test_isel_points()",
        },
    },
    "ts": {
        "file": "lib/cancel/CanceledError.js",
        "start_line": 13,
        "end_line": 17,
        "expected_defined": {
            "lib/cancel/CanceledError.js:CanceledError()",
        },
        "expected_callees": {
            # inter-file
            "lib/core/AxiosError.js:AxiosError()",
        },
        "expected_callers": {
            # inter-file
            "lib/cancel/CancelToken.js:CancelToken()",
            "lib/core/dispatchRequest.js:throwIfCancellationRequested()",
        },
    },
    "cpp": {
        "file": "include/fmt/format.h",
        "start_line": 1851,
        "end_line": 1891,
        "expected_defined": {
            # write() has multiple overloads in this range; dedup collapses to one.
            "include/fmt/format.h:fmt.detail.write()",
        },
        "expected_callees": {
            "include/fmt/core.h:fmt.basic_format_specs",
        },
        "expected_callers": {
            # intra-file
            "include/fmt/format.h:fmt.dynamic_formatter.format()",
            "include/fmt/format.h:fmt.detail.arg_formatter.operator()()",
            "include/fmt/format.h:fmt.to_string()",
            # inter-file
            "include/fmt/os.h:fmt.formatter<std.error_code, type-parameter-0-0>.format()",
            "include/fmt/format-inl.h:fmt.detail.format_float()",
        },
    },
    "rust": {
        # `Config` struct in src/config.rs — central data type with broad
        # cross-module usage. Picked for rich inter-file callers/callees.
        "file": "src/config.rs",
        "start_line": 33,
        "end_line": 88,
        "expected_defined": {
            "src/config.rs:Config.tab_width",
            "src/config.rs:Config.wrapping_mode",
            "src/config.rs:Config.pager",
            "src/config.rs:Config.true_color",
        },
        "expected_callees": {
            # inter-file (cross-module type references on the struct fields)
            "src/style.rs:StyleComponents",
            "src/wrapping.rs:WrappingMode",
            "src/paging.rs:PagingMode",
            "src/syntax_mapping.rs:SyntaxMapping",
            "src/line_range.rs:HighlightedLineRanges",
        },
        "expected_callers": {
            # inter-file (modules that hold or consume Config fields)
            "src/controller.rs:Controller.print_file()",
            "src/controller.rs:Controller.print_input()",
            "src/pretty_printer.rs:PrettyPrinter.line_ranges()",
            "src/pretty_printer.rs:PrettyPrinter.tab_width()",
            "src/bin/bat/app.rs:App.config()",
            "src/printer.rs:InteractivePrinter.preprocess()",
            "src/printer.rs:InteractivePrinter<Printer>.print_line()",
            "src/printer.rs:SimplePrinter<Printer>.print_line()",
        },
    },
    "go": {
        "file": "context.go",
        "start_line": 566,
        "end_line": 606,
        "expected_defined": {
            "context.go:Context.ShouldBind()",
            "context.go:Context.ShouldBindJSON()",
            "context.go:Context.ShouldBindXML()",
            "context.go:Context.ShouldBindQuery()",
            "context.go:Context.ShouldBindYAML()",
            "context.go:Context.MustBindWith()",
        },
        "expected_callees": {
            "binding/binding.go:Binding",
            "context.go:Context.ShouldBindWith()",
            "context.go:Context.AbortWithError()",
            "errors.go:Error.SetType()",
        },
        "expected_callers": {
            # inter-file
            "deprecated.go:Context.BindWith()",
            # intra-file (the Bind* family forwarding into ShouldBindWith)
            "context.go:Context.BindJSON()",
            "context.go:Context.BindQuery()",
            "context.go:Context.BindXML()",
            "context.go:Context.BindYAML()",
            "context.go:Context.Bind()",
        },
    },
}


@pytest.mark.integration
class TestGraphExpandLanguageMatrix:
    """Smoke graph_expand on real prebuilt v3 graphs across 5 target languages.

    Each language uses a fixed (file, range) probe (see ``_LANGUAGE_PROBES``)
    and asserts that a curated subset of expected unified_names appears in the
    correct role. Upstream commits are pinned in ``_PREBUILT_GRAPHS`` so the
    expected sets are stable; if a graph is regenerated against a newer
    commit, re-probe via /tmp/test_probe_graphs.py and update the constants.
    """

    def _smoke(self, lang: str, graph: CodeGraph):
        execute = _load_executor(ExpandContext(code_graph=graph))
        probe = _LANGUAGE_PROBES[lang]

        results = execute(
            ranges=[
                {
                    "file": probe["file"],
                    "start_line": probe["start_line"],
                    "end_line": probe["end_line"],
                }
            ],
            mode="all",
            top_k=200,
            filter_tests=False,
        )

        # --- shape invariants (catch silent structural breakage) ---
        assert len(results) > 0, f"{lang}: no results for {probe['file']}"
        roles = {r.role for r in results if r.node_name != "__truncated__"}
        assert roles.issubset(
            {"defined", "callees", "callers"}
        ), f"{lang}: unexpected roles: {roles}"

        for r in results:
            if r.role in ("callees", "callers"):
                assert (
                    r.anchor_file is not None
                ), f"{lang}: missing anchor_file on {r.role} {r.node_name!r}"
                assert (
                    r.anchor_line is not None
                ), f"{lang}: missing anchor_line on {r.role} {r.node_name!r}"
                assert r.edge_kind is not None
            elif r.role == "defined":
                assert r.anchor_file is None
                assert r.anchor_line is None

            if r.node_name != "__truncated__":
                assert r.file, f"{lang}: empty file on {r.node_name!r}"
                assert r.node_id, f"{lang}: empty node_id on {r.node_name!r}"

        # --- strict identity matching (catch silent regressions on known symbols) ---
        by_role: Dict[str, Set[str]] = {
            "defined": set(),
            "callees": set(),
            "callers": set(),
        }
        for r in results:
            if r.role in by_role:
                by_role[r.role].add(r.node_name)

        for role_name in ("defined", "callees", "callers"):
            expected = probe[f"expected_{role_name}"]
            if not expected:
                continue
            missing = expected - by_role[role_name]
            assert not missing, (
                f"{lang}: missing expected {role_name} entries: {sorted(missing)}\n"
                f"  got {role_name}: {sorted(by_role[role_name])}"
            )

    def test_python(self):
        self._smoke("python", _load_prebuilt("python"))

    def test_typescript(self):
        self._smoke("ts", _load_prebuilt("ts"))

    def test_cpp(self):
        self._smoke("cpp", _load_prebuilt("cpp"))

    def test_rust(self):
        self._smoke("rust", _load_prebuilt("rust"))

    def test_go(self):
        self._smoke("go", _load_prebuilt("go"))
