# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for provider-level LSP request replay benchmarks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from codeminer.eval.agent_runner.live_lsp_provider import lsp_locations_to_nodes
from codeminer.eval.agent_runner.lsp_replay_benchmark import (
    exit_code_for_lsp_replay_benchmark,
    generate_lsp_replay_requests,
    render_lsp_replay_benchmark_markdown,
    run_lsp_replay_benchmark,
)
from codeminer.graph.code_graph import CodeGraph
from codeminer.types import NODE_TYPE_FUNCTION


def _range_graph(tmp_path: Path) -> CodeGraph:
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n",
        encoding="utf-8",
    )
    (tmp_path / "callee.py").write_text(
        "\n\n\n\n" "def load_config():\n" "    return {}\n",
        encoding="utf-8",
    )

    graph = CodeGraph()
    graph.project_root = str(tmp_path)
    graph.add_file_node("caller.py")
    graph.add_symbol_node(
        "caller.run",
        line=0,
        scope_start_line=0,
        scope_end_line=3,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("caller.run", start_line=0, end_line=3)
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
        line=4,
        scope_start_line=4,
        scope_end_line=5,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.graph.vs[graph.name_to_vertex["callee.load_config"]][
        "unified_name"
    ] = "callee.py:load_config()"
    graph.build_range_indexes()
    return graph


class _FakeLiveProvider:
    def __init__(
        self,
        *,
        project_root: str | Path,
        language: str,
        command: Optional[Sequence[str]] = None,
        skip_probe: bool = False,
    ) -> None:
        self.project_root = Path(project_root)
        self.language = language
        self.command = command
        self.skip_probe = skip_probe
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def __call__(self, capability: str, arguments: Mapping[str, Any]) -> Any:
        assert self.started is True
        assert arguments["file_path"] == "caller.py"
        if capability == "definition":
            return self._nodes(
                [
                    {
                        "uri": (self.project_root / "callee.py").as_uri(),
                        "range": {
                            "start": {"line": 4, "character": 4},
                            "end": {"line": 4, "character": 15},
                        },
                    }
                ],
                relation="json-rpc definition",
                node_type="definition",
            )
        if capability == "references":
            return self._nodes(
                [
                    {
                        "uri": (self.project_root / "caller.py").as_uri(),
                        "range": {
                            "start": {"line": 1, "character": 11},
                            "end": {"line": 1, "character": 22},
                        },
                    },
                    {
                        "uri": (self.project_root / "callee.py").as_uri(),
                        "range": {
                            "start": {"line": 4, "character": 4},
                            "end": {"line": 4, "character": 15},
                        },
                    },
                ],
                relation="json-rpc reference",
                node_type="reference",
            )
        raise AssertionError(f"unexpected capability: {capability}")

    def _nodes(self, locations, *, relation: str, node_type: str):
        return lsp_locations_to_nodes(
            locations,
            project_root=self.project_root,
            relation=relation,
            node_type=node_type,
        )


def test_generate_lsp_replay_requests_from_reference_edges(tmp_path):
    graph = _range_graph(tmp_path)

    requests = generate_lsp_replay_requests(
        graph,
        capabilities=["definition", "references"],
        max_per_capability=1,
    )

    assert [request.normalized_capability for request in requests] == [
        "definition",
        "references",
    ]
    assert [request.arguments for request in requests] == [
        {"file_path": "caller.py", "line": 1, "character": 11, "top_k": 8},
        {
            "file_path": "caller.py",
            "line": 1,
            "character": 11,
            "include_declaration": True,
            "top_k": 40,
        },
    ]
    assert requests[0].request_id == "definition:caller.py:1:11:load_config"


def test_generate_lsp_replay_requests_spreads_candidates(tmp_path):
    graph = CodeGraph()
    graph.project_root = str(tmp_path)
    graph.add_file_node("callee.py")

    for file_stem, target in [
        ("a_first", "first_target"),
        ("m_middle", "middle_target"),
        ("z_last", "last_target"),
    ]:
        file_path = f"{file_stem}.py"
        (tmp_path / file_path).write_text(
            f"def run():\n    return {target}()\n",
            encoding="utf-8",
        )
        graph.add_file_node(file_path)
        caller = f"{file_stem}.run"
        graph.add_symbol_node(
            caller,
            line=0,
            scope_start_line=0,
            scope_end_line=1,
            symbol_type=NODE_TYPE_FUNCTION,
        )
        graph.update_current_scope(caller, start_line=0, end_line=1)
        graph.add_symbol_reference(
            f"callee.{target}",
            module_path="callee.py",
            symbol_type=NODE_TYPE_FUNCTION,
            anchor_file=file_path,
            anchor_line=1,
        )
        graph.add_symbol_node(
            f"callee.{target}",
            line=0,
            scope_start_line=0,
            scope_end_line=0,
            symbol_type=NODE_TYPE_FUNCTION,
        )
        graph.graph.vs[graph.name_to_vertex[f"callee.{target}"]][
            "unified_name"
        ] = f"callee.py:{target}()"

    graph.build_range_indexes()

    requests = generate_lsp_replay_requests(
        graph,
        capabilities=["definition"],
        max_per_capability=1,
    )

    assert len(requests) == 1
    assert requests[0].arguments["file_path"] == "m_middle.py"
    assert requests[0].arguments["character"] == 11


def test_run_lsp_replay_benchmark_reports_equivalent_latency(tmp_path):
    graph = _range_graph(tmp_path)
    requests = generate_lsp_replay_requests(
        graph,
        capabilities=["definition", "references"],
        max_per_capability=1,
    )

    payload = run_lsp_replay_benchmark(
        requests,
        graph=graph,
        project_root=tmp_path,
        language="python",
        warmup_reps=1,
        measured_reps=2,
        live_provider_factory=_FakeLiveProvider,
    )

    summary = payload["summary"]["overall"]
    assert payload["request_count"] == 2
    assert len(payload["comparisons"]) == 4
    assert summary["row_count"] == 4
    assert summary["equivalent_row_count"] == 4
    assert summary["mismatch_count"] == 0
    assert summary["guardrail_pass"] is True
    assert summary["static_ms"]["count"] == 4
    assert summary["live_ms"]["count"] == 4
    assert set(payload["summary"]["by_capability"]) == {"definition", "references"}

    payload["setup"]["graph_load_ms"] = 1.25
    markdown = render_lsp_replay_benchmark_markdown(payload)
    assert "# LSP request replay benchmark" in markdown
    assert "graph load 1.25 ms" in markdown
    assert "Equivalence guardrail: yes" in markdown
    assert "static p50/p95/p99 ms" in markdown


def test_exit_code_for_lsp_replay_benchmark_guardrails():
    assert exit_code_for_lsp_replay_benchmark({"summary": {"overall": {}}}) == 2
    assert (
        exit_code_for_lsp_replay_benchmark(
            {
                "summary": {
                    "overall": {
                        "row_count": 1,
                        "equivalent_row_count": 0,
                        "error_count": 0,
                        "mismatch_count": 1,
                    }
                }
            }
        )
        == 2
    )
    assert (
        exit_code_for_lsp_replay_benchmark(
            {
                "summary": {
                    "overall": {
                        "row_count": 2,
                        "equivalent_row_count": 1,
                        "error_count": 0,
                        "mismatch_count": 1,
                    }
                }
            },
            require_all_equivalent=True,
        )
        == 3
    )
    assert (
        exit_code_for_lsp_replay_benchmark(
            {
                "summary": {
                    "overall": {
                        "row_count": 2,
                        "equivalent_row_count": 1,
                        "error_count": 0,
                        "mismatch_count": 0,
                    }
                }
            },
            require_all_equivalent=True,
        )
        == 3
    )
