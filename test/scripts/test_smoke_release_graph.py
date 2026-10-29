# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def graph_smoke(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("scripts.smoke_release_graph")


@pytest.mark.parametrize("retain_old_target", [False, True])
def test_dirty_smoke_checks_rebuilt_target_before_closing_session(
    graph_smoke, monkeypatch, tmp_path, retain_old_target
):
    repo = tmp_path / "repository"
    repo.mkdir()
    source = repo / "calculator.py"
    source.write_text("def release_sum():\n    return 42\n", encoding="utf-8")
    agent_state = tmp_path / "agents.json"
    agent_state.write_text(json.dumps({"adds": 1}), encoding="utf-8")
    state = {"rebuilt": False, "closed": False, "enters": 0, "calls": []}
    fresh_checks = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            state["enters"] += 1
            return self

        async def __aexit__(self, *_args):
            state["closed"] = True

        async def call_tool(self, name, *, arguments):
            assert not state["closed"]
            assert name == "dependency_subgraph"
            assert arguments["symbol"] == "release_workflow"
            dirty = "release_sum_updated" in source.read_text(encoding="utf-8")
            state["calls"].append((dirty, state["rebuilt"]))
            if dirty and not state["rebuilt"]:
                return SimpleNamespace(
                    is_error=True,
                    structured_content=None,
                    content=[
                        SimpleNamespace(
                            text=(
                                "Indexed source changed. Run `codenib codegraph init` "
                                "for this repository and retry; this MCP connection "
                                "reloads rebuilt indexes."
                            )
                        )
                    ],
                )
            target = (
                "release_sum_updated"
                if state["rebuilt"] and not retain_old_target
                else "release_sum"
            )
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "nodes": [{"name": f"calculator.py:{target}()"}],
                    "edges": [{"source": "workflow", "target": target}],
                },
                content=[],
            )

    monkeypatch.setitem(
        sys.modules,
        "mcp",
        SimpleNamespace(Client=Client, StdioServerParameters=lambda **kwargs: kwargs),
    )
    monkeypatch.setitem(
        sys.modules,
        "mcp.client.stdio",
        SimpleNamespace(stdio_client=lambda *_args, **_kwargs: object()),
    )

    def run(command, **_kwargs):
        if list(command)[1:3] == ["codegraph", "init"]:
            state["rebuilt"] = True
        return SimpleNamespace(
            stdout=(
                " M calculator.py\n" if list(command)[:2] == ["git", "status"] else ""
            )
        )

    async def fresh_session(*_args, callee, **_kwargs):
        assert state["closed"]
        fresh_checks.append(callee)

    monkeypatch.setattr(graph_smoke, "_run", run)
    monkeypatch.setattr(graph_smoke, "_assert_codegraph_mcp_async", fresh_session)

    if retain_old_target:
        with pytest.raises(RuntimeError, match="did not reach the callee"):
            graph_smoke._assert_dirty_reinitialization(
                tmp_path, repo, executable="codenib", env={}, agent_state=agent_state
            )
        assert fresh_checks == []
    else:
        graph_smoke._assert_dirty_reinitialization(
            tmp_path, repo, executable="codenib", env={}, agent_state=agent_state
        )
        assert fresh_checks == ["release_sum_updated"]
    assert state["enters"] == 1
    assert state["calls"] == [(False, False), (True, False), (True, True)]
