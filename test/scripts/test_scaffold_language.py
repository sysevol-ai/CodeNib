# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for scripts/scaffold_language.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "scaffold_language.py"
_SPEC = importlib.util.spec_from_file_location(
    "_scaffold_language_for_tests", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
scaffold = importlib.util.module_from_spec(_SPEC)
sys.modules["_scaffold_language_for_tests"] = scaffold
_SPEC.loader.exec_module(scaffold)


def _config(**overrides):
    base = {
        "key": "zig",
        "display_name": "Zig",
        "extensions": (".zig",),
    }
    base.update(overrides)
    return scaffold.ScaffoldConfig(**base)


def test_chunker_only_plan_is_small_and_prints_registry_snippet():
    cfg = _config(aliases=("ziglang",))

    files = scaffold.build_plan(cfg)
    rendered = scaffold.render_plan(cfg, files)

    assert [str(file.path) for file in files] == [
        "codeminer/code_chunking/zig_chunker.py",
        "test/chunker/test_zig_chunker.py",
    ]
    assert "LanguageSpec(" in rendered
    assert "chunk_extensions=('.zig',)" in rendered
    assert "graph_language" not in scaffold.render_language_spec(cfg)
    assert "codeminer/code_chunking/__init__.py" in rendered


def test_scip_incremental_core_plan_covers_graph_patcher_and_parity_stubs():
    cfg = _config(
        graph_backend="scip",
        incremental_backend="lsp",
        core_decoder=True,
    )

    paths = {str(file.path) for file in scaffold.build_plan(cfg)}

    assert "codeminer/scip_interface/scip_indexer_zig.py" in paths
    assert "codeminer/scip_interface/scip_decode_zig.py" in paths
    assert "codeminer/graph/incremental/patcher_zig.py" in paths
    assert "core/scip_decode_zig.h" in paths
    assert "core/scip_decode_zig.cpp" in paths
    assert "test/scip/test_core_zig.py" in paths


def test_lsp_plan_uses_generic_lsp_registry_paths():
    cfg = _config(graph_backend="lsp")

    paths = {str(file.path) for file in scaffold.build_plan(cfg)}
    rendered = scaffold.render_language_spec(cfg)

    assert "codeminer/ls_index/zig_indexer.py" not in paths
    assert "codeminer/ls_index/zig_decode.py" not in paths
    assert "test/ls_index/test_zig_lsp.py" in paths
    assert all("scip_interface" not in path for path in paths)
    assert "lsp_indexer:GenericLSPIndexer" in rendered
    assert "lsp_graph_decode:GenericLSPGraphDecoder" in rendered
    assert "CODEMINER_ZIG_LSP_CMD" in rendered


def test_lsp_plan_can_record_scip_cold_start_candidate():
    cfg = _config(
        graph_backend="lsp",
        scip_cold_start_candidate="scip-zig",
        scip_cold_start_command=("scip-zig", "index"),
    )

    paths = {str(file.path) for file in scaffold.build_plan(cfg)}
    rendered = scaffold.render_language_spec(cfg)
    plan = scaffold.render_plan(cfg, scaffold.build_plan(cfg))

    assert "codeminer/scip_interface/scip_indexer_zig.py" not in paths
    assert "ScipColdStartOption(" in rendered
    assert "tool='scip-zig'" in rendered
    assert "status='candidate'" in rendered
    assert "command=('scip-zig', 'index')" in rendered
    assert "CODEMINER_ZIG_SCIP_CMD" in rendered
    assert "candidate disabled" in plan


def test_main_dry_run_prints_without_writing(tmp_path, capsys):
    rc = scaffold.main(
        [
            "zig",
            "--extension",
            "zig",
            "--alias",
            "ziglang",
            "--root",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out

    assert rc == 0
    assert "Dry run only" in out
    assert "chunk_extensions=('.zig',)" in out
    assert not (tmp_path / "codeminer").exists()


def test_main_write_creates_files_and_refuses_overwrite(tmp_path, capsys):
    args = ["zig", "--extension", ".zig", "--root", str(tmp_path), "--write"]

    assert scaffold.main(args) == 0
    assert (tmp_path / "codeminer/code_chunking/zig_chunker.py").exists()
    assert (tmp_path / "test/chunker/test_zig_chunker.py").exists()

    assert scaffold.main(args) == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err


def test_main_validates_core_backend(capsys):
    with pytest.raises(SystemExit):
        scaffold.main(
            [
                "zig",
                "--extension",
                ".zig",
                "--graph-backend",
                "lsp",
                "--core-decoder",
            ]
        )

    assert (
        "--core-decoder currently requires --graph-backend scip"
        in capsys.readouterr().err
    )


def test_main_validates_scip_candidate_command(capsys):
    with pytest.raises(SystemExit):
        scaffold.main(
            [
                "zig",
                "--extension",
                ".zig",
                "--graph-backend",
                "lsp",
                "--scip-cold-start-command",
                "scip-zig index",
            ]
        )

    assert (
        "--scip-cold-start-command requires --scip-cold-start-candidate"
        in capsys.readouterr().err
    )


def test_main_validates_existing_registry_key(capsys):
    with pytest.raises(SystemExit):
        scaffold.main(["python", "--extension", ".py"])

    assert "already registered" in capsys.readouterr().err
