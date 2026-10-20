# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compiler name spans must survive assigned-function document symbols."""

import copy
from pathlib import Path

import pytest

from codenib.graph.incremental import lsp_client
from codenib.graph.incremental.lsp_client import LSPClient
from codenib.ls_index.lsp_graph_decode import iter_lsp_symbol_definitions
from codenib.ls_index.lsp_indexer import GenericLSPIndexer


def _range(start_line, start_column, end_line, end_column):
    return {
        "start": {"line": start_line, "character": start_column},
        "end": {"line": end_line, "character": end_column},
    }


def _compiler_span(start_line, start_column, end_line, end_column):
    return {
        "start": {"line": start_line + 1, "offset": start_column + 1},
        "end": {"line": end_line + 1, "offset": end_column + 1},
    }


@pytest.fixture
def assigned_function(tmp_path):
    (tmp_path / "worker.js").write_text(
        "exports.work = async function () {\n  return 1;\n};\n",
        encoding="utf-8",
    )
    body = _range(0, 15, 2, 1)
    symbol = {"name": "work", "kind": 12, "range": body, "selectionRange": body}
    tree = {
        "text": '"worker"',
        "kind": "module",
        "childItems": [
            {
                "text": "work",
                "kind": "function",
                "spans": [_compiler_span(0, 15, 2, 1)],
                "nameSpan": _compiler_span(0, 8, 0, 12),
            }
        ],
    }
    return [symbol], tree


def _client(tmp_path, monkeypatch, symbols, tree, *, supported=True, language="ts"):
    client = LSPClient([], str(tmp_path), language)
    client._typescript_navtree_supported = supported
    calls = []
    monkeypatch.setattr(client, "_notify", lambda *_args: None)

    def request(method, params, **_kwargs):
        calls.append((method, params))
        if method == "textDocument/documentSymbol":
            return symbols
        if method == "workspace/executeCommand":
            assert params["command"] == "typescript.tsserverRequest"
            assert params["arguments"] == [
                "navtree",
                {"file": str(tmp_path / "worker.js")},
                {},
            ]
            return {"body": tree} if tree is not None else None
        if method == "textDocument/references":
            # The server resolves the property name, not the async keyword.
            if params["position"] == {"line": 0, "character": 8}:
                return [
                    {
                        "uri": (tmp_path / "caller.js").as_uri(),
                        "range": _range(3, 2, 3, 6),
                    }
                ]
            return []
        raise AssertionError(method)

    monkeypatch.setattr(client, "_request", request)
    return client, calls


def test_assigned_function_references_use_compiler_name_span(
    tmp_path, monkeypatch, assigned_function
):
    symbols, tree = assigned_function
    original = copy.deepcopy(symbols)
    client, calls = _client(tmp_path, monkeypatch, symbols, tree)

    result = client.document_symbol("worker.js")
    assert result[0]["selectionRange"] == _range(0, 8, 0, 12)
    assert result[0]["range"] == _range(0, 8, 2, 1)
    assert symbols == original
    definition = next(iter_lsp_symbol_definitions("worker.js", result, language="ts"))
    assert definition["selection_character"] == 8
    indexer = GenericLSPIndexer(tmp_path, tmp_path / "out", language="ts")
    references = indexer._collect_references(client, Path("worker.js"), result)
    assert len(references) == 1
    assert references[0]["locations"][0]["range"] == _range(3, 2, 3, 6)
    assert sum(method == "workspace/executeCommand" for method, _ in calls) == 1


@pytest.mark.parametrize("failure", ["unavailable", "renamed", "shifted", "ambiguous"])
def test_name_span_repair_requires_unambiguous_matching_compiler_body(
    tmp_path, monkeypatch, assigned_function, failure
):
    symbols, tree = assigned_function
    original = copy.deepcopy(symbols)
    if failure == "unavailable":
        tree = None
    elif failure == "renamed":
        tree["childItems"][0]["text"] = "other"
    elif failure == "shifted":
        tree["childItems"][0]["spans"] = [_compiler_span(1, 15, 3, 1)]
    else:
        duplicate = copy.deepcopy(tree["childItems"][0])
        duplicate["nameSpan"] = _compiler_span(0, 2, 0, 6)
        tree["childItems"].append(duplicate)
    client, _ = _client(tmp_path, monkeypatch, symbols, tree)

    assert client.document_symbol("worker.js") == original


@pytest.mark.parametrize("case", ["unsupported", "valid_selection"])
def test_symbol_repair_avoids_unneeded_custom_requests(
    tmp_path, monkeypatch, assigned_function, case
):
    symbols, tree = assigned_function
    if case == "valid_selection":
        symbols[0]["selectionRange"] = _range(0, 8, 0, 12)
    client, calls = _client(
        tmp_path, monkeypatch, symbols, tree, supported=case != "unsupported"
    )
    assert client.document_symbol("worker.js") == symbols
    assert [method for method, _ in calls] == ["textDocument/documentSymbol"]


@pytest.mark.parametrize(
    ("language", "advertised", "expected"),
    [("typescript", True, True), ("ts", False, False), ("python", True, False)],
)
def test_navtree_recovery_requires_server_capability(
    tmp_path, monkeypatch, language, advertised, expected
):
    client = LSPClient(["unused-server"], str(tmp_path), language)
    commands = ["typescript.tsserverRequest"] if advertised else []
    monkeypatch.setattr(lsp_client, "_lsp_process_env", lambda *_args: {})
    monkeypatch.setattr(
        lsp_client.subprocess, "Popen", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(client, "_notify", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "capabilities": {"executeCommandProvider": {"commands": commands}}
        },
    )

    client.start(skip_probe=True)

    assert client._typescript_navtree_supported is expected
