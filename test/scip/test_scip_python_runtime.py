# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from codenib.scip_interface import scip_indexer_python
from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer


def _captured_subprocess_env(monkeypatch, tmp_path, node_options=None):
    indexer = SCIPPythonIndexer(tmp_path, output_dir=tmp_path / "out")
    monkeypatch.setattr(indexer, "_get_conda_env_bin", lambda: "/scip/bin")
    if node_options is None:
        monkeypatch.delenv("NODE_OPTIONS", raising=False)
    else:
        monkeypatch.setenv("NODE_OPTIONS", node_options)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scip_indexer_python, "_run_checked_with_timeout", fake_run)
    assert indexer._run_in_conda_env(["scip-python", "index"], Path(tmp_path))
    return captured["env"]


def test_scip_python_raises_default_node_heap_for_cold_large_repos(
    monkeypatch, tmp_path
):
    env = _captured_subprocess_env(monkeypatch, tmp_path)

    assert env["NODE_OPTIONS"] == "--max-old-space-size=16384"


def test_scip_python_preserves_explicit_node_heap(monkeypatch, tmp_path):
    env = _captured_subprocess_env(
        monkeypatch,
        tmp_path,
        "--trace-gc --max-old-space-size=24576",
    )

    assert env["NODE_OPTIONS"] == "--trace-gc --max-old-space-size=24576"
