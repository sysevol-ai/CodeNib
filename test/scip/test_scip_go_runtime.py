# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codenib.scip_interface.scip_indexer_base import SCIPIndexerBase
from codenib.scip_interface.scip_indexer_go import SCIPGoIndexer
from codenib.scip_interface.scip_indexer_rust import SCIPRustIndexer


def _go_indexer(tmp_path: Path) -> SCIPGoIndexer:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "go.mod").write_text("module example.test/repo\n", encoding="utf-8")
    return SCIPGoIndexer(repository, output_dir=tmp_path / "state")


def test_go_indexer_writes_directly_to_the_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexer = _go_indexer(tmp_path)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)

    def run(command, **kwargs):
        commands.append(tuple(command))
        assert kwargs["cwd"] == indexer.project_root
        assert not (indexer.project_root / "index.scip").exists()
        indexer.index_file.write_bytes(b"scip")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    assert indexer.generate_index() is True
    assert commands == [("scip-go", "--output", str(indexer.index_file))]
    assert indexer.index_file.read_bytes() == b"scip"
    assert not (indexer.project_root / "index.scip").exists()


@pytest.mark.parametrize("failure", ["command", "cancel"])
def test_go_indexer_discards_partial_state_output_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    indexer = _go_indexer(tmp_path)
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)

    def run(command, **_kwargs):
        indexer.index_file.write_bytes(b"partial")
        if failure == "command":
            raise subprocess.CalledProcessError(1, command)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", run)

    if failure == "command":
        assert indexer.generate_index() is False
    else:
        with pytest.raises(KeyboardInterrupt):
            indexer.generate_index()
    assert not indexer.index_file.exists()
    assert not (indexer.project_root / "index.scip").exists()


def test_rust_pipeline_consumes_read_only_project_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    captured: dict[str, object] = {}

    def run_pipeline(_self, **kwargs):
        captured.update(kwargs)
        return "graph"

    monkeypatch.setattr(SCIPIndexerBase, "run_pipeline", run_pipeline)
    indexer = SCIPRustIndexer(repository, output_dir=tmp_path / "state")

    assert (
        indexer.run_pipeline(
            allow_project_preparation=False,
            custom_option="kept",
            report_profile=False,
        )
        == "graph"
    )
    assert "allow_project_preparation" not in captured
    assert captured["custom_option"] == "kept"
    assert captured["report_profile"] is False
