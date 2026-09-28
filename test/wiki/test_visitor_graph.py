# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer
from codenib.source_fingerprint import capture_repository_source
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION
from codenib.wiki import visitor_graph
from codenib.wiki.visitor_provider import WikiRunStopped


def graph_fixture():
    graph = CodeGraph()
    for symbol in ("caller", "callee"):
        name = f"{symbol}.py:{symbol}()"
        graph._add_vertex(
            name,
            {
                "type": NODE_TYPE_FUNCTION,
                "file": f"{symbol}.py",
                "start_line": 0,
                "end_line": 1,
                "unified_name": name,
            },
        )
    graph._add_edge(
        "caller.py:caller()",
        "callee.py:callee()",
        EDGE_TYPE_REFERENCE,
        anchor_file="caller.py",
        anchor_line=1,
    )
    return graph


def test_indexer_only_receives_source_and_empty_environment(tmp_path, monkeypatch):
    (tmp_path / "caller.py").write_text("def caller():\n    return callee()\n")
    (tmp_path / "pyrightconfig.json").write_text('{"extends":"/private/settings"}')
    (tmp_path / "pyproject.toml").write_text('[build-system]\nbuild-backend="evil"\n')
    seen = []

    def run(command, root, check):
        seen.append(root)
        assert {p.name for p in root.iterdir()} == {"caller.py", "pyrightconfig.json"}
        assert "extends" not in (root / "pyrightconfig.json").read_text()
        assert (
            json.loads(Path(command[command.index("--environment") + 1]).read_text())
            == []
        )
        assert command[command.index("--project-version") + 1] == "c" * 40
        Path(command[command.index("--output") + 1]).write_bytes(b"fixture")
        return True

    monkeypatch.setattr(
        visitor_graph, "resolve_command", lambda _: "/fixture/scip-python"
    )
    monkeypatch.setattr(visitor_graph, "_run_index", run)
    monkeypatch.setattr(SCIPPythonIndexer, "decode_index", lambda _: True)
    monkeypatch.setattr(SCIPPythonIndexer, "process_index", lambda _: graph_fixture())
    with capture_repository_source(tmp_path) as source:
        bundle = SimpleNamespace(
            source_reader=source.borrow_reader(),
            code_graph=lambda: None,
            entry=SimpleNamespace(base_commit="c" * 40),
        )
        visitor_graph.build_visitor_graph(bundle, lambda: None, lambda *_: None)
        assert bundle.graph_coverage["available"]
        assert bundle.code_graph().graph.ecount() == 1
    assert seen and not seen[0].exists()
    assert "extends" in (tmp_path / "pyrightconfig.json").read_text()


@pytest.mark.parametrize("timeout", [False, True])
def test_indexing_stop_and_timeout_kill_owned_process(tmp_path, monkeypatch, timeout):
    processes = []
    real = subprocess.Popen

    def spawn(*args, **kwargs):
        process = real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(visitor_graph.subprocess, "Popen", spawn)
    if timeout:
        monkeypatch.setattr(visitor_graph, "_INDEX_SECONDS", 0)

    def check():
        if not timeout:
            raise WikiRunStopped("stop")

    with pytest.raises(TimeoutError if timeout else WikiRunStopped):
        visitor_graph._run_index(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, check
        )
    assert len(processes) == 1 and processes[0].poll() is not None


def test_indexer_does_not_inherit_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-test-key")
    command = [
        sys.executable,
        "-c",
        "import os,pathlib; pathlib.Path('env.txt').write_text("
        "str('OPENROUTER_API_KEY' in os.environ))",
    ]
    assert visitor_graph._run_index(command, tmp_path, lambda: None)
    assert (tmp_path / "env.txt").read_text() == "False"


def test_saved_projection_preserves_real_edges_and_embeds_pinned_source(tmp_path):
    (tmp_path / "caller.py").write_text("def caller():\n    return callee()\n")
    (tmp_path / "callee.py").write_text("def callee():\n    return 1\n")
    pages = [{"id": "overview", "citations": []}]
    for symbol in ("caller", "callee"):
        pages.append(
            {
                "id": symbol,
                "title": symbol,
                "children": [],
                "citations": [
                    {
                        "file": f"{symbol}.py",
                        "symbol": f"{symbol}()",
                        "start_line": 1,
                        "end_line": 2,
                    }
                ],
            }
        )
    pages[0]["citations"] = [p["citations"][0] for p in pages[1:]]
    with capture_repository_source(tmp_path) as source:
        bundle = SimpleNamespace(
            source_reader=source.borrow_reader(),
            code_graph=graph_fixture,
            entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
            graph_coverage={"available": True},
        )
        summary, views = visitor_graph.wiki_graph_views(
            bundle, pages, pages, lambda: None
        )
    assert summary["system_map"]["available"]
    assert summary["system_map"]["links"][0]["source"] == "caller"
    view = views["overview"]
    assert len(view["edges"]) == 1
    anchor = view["edges"][0]["anchors"][0]
    assert anchor["file"] == "caller.py" and anchor["line"] == 2
    assert "return callee()" in anchor["source"]["content"]
    assert all(node["source"] for node in view["nodes"])
    assert str(tmp_path) not in json.dumps(views)
