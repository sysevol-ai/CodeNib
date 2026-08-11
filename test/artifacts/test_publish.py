# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import codenib.artifacts.security as security_module
from codenib import cli
from codenib._atomic_directory import (
    PublicationDirectoryReader,
    capture_directory_ownership,
    reopen_authenticated_directory,
)
from codenib.artifacts import CONTEXT_ARTIFACT_MANIFEST
from codenib.artifacts.security import (
    assert_publishable_json_value,
    assert_publishable_tree_reader,
)
from codenib.compiler.index_compiler import IndexCompiler
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


def test_publication_environment_marks_custom_embedding_key_as_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_EMBEDDING_CREDENTIAL", "runtime-secret-value")

    environment = cli._publication_environment("CUSTOM_EMBEDDING_CREDENTIAL")

    assert environment["CODENIB_PUBLICATION_CREDENTIAL_SECRET"] == (
        "runtime-secret-value"
    )


@pytest.mark.parametrize("value", [12345678, 12345678.0])
def test_publishability_scans_canonical_numeric_scalars(value: object) -> None:
    with pytest.raises(ValueError, match="configured credential"):
        assert_publishable_json_value(
            {"innocent": value},
            forbidden_paths=(),
            environ={"MY_TOKEN": "12345678"},
            label="numeric config",
        )


def test_publishability_always_rejects_lexical_path_when_resolve_is_redirected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = tmp_path / "owned-repository"
    foreign = tmp_path / "foreign-repository"
    real_resolve = Path.resolve

    def redirected_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == lexical:
            return foreign
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)

    with pytest.raises(ValueError, match="absolute build-machine path"):
        assert_publishable_json_value(
            {"path": str(lexical)},
            forbidden_paths=(lexical,),
            environ={},
            label="redirected config",
        )


@pytest.mark.parametrize("relative", ["config.json", "note.txt"])
def test_publishability_reader_rejects_lexical_path_when_resolve_is_redirected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    lexical = tmp_path / "owned-repository"
    foreign = tmp_path / "foreign-repository"
    root = tmp_path / "publishable"
    root.mkdir()
    payload = (
        json.dumps({"path": str(lexical)})
        if relative.endswith(".json")
        else f"source={lexical}\n"
    )
    (root / relative).write_text(payload, encoding="utf-8")
    ownership = capture_directory_ownership(root)
    real_resolve = Path.resolve

    def redirected_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == lexical:
            return foreign
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(lexical,),
            environ={},
            label="redirected tree",
        )

    with pytest.raises(ValueError, match="absolute build-machine path"):
        reopen_authenticated_directory(root, ownership, validate)


def _validate_publishable_reader(
    root: Path,
    *,
    environ: dict[str, str] | None = None,
) -> None:
    ownership = capture_directory_ownership(root)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={} if environ is None else environ,
            label="bounded publication",
        )

    reopen_authenticated_directory(root, ownership, validate)


@pytest.mark.parametrize(
    ("limit_name", "limit", "payload", "message"),
    [
        (
            "_MAX_PUBLISHABLE_JSON_NODES",
            4,
            b'{"a":0,"b":1,"c":2,"d":3}',
            "4-node limit",
        ),
        (
            "_MAX_PUBLISHABLE_JSON_TOKENS",
            4,
            b'{"a":0,"b":1}',
            "4-token limit",
        ),
        (
            "_MAX_PUBLISHABLE_JSON_DEPTH",
            3,
            b'{"a":{"b":{"c":0}}}',
            "3-level depth limit",
        ),
        (
            "_MAX_PUBLISHABLE_JSON_KEY_BYTES",
            4,
            b'{"abcde":0}',
            "key exceeding 4 bytes",
        ),
        (
            "_MAX_PUBLISHABLE_JSON_STRING_BYTES",
            4,
            b'{"a":"12345"}',
            "string exceeds its 4-byte limit",
        ),
        (
            "_MAX_PUBLISHABLE_JSON_ATOM_BYTES",
            4,
            b'{"a":12345}',
            "atom exceeds its 4-byte limit",
        ),
    ],
)
def test_publishability_reader_rejects_json_complexity_before_dom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    payload: bytes,
    message: str,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_bytes(payload)
    monkeypatch.setattr(security_module, limit_name, limit)

    def forbidden_loads(*_args: object, **_kwargs: object) -> object:
        pytest.fail("lexical complexity must be rejected before DOM allocation")

    monkeypatch.setattr(security_module.json, "loads", forbidden_loads)

    with pytest.raises(ValueError, match=message):
        _validate_publishable_reader(root)


@pytest.mark.parametrize(
    "payload",
    [b"{}", b"[]", b'"scalar"', b"7", b"true", b"null"],
)
def test_publishability_reader_accepts_bounded_arbitrary_top_level_json(
    tmp_path: Path,
    payload: bytes,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_bytes(payload)

    _validate_publishable_reader(root)


def test_publishability_reader_decodes_utf8_before_strict_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_bytes('{"value":"雪"}'.encode("utf-8"))
    real_loads = json.loads
    observed: list[str] = []

    def observe_loads(serialized: str, **kwargs: object) -> object:
        assert type(serialized) is str
        observed.append(serialized)
        return real_loads(serialized, **kwargs)

    monkeypatch.setattr(security_module.json, "loads", observe_loads)

    _validate_publishable_reader(root)

    assert observed == ['{"value":"雪"}']


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xef\xbb\xbf0", id="utf-8-bom"),
        pytest.param("0".encode("utf-16"), id="utf-16-bom"),
        pytest.param("0".encode("utf-16-le"), id="utf-16-le"),
        pytest.param("0".encode("utf-16-be"), id="utf-16-be"),
        pytest.param("0".encode("utf-32"), id="utf-32-bom"),
        pytest.param("0".encode("utf-32-le"), id="utf-32-le"),
        pytest.param("0".encode("utf-32-be"), id="utf-32-be"),
    ],
)
def test_publishability_reader_requires_utf8_without_bom(
    tmp_path: Path,
    payload: bytes,
) -> None:
    # Python's bytes decoder accepts these representations. Publication policy
    # deliberately narrows that ambient behavior to one portable encoding.
    assert json.loads(payload) == 0
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_bytes(payload)

    with pytest.raises(ValueError, match="UTF-8 without BOM required"):
        _validate_publishable_reader(root)


def test_publishability_reader_casefolds_json_suffix_before_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "CONFIG.JSON").write_text('{"safe":1,"\\u0073afe":2}', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        _validate_publishable_reader(root)


@pytest.mark.parametrize("payload", [b"NaN", b"Infinity", b"-Infinity"])
def test_publishability_reader_preserves_nonfinite_json_rejection(
    tmp_path: Path,
    payload: bytes,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_bytes(payload)

    with pytest.raises(ValueError, match="invalid JSON"):
        _validate_publishable_reader(root)


def test_publishability_reader_preserves_decoded_secret_rejection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_text(
        '{"value":"runtime-secret-value"}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="configured credential"):
        _validate_publishable_reader(
            root,
            environ={"CUSTOM_TOKEN": "runtime-secret-value"},
        )
