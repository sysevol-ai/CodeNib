# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for repository-level chunking language metadata."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from codenib.languages import extensions_for_language
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositoryChangedError,
    RepositorySourceBinding,
    capture_repository_source,
)


def test_repo_chunking_config_defaults_come_from_language_registry():
    cfg = RepoChunkingConfig()

    assert cfg.python_extensions == extensions_for_language("python", "chunker")
    assert cfg.cpp_extensions == extensions_for_language("cpp", "chunker")
    assert cfg.csharp_extensions == extensions_for_language("csharp", "chunker")
    assert cfg.rust_extensions == extensions_for_language("rust", "chunker")
    assert cfg.go_extensions == extensions_for_language("go", "chunker")
    assert cfg.java_extensions == extensions_for_language("java", "chunker")
    assert cfg.ruby_extensions == extensions_for_language("ruby", "chunker")
    assert cfg.php_extensions == extensions_for_language("php", "chunker")
    assert cfg.kotlin_extensions == extensions_for_language("kotlin", "chunker")
    assert cfg.javascript_extensions == extensions_for_language("javascript", "chunker")
    assert cfg.typescript_extensions == extensions_for_language("typescript", "chunker")


def test_repo_chunking_config_retains_an_exact_detached_selection_snapshot():
    selection = RepositorySourceSelection(["src/private"])
    cfg = RepoChunkingConfig(source_selection=selection)

    object.__setattr__(selection, "exclude_subtrees", ("src/changed",))

    assert cfg.source_selection is not selection
    assert cfg.source_selection.exclude_subtrees == ("src/private",)
    with pytest.raises(TypeError, match="RepositorySourceSelection"):
        RepoChunkingConfig(source_selection=object())


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


def test_repo_discovery_applies_exact_selection_before_file_reads(
    tmp_path: Path,
    monkeypatch,
):
    paths = (
        "src/app.py",
        "src/private/secret.py",
        "src/private_api/public.py",
        "other/private/also_public.py",
    )
    for relative in paths:
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("value = 1\n", encoding="utf-8")

    cfg = RepoChunkingConfig(
        languages=["python"],
        filter_tests=False,
        source_selection=RepositorySourceSelection(["src/private"]),
    )
    chunker = CodeChunker(language="python", repo_config=cfg)
    inspected = []

    def inspect_minification(file_path):
        inspected.append(file_path.relative_to(tmp_path).as_posix())
        return False

    monkeypatch.setattr(chunker, "_is_minified_file", inspect_minification)

    files = chunker._discover_files(tmp_path, cfg.languages)
    discovered = [path.relative_to(tmp_path).as_posix() for path, _ in files]

    assert discovered == [
        "other/private/also_public.py",
        "src/app.py",
        "src/private_api/public.py",
    ]
    assert sorted(inspected) == discovered
    assert "src/private/secret.py" not in inspected


def test_repo_language_detection_ignores_excluded_subtrees(tmp_path: Path):
    excluded = tmp_path / "generated" / "main.go"
    excluded.parent.mkdir()
    excluded.write_text("package main\n", encoding="utf-8")
    included = tmp_path / "lib" / "app.rb"
    included.parent.mkdir()
    included.write_text("class App\nend\n", encoding="utf-8")
    cfg = RepoChunkingConfig(
        source_selection=RepositorySourceSelection(["generated"]),
    )
    chunker = CodeChunker(language="python", repo_config=cfg)

    assert chunker._detect_repo_language(tmp_path) == ["ruby"]


def test_repository_chunking_rejects_an_excluded_chunk_result(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    cfg = RepoChunkingConfig(
        languages=["python"],
        filter_tests=False,
        source_selection=RepositorySourceSelection(["src/private"]),
    )
    chunker = CodeChunker(language="python", repo_config=cfg)
    monkeypatch.setattr(
        chunker,
        "_discover_files",
        lambda *_args, **_kwargs: [(source, "python")],
    )
    monkeypatch.setattr(
        chunker,
        "_chunk_file_with_language",
        lambda *_args: [SimpleNamespace(file="src/private/secret.py")],
    )

    with pytest.raises(RuntimeError, match="leaked excluded chunk"):
        chunker.chunk_repository(str(tmp_path), strict=True)


def test_authenticated_repository_chunking_uses_retained_bytes_and_paths(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / "src" / "private").mkdir(parents=True)
    (repo / "src" / "app.py").write_bytes(b"def app():\r\n    return 'retained'\r\n")
    (repo / "src" / "private" / "secret.py").write_text(
        "def secret():\n    return 1\n",
        encoding="utf-8",
    )
    selection = RepositorySourceSelection(("src/private",))
    config = RepoChunkingConfig(
        languages=["python"],
        filter_tests=False,
        source_selection=selection,
    )
    chunker = CodeChunker(language="python", repo_config=config)

    with capture_repository_source(repo, selection=selection) as source:
        chunks = chunker.chunk_repository_source(source)

    assert {chunk.file for chunk in chunks} == {"src/app.py"}
    assert {chunk.node_id for chunk in chunks} == {"src/app.py:app()"}
    assert all("\r" not in chunk.content for chunk in chunks)
    assert "retained" in chunks[0].content


def test_authenticated_repository_chunking_threads_cancellation_into_source_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    chunker = CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(languages=["python"]),
    )
    real_snapshot = RepositorySourceBinding.authenticated_identity_snapshot
    real_session = RepositorySourceBinding.read_session
    observed = []

    def tracked_snapshot(binding, *args, **kwargs):
        observed.append(("identity", kwargs.get("check_cancelled")))
        return real_snapshot(binding, *args, **kwargs)

    def tracked_session(binding, *args, **kwargs):
        observed.append(("session", kwargs.get("check_cancelled")))
        return real_session(binding, *args, **kwargs)

    monkeypatch.setattr(
        RepositorySourceBinding,
        "authenticated_identity_snapshot",
        tracked_snapshot,
    )
    monkeypatch.setattr(RepositorySourceBinding, "read_session", tracked_session)

    def check_cancelled() -> None:
        return None

    with capture_repository_source(repo) as source:
        chunker.chunk_repository_source(source, check_cancelled=check_cancelled)

    assert observed == [
        ("identity", check_cancelled),
        ("session", check_cancelled),
    ]


def test_authenticated_repository_chunking_rejects_policy_mismatch_before_reads(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    chunker = CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(
            languages=["python"],
            source_selection=RepositorySourceSelection(("generated",)),
        ),
    )

    with capture_repository_source(repo) as source:
        with pytest.raises(RuntimeError, match="differs from chunker policy"):
            chunker.chunk_repository_source(source)
        assert source.usable


def test_authenticated_repository_chunking_rejects_changed_source(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source_path = repo / "app.py"
    source_path.write_text("def app():\n    return 1\n", encoding="utf-8")
    chunker = CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(languages=["python"]),
    )

    with capture_repository_source(repo) as source:
        source_path.write_text("def replaced():\n    return 2\n", encoding="utf-8")
        with pytest.raises(RepositoryChangedError):
            chunker.chunk_repository_source(source)


def test_strict_repository_chunking_rejects_partial_results(tmp_path, monkeypatch):
    source = tmp_path / "broken.py"
    source.write_text("def broken():\n    pass\n", encoding="utf-8")
    cfg = RepoChunkingConfig(languages=["python"], filter_tests=False)
    chunker = CodeChunker(language="python", repo_config=cfg)

    def fail_chunking(*_args):
        raise ValueError("parser failed")

    monkeypatch.setattr(chunker, "_chunk_file_with_language", fail_chunking)

    assert chunker.chunk_repository(str(tmp_path)) == []
    with pytest.raises(
        RuntimeError,
        match=r"Failed to chunk repository source broken\.py: parser failed",
    ):
        chunker.chunk_repository(str(tmp_path), strict=True)


def test_repo_discovery_is_sorted_independently_of_walk_order(
    tmp_path: Path, monkeypatch
):
    for directory in (tmp_path, tmp_path / "zeta", tmp_path / "alpha"):
        directory.mkdir(exist_ok=True)
        (directory / "z.py").write_text("z = 1\n", encoding="utf-8")
        (directory / "a.py").write_text("a = 1\n", encoding="utf-8")

    def reverse_walk(root: Path):
        root = Path(root)
        directories = ["zeta", "alpha"]
        yield str(root), directories, ["z.py", "a.py"]
        for directory in directories:
            yield str(root / directory), [], ["z.py", "a.py"]

    monkeypatch.setattr("codenib.code_chunker.os.walk", reverse_walk)
    cfg = RepoChunkingConfig(languages=["python"], filter_tests=False)
    chunker = CodeChunker(language="python", repo_config=cfg)

    files = chunker._discover_files(tmp_path, cfg.languages)

    assert [path.relative_to(tmp_path).as_posix() for path, _ in files] == [
        "a.py",
        "alpha/a.py",
        "alpha/z.py",
        "z.py",
        "zeta/a.py",
        "zeta/z.py",
    ]


def test_repo_discovery_skips_generated_and_vendored_trees(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def app():\n    return 1\n")
    for directory in (
        "site",
        ".next",
        ".codenib_cache",
        ".codenib-action",
        "third_party",
        "vendor",
    ):
        ignored = tmp_path / directory
        ignored.mkdir()
        (ignored / "ignored.py").write_text("def ignored():\n    return 0\n")

    cfg = RepoChunkingConfig(languages=["python"], filter_tests=False)
    chunker = CodeChunker(language="python", repo_config=cfg)

    files = chunker._discover_files(tmp_path, cfg.languages)

    assert [path.relative_to(tmp_path).as_posix() for path, _ in files] == [
        "src/app.py"
    ]


def test_repo_language_detection_uses_registered_chunk_extensions(tmp_path: Path):
    (tmp_path / "app.jsx").write_text("export const x = () => 1;\n")
    (tmp_path / "types.cts").write_text("export const y: number = 1;\n")
    (tmp_path / "main.go").write_text("package main\n")
    (tmp_path / "Program.cs").write_text("class Program {}\n")
    (tmp_path / "App.java").write_text("class App {}\n")
    (tmp_path / "app.rb").write_text("class App\nend\n")
    (tmp_path / "index.php").write_text("<?php\nfunction index() {}\n")
    (tmp_path / "Main.kt").write_text("class Main {}\n")
    hidden_tool = tmp_path / ".codenib-action"
    hidden_tool.mkdir()
    (hidden_tool / "Hidden.swift").write_text("struct Hidden {}\n")

    chunker = CodeChunker(language="python")

    assert sorted(chunker._detect_repo_language(tmp_path)) == [
        "csharp",
        "go",
        "java",
        "javascript",
        "kotlin",
        "php",
        "ruby",
        "typescript",
    ]
