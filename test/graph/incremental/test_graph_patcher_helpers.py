# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest
import test_graph_patcher as graph_patch


def test_failed_scip_build_is_not_retried(tmp_path, monkeypatch):
    """The cache-aware pipeline already attempted the failed cold build."""
    calls = []

    def fail_build(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(graph_patch, "_git", lambda *_args, **_kwargs: "source-sha")
    monkeypatch.setattr(
        graph_patch,
        "LSIndexer",
        lambda **_kwargs: SimpleNamespace(run_pipeline=fail_build),
    )

    with pytest.raises(AssertionError, match="SCIP run_pipeline returned None"):
        graph_patch._build_graph_with_scip(tmp_path, "python", tmp_path / "cache")

    assert calls == [{"skip_level": "graph", "report_profile": False}]


@pytest.mark.parametrize("failure_at", ["start", "build"])
def test_python_fixture_finalizes_lsp_on_failure(tmp_path, monkeypatch, failure_at):
    """Pytest owns cleanup even if startup or cold indexing raises."""
    events = []
    finalizers = []

    def start():
        events.append("start")
        if failure_at == "start":
            raise RuntimeError("fixture failed")

    def build(*_args, **_kwargs):
        raise RuntimeError("fixture failed")

    patcher = SimpleNamespace(start_lsp=start, stop_lsp=lambda: events.append("stop"))
    monkeypatch.setattr(graph_patch, "_get_earlier_commit", lambda *_args: "earlier")
    monkeypatch.setattr(graph_patch, "_git", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(graph_patch, "GraphPatcher", lambda *_args: patcher)
    monkeypatch.setattr(graph_patch, "_build_graph_with_scip", build)
    request = SimpleNamespace(addfinalizer=finalizers.append)

    with pytest.raises(RuntimeError, match="fixture failed"):
        graph_patch.TestSWEBenchPython().test_graph_patch(tmp_path, tmp_path, request)
    for finalize in reversed(finalizers):
        finalize()

    assert events == ["start", "stop"]
