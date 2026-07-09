# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the LSP provider validation CLI."""

from __future__ import annotations

import json

from codeminer.eval.agent_runner import lsp_provider_cli
from codeminer.eval.agent_runner.lsp_provider_validation import (
    LSPProviderCall,
    LSPProviderComparison,
    LSPProviderRequest,
)


def _comparison(verdict: str, *, same_result: bool | None = True):
    request = LSPProviderRequest(
        capability="textDocument/definition",
        arguments={"file_path": "caller.py", "line": 2, "character": 4},
        request_id="caller-definition",
    )
    return LSPProviderComparison(
        request=request,
        static_call=LSPProviderCall(
            provider="codeminer_static_index",
            capability="definition",
            status="ok",
            duration_ms=1.0,
            result_count=1,
            result_fingerprint="static",
        ),
        reference_call=LSPProviderCall(
            provider="json_rpc",
            capability="definition",
            status="ok",
            duration_ms=5.0,
            result_count=1,
            result_fingerprint="static" if same_result else "live",
        ),
        same_result=same_result,
        verdict=verdict,
        latency_saved_ms=4.0,
        speedup_ratio=5.0,
    )


def test_load_lsp_provider_requests_accepts_jsonl(tmp_path):
    path = tmp_path / "requests.jsonl"
    path.write_text(
        "\n".join(
            [
                "# comment",
                json.dumps(
                    {
                        "lsp_method": "textDocument/definition",
                        "resolved_arguments": {
                            "file_path": "caller.py",
                            "line": 2,
                            "character": 4,
                        },
                        "request_id": "caller-definition",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    requests = lsp_provider_cli.load_lsp_provider_requests(path)

    assert len(requests) == 1
    assert requests[0].normalized_capability == "definition"
    assert requests[0].arguments == {
        "file_path": "caller.py",
        "line": 2,
        "character": 4,
    }
    assert requests[0].request_id == "caller-definition"


def test_cli_writes_reports_and_requires_promotion(tmp_path, monkeypatch):
    requests_path = tmp_path / "requests.json"
    output_json = tmp_path / "report.json"
    output_md = tmp_path / "report.md"
    requests_path.write_text(
        json.dumps(
            [
                {
                    "capability": "textDocument/definition",
                    "arguments": {"file_path": "caller.py", "line": 2},
                }
            ]
        ),
        encoding="utf-8",
    )

    sentinel_graph = object()
    seen = {}

    def fake_load_graph(args):
        return sentinel_graph

    def fake_compare(requests, **kwargs):
        seen["requests"] = requests
        seen["kwargs"] = kwargs
        return [_comparison("equivalent_static_faster")]

    monkeypatch.setattr(lsp_provider_cli, "_load_graph_from_args", fake_load_graph)
    monkeypatch.setattr(
        lsp_provider_cli, "compare_static_to_live_lsp_provider", fake_compare
    )

    code = lsp_provider_cli.main(
        [
            "--graph",
            str(tmp_path / "graph.pkl"),
            "--project-root",
            str(tmp_path),
            "--language",
            "python",
            "--requests",
            str(requests_path),
            "--output-json",
            str(output_json),
            "--output-markdown",
            str(output_md),
            "--require-promotion",
            "--quiet",
        ]
    )

    assert code == 0
    assert seen["kwargs"]["graph"] is sentinel_graph
    assert seen["kwargs"]["language"] == "python"
    assert seen["requests"][0].normalized_capability == "definition"
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["summary"]["promotion_ready"] is True
    assert payload["summary"]["verdict_counts"] == {"equivalent_static_faster": 1}
    assert "Promotion ready: yes" in output_md.read_text(encoding="utf-8")


def test_exit_code_for_lsp_provider_summary_blocks_mismatch_and_fallback():
    assert (
        lsp_provider_cli.exit_code_for_lsp_provider_summary(
            {"mismatch_count": 1, "error_count": 0}
        )
        == 2
    )
    assert (
        lsp_provider_cli.exit_code_for_lsp_provider_summary(
            {"mismatch_count": 0, "error_count": 0, "fallback_count": 1},
            fail_on_fallback=True,
        )
        == 3
    )
    assert (
        lsp_provider_cli.exit_code_for_lsp_provider_summary(
            {
                "mismatch_count": 0,
                "error_count": 0,
                "fallback_count": 0,
                "promotion_ready": False,
            },
            require_promotion=True,
        )
        == 4
    )
