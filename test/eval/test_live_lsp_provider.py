# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for live JSON-RPC LSP reference-provider adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeminer.agent.lsp_provider import JSON_RPC_LSP_PROVIDER, lsp_result_metadata
from codeminer.eval.agent_runner.live_lsp_provider import (
    LiveLSPReferenceProvider,
    compare_static_to_live_lsp_provider,
    lsp_locations_to_nodes,
)
from codeminer.eval.agent_runner.lsp_provider_validation import (
    LSPProviderRequest,
    compare_static_lsp_provider,
    fingerprint_lsp_start_locations,
)
from codeminer.graph.code_graph import CodeGraph
from codeminer.types import NODE_TYPE_FUNCTION


def _range_graph() -> CodeGraph:
    graph = CodeGraph()
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
        scope_end_line=8,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.graph.vs[graph.name_to_vertex["callee.load_config"]][
        "unified_name"
    ] = "callee.py:load_config()"
    graph.build_range_indexes()
    return graph


def test_lsp_locations_to_nodes_normalizes_location_and_location_link(tmp_path):
    target = tmp_path / "pkg" / "service.py"
    target.parent.mkdir()
    target.write_text("def handle():\n    pass\n", encoding="utf-8")

    nodes = lsp_locations_to_nodes(
        [
            {
                "uri": target.as_uri(),
                "range": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 10},
                },
            },
            {
                "targetUri": target.as_uri(),
                "targetSelectionRange": {
                    "start": {"line": 1, "character": 4},
                    "end": {"line": 1, "character": 8},
                },
            },
        ],
        project_root=tmp_path,
        relation="json-rpc definition",
        node_type="definition",
    )

    assert [node.file for node in nodes] == ["pkg/service.py", "pkg/service.py"]
    assert [node.start_line for node in nodes] == [0, 1]
    assert nodes[0].node_name == "pkg/service.py:1"
    assert nodes[0].content == "json-rpc definition"


def test_live_lsp_reference_provider_calls_injected_client(tmp_path):
    class FakeClient:
        def __init__(self, command, project_root, language):
            self.command = command
            self.project_root = project_root
            self.language = language
            self.calls = []
            self.started = False
            self.stopped = False

        def start(self, skip_probe=False):
            self.started = skip_probe is True

        def shutdown(self):
            self.stopped = True

        def definition(self, file_path, line, character):
            self.calls.append(("definition", file_path, line, character))
            return {
                "uri": (Path(self.project_root) / "callee.py").as_uri(),
                "range": {
                    "start": {"line": 4, "character": 4},
                    "end": {"line": 4, "character": 15},
                },
            }

    made = []

    def client_factory(*args):
        client = FakeClient(*args)
        made.append(client)
        return client

    provider = LiveLSPReferenceProvider(
        project_root=tmp_path,
        language="python",
        command=["fake-lsp"],
        client_factory=client_factory,
        skip_probe=True,
    )

    with provider:
        result = provider(
            "textDocument/definition",
            {"file_path": "caller.py", "line": 1, "character": 9},
        )

    assert made[0].started is True
    assert made[0].stopped is True
    assert made[0].calls == [("definition", str(tmp_path / "caller.py"), 1, 9)]
    assert result[0].file == "callee.py"
    assert result[0].start_line == 4
    assert lsp_result_metadata(result) == {
        "provider": JSON_RPC_LSP_PROVIDER,
        "capability": "definition",
        "status": "ok",
        "lsp_method": "textDocument/definition",
        "behavior_contract": "json_rpc_lsp_v1",
        "position_granularity": "character",
    }


def test_live_lsp_reference_provider_raises_on_transport_timeout(tmp_path):
    class TimeoutClient:
        _last_error = None

        def __init__(self, command, project_root, language):
            pass

        def start(self, skip_probe=False):
            pass

        def shutdown(self):
            pass

        def definition(self, file_path, line, character):
            self._last_error = {"code": -1, "message": "timeout"}
            return []

    provider = LiveLSPReferenceProvider(
        project_root=tmp_path,
        language="python",
        command=["fake-lsp"],
        client_factory=TimeoutClient,
    )

    with provider, pytest.raises(RuntimeError, match="definition failed.*timeout"):
        provider.definition(file_path="caller.py", line=1, character=0)


def test_live_lsp_reference_provider_accepts_null_location(tmp_path):
    class NullClient:
        _last_error = None

        def __init__(self, command, project_root, language):
            pass

        def start(self, skip_probe=False):
            pass

        def shutdown(self):
            pass

        def definition(self, file_path, line, character):
            self._last_error = {"code": -2, "message": "null result"}
            return []

    provider = LiveLSPReferenceProvider(
        project_root=tmp_path,
        language="python",
        command=["fake-lsp"],
        client_factory=NullClient,
    )

    with provider:
        assert provider.definition(file_path="caller.py", line=1, character=0) == []


def test_compare_static_to_live_lsp_uses_start_location_fingerprint(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    graph = _range_graph()
    graph.project_root = str(tmp_path)

    def live_reference_provider(capability, arguments):
        assert capability == "definition"
        assert arguments["file_path"] == "caller.py"
        return lsp_locations_to_nodes(
            [
                {
                    "uri": (tmp_path / "callee.py").as_uri(),
                    "range": {
                        "start": {"line": 4, "character": 4},
                        "end": {"line": 4, "character": 15},
                    },
                }
            ],
            project_root=tmp_path,
            relation="json-rpc definition",
            node_type="definition",
        )

    rows = compare_static_lsp_provider(
        [
            LSPProviderRequest(
                capability="textDocument/definition",
                arguments={"file_path": "caller.py", "line": 1, "character": 15},
                request_id="caller-to-load-config",
            )
        ],
        graph=graph,
        reference_provider=live_reference_provider,
        fingerprint_fn=fingerprint_lsp_start_locations,
    )

    assert rows[0].same_result is True
    assert rows[0].verdict in {
        "equivalent_static_faster",
        "equivalent_static_not_faster",
    }


def test_compare_static_to_live_lsp_provider_starts_fake_client(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    graph = _range_graph()
    graph.project_root = str(tmp_path)
    made = []

    class FakeClient:
        def __init__(self, command, project_root, language):
            self.command = command
            self.project_root = project_root
            self.language = language
            self.started = False
            self.stopped = False
            made.append(self)

        def start(self, skip_probe=False):
            self.started = True

        def shutdown(self):
            self.stopped = True

        def definition(self, file_path, line, character):
            return {
                "targetUri": (Path(self.project_root) / "callee.py").as_uri(),
                "targetSelectionRange": {
                    "start": {"line": 4, "character": 4},
                    "end": {"line": 4, "character": 15},
                },
            }

    rows = compare_static_to_live_lsp_provider(
        [
            LSPProviderRequest(
                capability="textDocument/definition",
                arguments={"file_path": "caller.py", "line": 1, "character": 15},
            )
        ],
        graph=graph,
        project_root=tmp_path,
        language="python",
        command=["fake-lsp"],
        client_factory=FakeClient,
    )

    assert made[0].started is True
    assert made[0].stopped is True
    assert rows[0].same_result is True


def test_compare_static_to_live_lsp_provider_rejects_route_without_lsp_start(
    tmp_path,
):
    graph = _range_graph()
    made = []

    class FakeClient:
        def __init__(self, command, project_root, language):
            made.append((command, project_root, language))

    try:
        compare_static_to_live_lsp_provider(
            [
                LSPProviderRequest(
                    capability="codeminer/lspRoute",
                    arguments={"symbols": ["callee.load_config"]},
                    request_id="route-extension",
                )
            ],
            graph=graph,
            project_root=tmp_path,
            language="python",
            command=["fake-lsp"],
            client_factory=FakeClient,
        )
    except ValueError as exc:
        assert "supports only native capabilities" in str(exc)
        assert "route-extension:route" in str(exc)
    else:
        raise AssertionError("expected live JSON-RPC validation to reject route")

    assert made == []
