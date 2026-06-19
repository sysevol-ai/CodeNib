# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for repository-level chunking language metadata."""

from pathlib import Path

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig
from codeminer.languages import extensions_for_language


def test_repo_chunking_config_defaults_come_from_language_registry():
    cfg = RepoChunkingConfig()

    assert cfg.python_extensions == extensions_for_language("python", "chunker")
    assert cfg.cpp_extensions == extensions_for_language("cpp", "chunker")
    assert cfg.rust_extensions == extensions_for_language("rust", "chunker")
    assert cfg.go_extensions == extensions_for_language("go", "chunker")
    assert cfg.java_extensions == extensions_for_language("java", "chunker")
    assert cfg.javascript_extensions == extensions_for_language("javascript", "chunker")
    assert cfg.typescript_extensions == extensions_for_language("typescript", "chunker")


def test_repo_discovery_normalizes_language_aliases(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.mjs").write_text("export const x = 1;\n")
    (tmp_path / "src" / "app.mts").write_text("export const y: number = 1;\n")

    cfg = RepoChunkingConfig(languages=["js", "ts"], filter_tests=False)
    chunker = CodeChunker(language="javascript", repo_config=cfg)

    files = chunker._discover_files(tmp_path, cfg.languages)
    discovered = {
        (path.relative_to(tmp_path).as_posix(), language) for path, language in files
    }

    assert discovered == {
        ("src/app.mjs", "javascript"),
        ("src/app.mts", "typescript"),
    }


def test_repo_discovery_respects_custom_extension_overrides(tmp_path: Path):
    (tmp_path / "tool.py").write_text("def ignored():\n    pass\n")
    (tmp_path / "tool.pyw").write_text("def included():\n    pass\n")

    cfg = RepoChunkingConfig(
        languages=["python"],
        python_extensions={".pyw"},
        filter_tests=False,
    )
    chunker = CodeChunker(language="python", repo_config=cfg)

    stats = chunker.get_repository_stats(str(tmp_path))

    assert stats["total_files"] == 1
    assert stats["files_by_language"]["python"][0]["path"] == "tool.pyw"


def test_repo_language_detection_uses_registered_chunk_extensions(tmp_path: Path):
    (tmp_path / "app.jsx").write_text("export const x = () => 1;\n")
    (tmp_path / "types.cts").write_text("export const y: number = 1;\n")
    (tmp_path / "main.go").write_text("package main\n")
    (tmp_path / "App.java").write_text("class App {}\n")

    chunker = CodeChunker(language="python")

    assert sorted(chunker._detect_repo_language(tmp_path)) == [
        "go",
        "java",
        "javascript",
        "typescript",
    ]
