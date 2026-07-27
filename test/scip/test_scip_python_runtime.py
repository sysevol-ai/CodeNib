# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
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


def test_scip_python_applies_temporary_excludes_without_dirtying_repo(tmp_path):
    indexer = SCIPPythonIndexer(
        tmp_path,
        output_dir=tmp_path / "out",
        exclude_patterns=["third_party/**", ".next/**"],
    )
    config_path = tmp_path / "pyrightconfig.json"

    with indexer._temporary_exclude_config():
        assert json.loads(config_path.read_text()) == {
            "exclude": [".next/**", "third_party/**"]
        }

    assert not config_path.exists()


def test_scip_python_preserves_project_owned_pyright_config(tmp_path):
    config_path = tmp_path / "pyrightconfig.json"
    original = '{"include": ["src"]}\n'
    config_path.write_text(original)
    indexer = SCIPPythonIndexer(
        tmp_path,
        output_dir=tmp_path / "out",
        exclude_patterns=["third_party/**"],
    )

    with indexer._temporary_exclude_config():
        assert config_path.read_text() == original

    assert config_path.read_text() == original
