# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codenib import cli
from codenib.artifacts import CONTEXT_ARTIFACT_MANIFEST
from codenib.compiler.index_compiler import IndexCompiler
from codenib.paths import repo_index_dir
from codenib.web.static_export import STATIC_EXPORT_MANIFEST


def _frontend(root: Path) -> Path:
    frontend = root / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><html><head><base href='/'></head><body>"
        "<script src='./runtime-config.js'></script>"
        "<script type='module' src='./assets/app.js'></script></body></html>"
    )
    (frontend / "runtime-config.js").write_text('window.__CODENIB_API_BASE__ = "";\n')
    (frontend / "assets" / "app.js").write_text("console.log('wiki');\n")
    return frontend


def test_publish_builds_static_site_and_portable_context_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text(
        "def run(value: int) -> int:\n    return value + 1\n"
    )
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    _git(repo, "add", "runtime.py")
    _git(repo, "commit", "--quiet", "-m", "initial")
    generated = repo / "dist"
    generated.mkdir()
    (generated / "bundle.js").write_text("generated output\n")
    site = tmp_path / "published" / "site"
    context = tmp_path / "published" / "context"
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "Example/Project")

    result = cli.run(
        [
            "publish",
            str(repo),
            "--preset",
            "fast",
            "--site-output",
            str(site),
            "--context-output",
            str(context),
            "--base-path",
            "/project",
            "--frontend-dir",
            str(_frontend(tmp_path)),
        ]
    )

    assert result == 0
    assert (site / "index.html").is_file()
    static_metadata = json.loads((site / STATIC_EXPORT_MANIFEST).read_text())
    context_metadata = json.loads((context / CONTEXT_ARTIFACT_MANIFEST).read_text())
    assert static_metadata["base_path"] == "/project"
    assert context_metadata["repository"]["slug"] == "example/project"
    assert context_metadata["views"] == ["bm25"]
    assert (context / "views" / "bm25").is_dir()
    output = capsys.readouterr().out
    assert "Published Wiki:" in output
    assert "Context artifact:" in output


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _clean_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    (repo / "runtime.py").write_text("VALUE = 1\n")
    _git(repo, "add", "runtime.py")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


@pytest.mark.parametrize("change", ["tracked", "untracked"])
def test_publish_rejects_source_visible_dirty_checkout_before_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    repo = _clean_repo(tmp_path)
    if change == "tracked":
        (repo / "runtime.py").write_text("VALUE = 2\n")
    else:
        (repo / "new_module.py").write_text("VALUE = 2\n")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        cli,
        "_run_index",
        lambda *_args, **_kwargs: pytest.fail("publish indexed a dirty checkout"),
    )

    result = cli.run(
        [
            "publish",
            str(repo),
            "--site-output",
            str(tmp_path / "site"),
            "--context-output",
            str(tmp_path / "context"),
        ]
    )

    assert result == 2
    assert "require a clean Git checkout" in capsys.readouterr().err


def test_artifact_pack_rejects_non_git_checkout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("VALUE = 1\n")

    result = cli.run(
        [
            "artifact",
            "pack",
            str(repo),
            "--output",
            str(tmp_path / "context"),
        ]
    )

    assert result == 2
    assert "require a clean Git checkout" in capsys.readouterr().err


def test_publish_second_commit_uses_incremental_compiler_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    source = repo / "runtime.py"
    source.write_text("def run() -> int:\n    return 1\n")
    _git(repo, "add", "runtime.py")
    _git(repo, "commit", "--quiet", "-m", "initial")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    frontend = _frontend(tmp_path)
    site = tmp_path / "published" / "site"
    context = tmp_path / "published" / "context"
    command = [
        "publish",
        str(repo),
        "--preset",
        "fast",
        "--site-output",
        str(site),
        "--context-output",
        str(context),
        "--frontend-dir",
        str(frontend),
    ]

    assert cli.run(command) == 0
    first_commit = json.loads((context / CONTEXT_ARTIFACT_MANIFEST).read_text())[
        "repository"
    ]["commit"]

    source.write_text("def run() -> int:\n    return 2\n")
    _git(repo, "add", "runtime.py")
    _git(repo, "commit", "--quiet", "-m", "update")
    calls: list[tuple[tuple, dict]] = []
    original = IndexCompiler.update_repo

    def recording_update(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(IndexCompiler, "update_repo", recording_update)

    assert cli.run(command) == 0
    second_commit = _git(repo, "rev-parse", "HEAD")
    metadata = json.loads((context / CONTEXT_ARTIFACT_MANIFEST).read_text())
    assert calls
    assert first_commit != second_commit
    assert metadata["repository"]["commit"] == second_commit
    assert metadata["source_locations"]["commit"] == second_commit


def test_publish_rejects_nested_site_and_context_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("VALUE = 1\n")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    output = tmp_path / "published"
    monkeypatch.setattr(
        cli,
        "_run_index",
        lambda *_args, **_kwargs: pytest.fail("publish indexed before preflight"),
    )

    result = cli.run(
        [
            "publish",
            str(repo),
            "--site-output",
            str(output),
            "--context-output",
            str(output / "context"),
            "--frontend-dir",
            str(_frontend(tmp_path)),
        ]
    )

    assert result == 2
    assert not output.exists()


def test_publish_rejects_output_inside_repository_before_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("VALUE = 1\n")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        cli,
        "_run_index",
        lambda *_args, **_kwargs: pytest.fail("publish indexed before preflight"),
    )

    result = cli.run(
        [
            "publish",
            str(repo),
            "--site-output",
            str(repo / "published"),
            "--context-output",
            str(tmp_path / "context"),
            "--frontend-dir",
            str(_frontend(tmp_path)),
        ]
    )

    assert result == 2
    assert not (repo / "published").exists()


@pytest.mark.parametrize(
    ("output_option", "protected_name"),
    [
        ("--site-output", "repository"),
        ("--context-output", "index"),
    ],
)
def test_publish_rejects_symlinked_output_ancestor_before_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_option: str,
    protected_name: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("VALUE = 1\n")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    protected = repo if protected_name == "repository" else repo_index_dir(repo)
    protected.mkdir(parents=True, exist_ok=True)
    alias = tmp_path / "output-alias"
    alias.symlink_to(protected, target_is_directory=True)
    monkeypatch.setattr(
        cli,
        "_run_index",
        lambda *_args, **_kwargs: pytest.fail("publish indexed before preflight"),
    )
    site = tmp_path / "site"
    context = tmp_path / "context"
    if output_option == "--site-output":
        site = alias / "published"
    else:
        context = alias / "published"

    result = cli.run(
        [
            "publish",
            str(repo),
            "--site-output",
            str(site),
            "--context-output",
            str(context),
            "--frontend-dir",
            str(_frontend(tmp_path)),
        ]
    )

    assert result == 2
    assert not (protected / "published").exists()


def test_publication_environment_marks_custom_embedding_key_as_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_EMBEDDING_CREDENTIAL", "runtime-secret-value")

    environment = cli._publication_environment("CUSTOM_EMBEDDING_CREDENTIAL")

    assert environment["CODENIB_PUBLICATION_CREDENTIAL_SECRET"] == (
        "runtime-secret-value"
    )
