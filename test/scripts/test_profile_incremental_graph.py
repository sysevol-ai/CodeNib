# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``scripts/profiling/profile_incremental_graph.py``.

The benchmark itself needs real SCIP/clangd + cloned repos (that side is an
integration concern, exercised manually). These tests cover the pure helpers
and the per-step comparison harness with mocked strategy sub-records — no LSP,
no git, no ``codeminer.*`` heavy deps (those imports are lazy in the script).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "profiling" / "profile_incremental_graph.py"
_SPEC = importlib.util.spec_from_file_location(
    "_profile_incremental_graph_for_tests", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
prof = importlib.util.module_from_spec(_SPEC)
sys.modules["_profile_incremental_graph_for_tests"] = prof
_SPEC.loader.exec_module(prof)


class TestIsTestPath:
    """Files/dirs the test-exclusion filter should catch vs. let through."""

    @pytest.mark.parametrize(
        "path",
        [
            "test/foo.py",
            "tests/bar.go",
            "src/__tests__/baz.ts",
            "pkg/testing/x.go",
            "fixtures/data.json",
            "testdata/sample.txt",
            "src/test_helper.py",
            "src/handler_test.go",
            "src/handler_test.py",
            "src/component.test.ts",
            "src/component.test.tsx",
            "src/component.spec.js",
            "src/SPEC.spec.ts",
            "deeply/nested/specs/x.py",
        ],
    )
    def test_excludes(self, path):
        assert prof.is_test_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/foo.py",
            "lib/handler.go",
            "pkg/server/main.go",
            "src/component.ts",
            "src/myspec.py",
            "src/contestant.py",
        ],
    )
    def test_includes(self, path):
        assert prof.is_test_path(path) is False


class TestParseInstancesInline:
    """Comma-separated instance spec parsing."""

    def test_three_field_form(self, tmp_path):
        out = prof.parse_instances_inline("foo:python:abc123,bar:go:def456", tmp_path)
        assert out == [("foo", "python", "abc123"), ("bar", "go", "def456")]

    def test_whitespace_tolerance(self, tmp_path):
        out = prof.parse_instances_inline(" foo:python:abc , bar:go:def ", tmp_path)
        assert out == [("foo", "python", "abc"), ("bar", "go", "def")]

    def test_skip_malformed(self, tmp_path, capsys):
        out = prof.parse_instances_inline("only_one_field,foo:python:abc", tmp_path)
        assert out == [("foo", "python", "abc")]
        assert "cannot parse" in capsys.readouterr().err

    def test_two_field_with_inference_failure(self, tmp_path, capsys):
        # No repo on disk -> inference returns None -> entry dropped + warning.
        out = prof.parse_instances_inline("ghost:abc123", tmp_path)
        assert out == []
        assert "language inference failed" in capsys.readouterr().err

    def test_two_field_with_inference_success(self, tmp_path):
        # Create a repo dir with .py files to force python inference.
        repo = tmp_path / "real" / "repo"
        repo.mkdir(parents=True)
        for i in range(3):
            (repo / f"f{i}.py").write_text("x = 1\n")
        out = prof.parse_instances_inline("real:abc123", tmp_path)
        assert out == [("real", "python", "abc123")]


class TestParseInstancesConfig:
    """JSON config file parsing."""

    def test_roundtrip(self, tmp_path):
        cfg = tmp_path / "instances.json"
        cfg.write_text(
            '[{"instance_id": "foo", "language": "rust", "base_commit": "aa"}]'
        )
        assert prof.parse_instances_config(cfg) == [("foo", "rust", "aa")]


class TestInferLanguage:
    """Dominant-language detection from a repo's file extensions."""

    def test_picks_dominant(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        for i in range(5):
            (repo / f"a{i}.rs").write_text("fn main() {}\n")
        (repo / "one.py").write_text("x = 1\n")
        assert prof._infer_language(repo) == "rust"

    def test_none_when_no_source(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# hi\n")
        assert prof._infer_language(repo) is None

    def test_missing_dir(self, tmp_path):
        assert prof._infer_language(tmp_path / "nope") is None


class TestSummarizeLspProfile:
    """LSP profile JSONL -> aggregate summary."""

    def test_missing_file(self, tmp_path):
        out = prof._summarize_lsp_profile(tmp_path / "nope.jsonl")
        assert out == {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}

    def test_none_path(self):
        out = prof._summarize_lsp_profile(None)
        assert out == {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}

    def test_aggregates_by_method(self, tmp_path):
        p = tmp_path / "calls.jsonl"
        p.write_text(
            "\n".join(
                [
                    '{"m": "definition", "ms": 10.5, "n": 2}',
                    '{"m": "definition", "ms": 4.5, "n": 0}',
                    '{"m": "references", "ms": 100, "n": 5}',
                    "",
                    "garbage line - must not crash the parser",
                ]
            )
        )
        out = prof._summarize_lsp_profile(p)
        assert out["calls_total"] == 3
        assert out["calls_by_method"] == {"definition": 2, "references": 1}
        assert out["ms_by_method"]["definition"] == 15.0
        assert out["ms_by_method"]["references"] == 100.0
        assert out["empty_by_method"]["definition"] == 1
        assert out["empty_by_method"]["references"] == 0


class _FakeVertex:
    def __init__(self, name, **attrs):
        self._attrs = {"name": name, **attrs}

    def attributes(self):
        return dict(self._attrs)

    def __getitem__(self, key):
        return self._attrs[key]


class _FakeEdge:
    def __init__(self, source, target, **attrs):
        self.source = source
        self.target = target
        self._attrs = attrs

    def attributes(self):
        return dict(self._attrs)


class _FakeCodeGraph:
    def __init__(self, vertices, edges):
        self.graph = type("FakeIgraph", (), {"vs": vertices, "es": edges})()


class TestGraphFidelity:
    def _reference_graph(self):
        vertices = [
            _FakeVertex("src/a.py", type="file"),
            _FakeVertex(
                "internal-a",
                type="function",
                unified_name="pkg.a()",
                file="src/a.py",
                start_line=1,
                end_line=3,
                selection_line=1,
            ),
            _FakeVertex(
                "internal-b",
                type="function",
                unified_name="pkg.b()",
                file="src/b.py",
                start_line=5,
                end_line=7,
                selection_line=5,
            ),
        ]
        edges = [
            _FakeEdge(0, 1, type="contain"),
            _FakeEdge(
                1,
                2,
                type="reference",
                anchor_file="src/a.py",
                anchor_line=2,
            ),
        ]
        return _FakeCodeGraph(vertices, edges)

    def test_exact_graph_matches_globally_and_on_changed_slice(self):
        reference = self._reference_graph()
        metrics = prof.compare_graphs(reference, reference, {"src/a.py"})
        assert all(metric["f1"] == 1.0 for metric in metrics.values())
        assert metrics["changed_reference_edges"]["reference"] == 1

    def test_missing_reference_is_visible_on_changed_slice(self):
        reference = self._reference_graph()
        predicted = _FakeCodeGraph(reference.graph.vs, reference.graph.es[:1])
        metrics = prof.compare_graphs(predicted, reference, {"src/a.py"})
        assert metrics["vertices"]["f1"] == 1.0
        assert metrics["edges"]["recall"] == pytest.approx(0.5)
        assert metrics["changed_reference_edges"]["recall"] == 0.0

    def test_overlap_preserves_duplicate_facts(self):
        metrics = prof._fact_overlap_metrics(["a", "a"], ["a", "a", "a"])
        assert metrics == {
            "predicted": 2,
            "reference": 3,
            "overlap": 2,
            "precision": 1.0,
            "recall": pytest.approx(2 / 3),
            "f1": pytest.approx(0.8),
        }

    def test_scip_repository_commit_is_not_a_semantic_identity(self):
        base = _FakeVertex(
            "external",
            type="function",
            unified_name=(
                "scip-python python repo "
                "b90661d6a46aa3619d3eec94d5281f5888add501 _operator:itemgetter"
            ),
            file=(
                "scip-python python repo "
                "b90661d6a46aa3619d3eec94d5281f5888add501 _operator"
            ),
        )
        target = _FakeVertex(
            "external",
            type="function",
            unified_name=(
                "scip-python python repo "
                "40e1536840d213fc6ca726efc7e5e19b15513eac _operator:itemgetter"
            ),
            file=(
                "scip-python python repo "
                "40e1536840d213fc6ca726efc7e5e19b15513eac _operator"
            ),
        )

        assert prof._vertex_identity(base) == prof._vertex_identity(target)

    def test_scip_commit_difference_remains_visible_for_source_mapped_symbol(self):
        common = {
            "type": "function",
            "file": "src/operator.py",
            "start_line": 3,
            "end_line": 5,
            "selection_line": 3,
        }
        base = _FakeVertex(
            "external",
            unified_name=(
                "scip-python python repo "
                "b90661d6a46aa3619d3eec94d5281f5888add501 operator:itemgetter"
            ),
            **common,
        )
        target = _FakeVertex(
            "external",
            unified_name=(
                "scip-python python repo "
                "40e1536840d213fc6ca726efc7e5e19b15513eac operator:itemgetter"
            ),
            **common,
        )

        assert prof._vertex_identity(base) != prof._vertex_identity(target)

    def test_changed_paths_include_both_rename_sides(self):
        changed = {
            "modified": ["m.py"],
            "added": ["a.py"],
            "deleted": ["d.py"],
            "renamed": [("old.py", "new.py")],
        }
        assert prof._changed_paths(changed) == {
            "m.py",
            "a.py",
            "d.py",
            "old.py",
            "new.py",
        }

    @staticmethod
    def _behavior_graph(tmp_path, *, target_line=4):
        from codeminer.graph.code_graph import CodeGraph
        from codeminer.types import NODE_TYPE_FUNCTION

        (tmp_path / "caller.py").write_text(
            "def run():\n    return load_config()\n",
            encoding="utf-8",
        )
        (tmp_path / "callee.py").write_text(
            "\n" * target_line + "def load_config():\n    return {}\n",
            encoding="utf-8",
        )
        graph = CodeGraph()
        graph.project_root = str(tmp_path)
        graph.add_file_node("caller.py")
        graph.add_symbol_node(
            "caller.run",
            line=0,
            scope_start_line=0,
            scope_end_line=1,
            symbol_type=NODE_TYPE_FUNCTION,
        )
        graph.update_current_scope("caller.run", start_line=0, end_line=1)
        graph.add_symbol_reference(
            "callee.load_config",
            module_path="callee.py",
            symbol_type=NODE_TYPE_FUNCTION,
            anchor_file="caller.py",
            anchor_line=1,
        )
        graph.add_file_node("callee.py")
        graph.add_symbol_node(
            "callee.load_config",
            line=target_line,
            scope_start_line=target_line,
            scope_end_line=target_line + 1,
            symbol_type=NODE_TYPE_FUNCTION,
        )
        graph.graph.vs[graph.name_to_vertex["callee.load_config"]][
            "unified_name"
        ] = "callee.py:load_config()"
        graph.build_range_indexes()
        return graph

    def test_behavior_guard_accepts_equal_source_locations(self, tmp_path):
        reference = self._behavior_graph(tmp_path)
        result = prof.compare_graph_behavior(
            reference,
            reference,
            project_root=tmp_path,
            changed_files={"caller.py"},
        )

        assert result["status"] == "ok"
        assert result["request_count"] == 2
        assert result["equivalent_count"] == 2
        assert result["exact"] is True

    def test_behavior_guard_rejects_changed_definition_location(self, tmp_path):
        reference = self._behavior_graph(tmp_path, target_line=4)
        predicted = self._behavior_graph(tmp_path, target_line=5)
        result = prof.compare_graph_behavior(
            predicted,
            reference,
            project_root=tmp_path,
            changed_files={"caller.py"},
        )

        assert result["status"] == "ok"
        assert result["equivalent_count"] < result["request_count"]
        assert result["exact"] is False
        assert result["mismatch_examples"]

    def test_fidelity_checks_out_the_measured_commit(self, tmp_path, monkeypatch):
        events = []
        graph = self._behavior_graph(tmp_path)
        record = {
            "instance_id": "owner__repo-1",
            "step": 1,
            "fully-rebuild": {"status": "ok"},
            "symbol-level-patch": {
                "status": "ok",
                "changed_files": ["caller.py"],
            },
        }

        monkeypatch.setattr(
            prof,
            "checkout",
            lambda project_root, commit: events.append((project_root, commit)),
        )
        monkeypatch.setattr(
            prof,
            "_CodeGraph",
            lambda: type("Loader", (), {"load_graph": lambda _self, _path: graph})(),
        )

        prof.attach_graph_fidelity(
            record,
            tmp_path,
            ["symbol-level-patch"],
            tmp_path,
            "target-commit",
        )

        assert events == [(tmp_path, "target-commit")]
        assert record["symbol-level-patch"]["fidelity"]["status"] == "ok"

    def test_fidelity_guard_rejects_inherited_whole_graph_mismatch(
        self, tmp_path, monkeypatch
    ):
        graph = self._behavior_graph(tmp_path)
        record = {
            "instance_id": "owner__repo-1",
            "step": 2,
            "fully-rebuild": {"status": "ok"},
            "symbol-level-patch": {
                "status": "ok",
                "changed_files": ["caller.py"],
            },
        }
        exact = {"precision": 1.0, "recall": 1.0}
        whole_edge_mismatch = {"precision": 0.99, "recall": 1.0}

        monkeypatch.setattr(prof, "checkout", lambda *_args: None)
        monkeypatch.setattr(
            prof,
            "_CodeGraph",
            lambda: type("Loader", (), {"load_graph": lambda _self, _path: graph})(),
        )
        monkeypatch.setattr(
            prof,
            "compare_graphs",
            lambda *_args, **_kwargs: {
                "vertices": exact,
                "edges": whole_edge_mismatch,
                "changed_vertices": exact,
                "changed_edges": exact,
            },
        )
        monkeypatch.setattr(
            prof,
            "compare_graph_behavior",
            lambda *_args, **_kwargs: {"status": "ok", "exact": True},
        )

        prof.attach_graph_fidelity(
            record,
            tmp_path,
            ["symbol-level-patch"],
            tmp_path,
            "target-commit",
        )

        fidelity = record["symbol-level-patch"]["fidelity"]
        assert fidelity["raw_graph"]["changed_exact"] is True
        assert fidelity["raw_graph"]["whole_exact"] is False
        assert fidelity["behavior_exact"] is True
        assert fidelity["equivalence_guardrail_pass"] is False


class TestPhaseOrder:
    def test_order_is_stable_and_counterbalanced(self):
        first = prof.patcher_phase_order("owner__repo-1")
        assert prof.patcher_phase_order("owner__repo-1") == first
        orders = {prof.patcher_phase_order(f"repo-{i}") for i in range(100)}
        assert orders == {
            ("file-level-patch", "symbol-level-patch"),
            ("symbol-level-patch", "file-level-patch"),
        }


class TestFinalizeStepRecord:
    """Per-step comparison harness: derive speedups from mocked strategy
    sub-records (no LSP / git / indexer)."""

    def _rec(self, rebuild=None, file_level=None, symbol=None):
        rec = {"instance_id": "x", "step": 1}
        if rebuild is not None:
            rec["fully-rebuild"] = rebuild
        if file_level is not None:
            rec["file-level-patch"] = file_level
        if symbol is not None:
            rec["symbol-level-patch"] = symbol
        return rec

    def test_speedups_computed(self):
        rec = self._rec(
            rebuild={"status": "ok", "t_s": 100.0},
            file_level={"status": "ok", "t_s": 40.0},
            symbol={"status": "ok", "t_s": 10.0},
        )
        out = prof.finalize_step_record(rec)
        assert out["speedup_vs_fully_rebuild"] == pytest.approx(10.0)
        assert out["speedup_vs_file_level_patch"] == pytest.approx(4.0)
        assert out["speedup_basis"] == "amortized_t_s"
        assert "finished" in out

    def test_guarded_speedup_requires_exact_fidelity(self):
        rec = self._rec(
            rebuild={"status": "ok", "t_s": 100.0},
            symbol={
                "status": "ok",
                "t_s": 10.0,
                "fidelity": {
                    "status": "ok",
                    "behavior_exact": True,
                    "equivalence_guardrail_pass": True,
                },
            },
        )
        out = prof.finalize_step_record(rec)
        assert out["equivalence_guarded_speedup_vs_fully_rebuild"] == 10.0

    def test_guarded_speedup_rejects_raw_changed_graph_mismatch(self):
        rec = self._rec(
            rebuild={"status": "ok", "t_s": 100.0},
            symbol={
                "status": "ok",
                "t_s": 10.0,
                "fidelity": {
                    "status": "ok",
                    "behavior_exact": True,
                    "equivalence_guardrail_pass": False,
                },
            },
        )

        out = prof.finalize_step_record(rec)

        assert out["equivalence_guarded_speedup_vs_fully_rebuild"] is None

    def test_none_when_symbol_failed(self):
        # Symbol strategy errored -> no t_s denominator -> ratios are None.
        rec = self._rec(
            rebuild={"status": "ok", "t_s": 100.0},
            file_level={"status": "ok", "t_s": 40.0},
            symbol={"status": "error", "error": "boom"},
        )
        out = prof.finalize_step_record(rec)
        assert out["speedup_vs_fully_rebuild"] is None
        assert out["speedup_vs_file_level_patch"] is None

    def test_none_when_rebuild_failed(self):
        # Rebuild timed out -> rebuild ratio None, but file-level ratio holds.
        rec = self._rec(
            rebuild={"status": "timeout", "t_s": 600.0},
            file_level={"status": "ok", "t_s": 40.0},
            symbol={"status": "ok", "t_s": 10.0},
        )
        out = prof.finalize_step_record(rec)
        assert out["speedup_vs_fully_rebuild"] is None
        assert out["speedup_vs_file_level_patch"] == pytest.approx(4.0)

    def test_missing_strategies_are_safe(self):
        # No strategy sub-records at all -> both ratios None, no crash.
        out = prof.finalize_step_record(self._rec())
        assert out["speedup_vs_fully_rebuild"] is None
        assert out["speedup_vs_file_level_patch"] is None


class TestResumeEligibility:
    def test_requires_current_protocol_success_and_fidelity(self):
        row = {
            "protocol_version": prof.PROTOCOL_VERSION,
            "step": 1,
            "primary_incremental_strategy": "symbol-level-patch",
            "fully-rebuild": {"status": "ok"},
            "symbol-level-patch": {
                "status": "ok",
                "fidelity": {"status": "ok", "behavior_exact": False},
            },
        }
        assert prof._record_is_resumable(row) is True

    @pytest.mark.parametrize("version", [None, 1, 2, 3])
    def test_rejects_old_protocols(self, version):
        assert (
            prof._record_is_resumable({"protocol_version": version, "step": 1}) is False
        )


class TestStrategyNames:
    """Guard the three strategy labels the issue specifies stay wired up."""

    def test_default_instances_cover_five_languages(self):
        langs = {lang for (_iid, lang, _sha) in prof.DEFAULT_INSTANCES}
        assert langs == {"cpp", "go", "python", "rust", "ts"}

    def test_lang_exts_and_pathspec_cover_same_languages(self):
        assert set(prof.LANG_EXTS) == set(prof.LANG_PATHSPEC)
