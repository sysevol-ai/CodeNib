# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib._secret_fields as secret_fields_module
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


def test_publishability_reader_streams_exact_caller_validated_json(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "documents.json").write_text(
        json.dumps([{"page_content": "safe", "metadata": {}}]),
        encoding="utf-8",
    )
    ownership = capture_directory_ownership(root)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={},
            label="streamed tree",
            max_json_bytes=8,
            streaming_json_paths=("documents.json",),
        )

    reopen_authenticated_directory(root, ownership, validate)


def test_publishability_reader_streaming_paths_is_explicit_keyword_api() -> None:
    signature = inspect.signature(assert_publishable_tree_reader)

    assert list(signature.parameters) == [
        "reader",
        "forbidden_paths",
        "environ",
        "label",
        "max_json_bytes",
        "streaming_json_paths",
    ]
    assert signature.parameters["streaming_json_paths"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert signature.parameters["streaming_json_paths"].default == ()


def test_publishability_reader_streamed_json_still_has_lexical_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    # The security layer rechecks bounded lexical framing; the caller-owned
    # semantic validator is authoritative for exact JSON grammar/canonicality.
    (root / "documents.json").write_bytes(b'{"truncated": [1, 2')
    ownership = capture_directory_ownership(root)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={},
            label="streamed tree",
            max_json_bytes=8,
            streaming_json_paths=("documents.json",),
        )

    with pytest.raises(ValueError, match="invalid JSON|truncated JSON"):
        reopen_authenticated_directory(root, ownership, validate)


def test_publishability_reader_streamed_json_still_has_full_secret_scan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    secret = "configured-secret-value"
    (root / "documents.json").write_text(
        json.dumps([{"page_content": secret, "metadata": {}}]),
        encoding="utf-8",
    )
    ownership = capture_directory_ownership(root)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={"CODENIB_DEMO_API_KEY": secret},
            label="streamed tree",
            max_json_bytes=8,
            streaming_json_paths=("documents.json",),
        )

    with pytest.raises(ValueError, match="configured credential"):
        reopen_authenticated_directory(root, ownership, validate)


def test_publishability_reader_does_not_whitelist_json_by_basename(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    (root / "nested").mkdir(parents=True)
    (root / "documents.json").write_text("[]\n", encoding="utf-8")
    (root / "nested" / "documents.json").write_text(
        json.dumps([{"unexpected": "large-enough"}]),
        encoding="utf-8",
    )
    ownership = capture_directory_ownership(root)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={},
            label="streamed tree",
            max_json_bytes=8,
            streaming_json_paths=("documents.json",),
        )

    with pytest.raises(ValueError, match="nested/documents.json"):
        reopen_authenticated_directory(root, ownership, validate)


def test_publishability_reader_requires_every_streaming_json_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_text("{}\n", encoding="utf-8")
    ownership = capture_directory_ownership(root)

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={},
            label="streamed tree",
            streaming_json_paths=("documents.json",),
        )

    with pytest.raises(ValueError, match="path is absent: documents.json"):
        reopen_authenticated_directory(root, ownership, validate)


@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, ValueError, KeyboardInterrupt, BaseException],
)
def test_publishability_record_paths_stop_before_poisoned_future(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    poisoned = False

    class PoisonedRecords(tuple[object, ...]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal poisoned
            yield tuple.__getitem__(self, 0)
            poisoned = True
            raise AssertionError(
                "publishability validation consumed its poisoned future record"
            )

    records = PoisonedRecords(
        (
            SimpleNamespace(path="documents.json", size=2),
            SimpleNamespace(path="poison.json", size=2),
        )
    )
    reader = SimpleNamespace(
        capture_ownership=lambda **_kwargs: object(),
        open_authenticated_file=lambda *_args, **_kwargs: pytest.fail(
            "cancellation must precede file consumption"
        ),
    )
    monkeypatch.setattr(
        security_module,
        "directory_ownership_file_records",
        lambda _ownership: records,
    )
    stop = error_type("injected publishability record cancellation")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(BaseException) as caught:
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={},
            label="poisoned publication",
            streaming_json_paths=("documents.json",),
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


def test_publishability_missing_streaming_path_precedes_armed_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = False

    class ArmingRecord:
        size = 2

        @property
        def path(self) -> str:
            nonlocal armed
            armed = True
            return "other.json"

    reader = SimpleNamespace(capture_ownership=lambda **_kwargs: object())
    monkeypatch.setattr(
        security_module,
        "directory_ownership_file_records",
        lambda _ownership: (ArmingRecord(),),
    )
    stop = KeyboardInterrupt("must not precede the missing-path mismatch")

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(ValueError, match="path is absent: documents.json"):
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={},
            label="missing streamed publication",
            streaming_json_paths=("documents.json",),
            check_cancelled=check_cancelled,
        )

    assert armed


def test_publishability_final_streaming_path_error_precedes_armed_stop() -> None:
    cancellation_calls = 0
    stop = BaseException("must not precede the final duplicate-path error")

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        if cancellation_calls > 1:
            raise stop

    reader = SimpleNamespace(capture_ownership=lambda **_kwargs: object())
    with pytest.raises(ValueError, match="path is duplicated"):
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={},
            label="duplicate streamed publication",
            streaming_json_paths=("documents.json", "documents.json"),
            check_cancelled=check_cancelled,
        )

    assert cancellation_calls == 1


def test_publishability_none_path_preserves_noninterruptible_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = SimpleNamespace(capture_ownership=lambda **_kwargs: object())
    monkeypatch.setattr(
        security_module,
        "directory_ownership_file_records",
        lambda _ownership: (SimpleNamespace(path="other.json", size=2),),
    )

    def forbidden_interitem(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("None publishability path entered cancellation helper")

    monkeypatch.setattr(
        security_module,
        "_interitem_cancellation",
        forbidden_interitem,
    )

    with pytest.raises(ValueError, match="path is absent: documents.json"):
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={},
            label="noninterruptible publication",
            streaming_json_paths=("documents.json",),
        )


def test_publishable_json_forbidden_paths_stop_before_poisoned_future() -> None:
    poisoned = False

    class PoisonedPaths(tuple[Path, ...]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal poisoned
            yield tuple.__getitem__(self, 0)
            poisoned = True
            raise AssertionError("JSON policy consumed its poisoned forbidden path")

    paths = PoisonedPaths((Path("safe"), Path("poison")))
    stop = BaseException("injected forbidden-path cancellation")
    cancellation_calls = 0

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        if cancellation_calls == 2:
            raise stop

    with pytest.raises(BaseException) as caught:
        assert_publishable_json_value(
            None,
            forbidden_paths=paths,
            environ={},
            label="poisoned JSON policy",
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


def test_publishable_json_final_pattern_match_precedes_armed_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = False
    cancellation_calls = 0
    stop = KeyboardInterrupt("must not precede the final forbidden match")
    monkeypatch.setattr(
        security_module,
        "_forbidden_path_strings",
        lambda _paths, **_kwargs: ("absent", "forbidden"),
    )

    def check_cancelled() -> None:
        nonlocal armed, cancellation_calls
        cancellation_calls += 1
        if armed:
            raise stop
        if cancellation_calls == 3:
            armed = True

    with pytest.raises(ValueError, match="absolute build-machine path"):
        assert_publishable_json_value(
            "contains forbidden",
            forbidden_paths=(),
            environ={},
            label="matched JSON policy",
            check_cancelled=check_cancelled,
        )

    assert armed
    assert cancellation_calls == 3


def test_publishable_json_environment_stops_before_poisoned_future() -> None:
    poisoned = False

    class PoisonedItems(tuple[tuple[str, str], ...]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal poisoned
            yield tuple.__getitem__(self, 0)
            poisoned = True
            raise AssertionError("JSON policy consumed its poisoned environment item")

    class PoisonedEnvironment(dict[str, str]):
        def items(self):  # type: ignore[override, no-untyped-def]
            return PoisonedItems(
                (
                    ("OPENAI_API_KEY", "safe-value"),
                    ("SECOND_API_KEY", "poison-value"),
                )
            )

    stop = ValueError("injected environment cancellation")
    cancellation_calls = 0

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        if cancellation_calls == 2:
            raise stop

    with pytest.raises(ValueError) as caught:
        assert_publishable_json_value(
            None,
            forbidden_paths=(),
            environ=PoisonedEnvironment(),
            label="poisoned JSON environment",
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


def test_matching_blocks_final_pattern_match_precedes_armed_stop() -> None:
    armed = False
    cancellation_calls = 0
    stop = BaseException("must not precede the final byte-pattern match")

    def check_cancelled() -> None:
        nonlocal armed, cancellation_calls
        cancellation_calls += 1
        if armed:
            raise stop
        if cancellation_calls == 2:
            armed = True

    assert (
        security_module._matching_kind_blocks(
            (b"contains-secret",),
            forbidden=(b"absent",),
            secrets=(b"secret",),
            check_cancelled=check_cancelled,
        )
        == "secret"
    )
    assert armed
    assert cancellation_calls == 2


@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, ValueError, KeyboardInterrupt, BaseException],
)
def test_publishable_json_complexity_stops_before_poisoned_future(
    error_type: type[BaseException],
) -> None:
    poisoned = False

    class PoisonedObject(dict[str, object]):
        def __len__(self) -> int:
            return 2

        def items(self):  # type: ignore[override, no-untyped-def]
            nonlocal poisoned
            yield "current", None
            poisoned = True
            raise AssertionError("JSON complexity consumed its poisoned future item")

    stop = error_type("injected JSON complexity cancellation")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(BaseException) as caught:
        security_module._validate_json_complexity_interruptibly(
            PoisonedObject(),
            label="poisoned JSON complexity",
            max_nodes=100_000,
            max_depth=100,
            max_key_bytes=1_024,
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


def test_publishable_json_complexity_current_error_precedes_armed_stop() -> None:
    stop = KeyboardInterrupt("must not precede the current malformed key")
    cancellation_calls = 0

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        raise stop

    with pytest.raises(ValueError, match="key exceeding 1 bytes"):
        security_module._validate_json_complexity_interruptibly(
            {"oversized": None},
            label="malformed JSON complexity",
            max_nodes=100_000,
            max_depth=100,
            max_key_bytes=1,
            check_cancelled=check_cancelled,
        )

    assert cancellation_calls == 0


@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, ValueError, KeyboardInterrupt, BaseException],
)
def test_payload_blocks_stop_before_poisoned_future(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    monkeypatch.setattr(security_module, "_SCAN_CHUNK_BYTES", 4)
    poisoned = False

    class PoisonedPayload(bytes):
        def __getitem__(self, item: object) -> object:
            nonlocal poisoned
            assert isinstance(item, slice)
            if item.start and item.start >= 4:
                poisoned = True
                raise AssertionError("payload scan consumed its poisoned future block")
            return bytes.__getitem__(self, item)

    stop = error_type("injected payload block cancellation")

    def check_cancelled() -> None:
        raise stop

    blocks = security_module._interruptible_payload_blocks(
        PoisonedPayload(b"safe-poison"),
        check_cancelled,
    )
    assert next(blocks) == b"safe"
    with pytest.raises(BaseException) as caught:
        next(blocks)

    assert caught.value is stop
    assert not poisoned


def test_payload_matching_attests_current_block_before_interblock_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_module, "_SCAN_CHUNK_BYTES", 8)
    cancellation_calls = 0
    stop = BaseException("must not precede the current block match")

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        raise stop

    blocks = security_module._interruptible_payload_blocks(
        b"secret!!future!!",
        check_cancelled,
    )
    assert (
        security_module._matching_kind_blocks(
            blocks,
            forbidden=(),
            secrets=(b"secret",),
            check_cancelled=check_cancelled,
        )
        == "secret"
    )
    assert cancellation_calls == 0


def test_payload_matching_preserves_cross_block_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_module, "_SCAN_CHUNK_BYTES", 8)
    cancellation_calls = 0

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1

    blocks = security_module._interruptible_payload_blocks(
        b"123456secre" b"t-future",
        check_cancelled,
    )
    assert (
        security_module._matching_kind_blocks(
            blocks,
            forbidden=(),
            secrets=(b"secret",),
            check_cancelled=check_cancelled,
        )
        == "secret"
    )
    assert cancellation_calls >= 1


def test_publishability_malformed_current_payload_precedes_armed_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_bytes(b"not-json")
    ownership = capture_directory_ownership(root)
    real_blocks = security_module._interruptible_payload_blocks
    armed = False
    stop = BaseException("must not precede malformed current JSON")

    def arm_after_current_payload(
        payload: bytes,
        check_cancelled: object,
    ):
        nonlocal armed
        assert callable(check_cancelled)
        for block in real_blocks(payload, check_cancelled):
            yield block
            armed = True

    def check_cancelled() -> None:
        if armed:
            raise stop

    monkeypatch.setattr(
        security_module,
        "validate_bounded_json_stream",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        security_module,
        "_interruptible_payload_blocks",
        arm_after_current_payload,
    )

    def validate(reader: PublicationDirectoryReader) -> None:
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={"MY_TOKEN": "absent-secret-value"},
            label="malformed current publication",
            check_cancelled=check_cancelled,
        )

    with pytest.raises(ValueError, match="contains invalid JSON") as caught:
        reopen_authenticated_directory(root, ownership, validate)

    assert caught.value is not stop
    assert armed


def test_publishability_none_path_preserves_complexity_and_payload_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    (root / "config.json").write_text('{"safe":true}', encoding="utf-8")
    ownership = capture_directory_ownership(root)
    real_complexity = security_module.validate_json_complexity
    complexity_calls: list[tuple[object, dict[str, object]]] = []

    def observe_complexity(value: object, **kwargs: object) -> None:
        complexity_calls.append((value, kwargs))
        real_complexity(value, **kwargs)

    def forbidden_interruptible_blocks(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("None payload scan used interruptible block derivation")

    monkeypatch.setattr(
        security_module,
        "validate_json_complexity",
        observe_complexity,
    )
    monkeypatch.setattr(
        security_module,
        "_interruptible_payload_blocks",
        forbidden_interruptible_blocks,
    )

    def validate(reader: PublicationDirectoryReader) -> None:
        assert_publishable_tree_reader(
            reader,
            forbidden_paths=(),
            environ={},
            label="None-shaped publication",
        )

    reopen_authenticated_directory(root, ownership, validate)

    assert len(complexity_calls) == 1
    assert complexity_calls[0][1] == {
        "label": "None-shaped publication JSON config.json",
        "max_nodes": security_module._MAX_PUBLISHABLE_JSON_NODES,
        "max_depth": security_module._MAX_PUBLISHABLE_JSON_DEPTH,
        "max_key_bytes": security_module._MAX_PUBLISHABLE_JSON_KEY_BYTES,
    }


@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, ValueError, KeyboardInterrupt, BaseException],
)
@pytest.mark.parametrize("mutate", [False, True])
def test_publishability_final_ownership_stop_reconciles_exactly(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    mutate: bool,
) -> None:
    expected_ownership = object()
    changed_ownership = object()
    stop = error_type("injected final ownership cancellation")

    class FinalCaptureReader:
        def __init__(self) -> None:
            self.capture_calls = 0

        def capture_ownership(self, **kwargs: object) -> object:
            self.capture_calls += 1
            check_cancelled = kwargs.get("check_cancelled")
            if self.capture_calls == 1:
                assert callable(check_cancelled)
                return expected_ownership
            if self.capture_calls == 2:
                assert callable(check_cancelled)
                check_cancelled()
                raise AssertionError("final ownership callback returned")
            assert self.capture_calls == 3
            assert check_cancelled is None
            return changed_ownership if mutate else expected_ownership

        def open_authenticated_file(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> object:
            raise AssertionError("empty ownership must not open a file")

    reader = FinalCaptureReader()
    monkeypatch.setattr(
        security_module,
        "directory_ownership_file_records",
        lambda ownership: () if ownership is expected_ownership else pytest.fail(),
    )

    def check_cancelled() -> None:
        raise stop

    expected_error = RuntimeError if mutate else error_type
    with pytest.raises(expected_error) as caught:
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={},
            label="postflight publication",
            check_cancelled=check_cancelled,
        )

    if mutate:
        assert "changed during cancellation reconciliation" in str(caught.value)
        assert caught.value.__cause__ is stop
    else:
        assert caught.value is stop
    assert reader.capture_calls == 3


@pytest.mark.parametrize("mutate", [False, True])
def test_publishability_final_policy_stop_uses_tracked_reconciliation(
    mutate: bool,
) -> None:
    expected_ownership = object()
    changed_ownership = object()
    stop = BaseException("injected final entry-policy cancellation")

    class PolicyCaptureReader:
        def __init__(self) -> None:
            self.capture_calls = 0

        def capture_ownership(self, **kwargs: object) -> object:
            self.capture_calls += 1
            policy = kwargs["entry_policy"]
            assert callable(policy)
            policy("candidate.bin", "file", 0o600, 1)
            if self.capture_calls == 1:
                raise AssertionError("tracked entry policy returned")
            assert self.capture_calls == 2
            assert kwargs.get("check_cancelled") is None
            return changed_ownership if mutate else expected_ownership

    def entry_policy(
        _path: str,
        _kind: str,
        _mode: int,
        _size: int,
    ) -> None:
        raise AssertionError("callback path reused the untracked entry policy")

    def entry_validator(
        _path: str,
        _kind: str,
        _mode: int,
        _size: int,
        cancellation_check: object,
    ) -> None:
        if cancellation_check is not None:
            assert callable(cancellation_check)
            cancellation_check()

    reader = PolicyCaptureReader()

    def check_cancelled() -> None:
        raise stop

    expected_error = RuntimeError if mutate else BaseException
    with pytest.raises(expected_error) as caught:
        security_module._capture_publishable_tree_postflight(
            reader,
            expected=expected_ownership,
            entry_policy=entry_policy,
            entry_validator=entry_validator,
            check_cancelled=check_cancelled,
        )

    if mutate:
        assert "changed during cancellation reconciliation" in str(caught.value)
        assert caught.value.__cause__ is stop
    else:
        assert caught.value is stop
    assert reader.capture_calls == 2


@pytest.mark.parametrize("mutate", [False, True])
def test_publishability_file_stop_finalizes_current_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: bool,
) -> None:
    root = tmp_path / "publication"
    root.mkdir()
    config = root / "config.json"
    config.write_bytes(b'{"x":1}\n')
    ownership = capture_directory_ownership(root)
    stop = StopIteration("injected authenticated-file stop")
    armed = False
    mutated = False

    def stop_inside_reader(source: object, **_kwargs: object) -> None:
        nonlocal armed
        armed = True
        source.read(1)  # type: ignore[attr-defined]
        raise AssertionError("authenticated reader callback returned")

    def check_cancelled() -> None:
        nonlocal mutated
        if not armed:
            return
        if mutate and not mutated:
            config.write_bytes(b'{"x":2}\n')
            mutated = True
        raise stop

    monkeypatch.setattr(
        security_module,
        "validate_bounded_json_stream",
        stop_inside_reader,
    )

    def validate(reader: PublicationDirectoryReader) -> None:
        security_module._assert_publishable_tree_reader_interruptibly(
            reader,
            forbidden_paths=(),
            environ={},
            label="authenticated publication",
            check_cancelled=check_cancelled,
        )

    if not mutate:
        with pytest.raises(StopIteration) as caught:
            reopen_authenticated_directory(root, ownership, validate)
        assert caught.value is stop
    else:
        with pytest.raises(BaseException) as caught:
            reopen_authenticated_directory(root, ownership, validate)
        assert caught.value is not stop
        assert isinstance(caught.value, (RuntimeError, ValueError))
        assert caught.value.__cause__ is not None
    assert armed
    assert mutated is mutate


def test_publishability_carrier_merges_source_cleanup_owners_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupOwner:
        def __init__(self) -> None:
            self.closed = False
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls > 1:
                raise AssertionError("cleanup owner closed twice")
            self.closed = True

    class HostileStop(StopIteration):
        def __getattribute__(self, name: str) -> object:
            if name == "source_cleanup_owner":
                raise AssertionError("callback descriptor was invoked")
            return super().__getattribute__(name)

        def __setattr__(self, name: str, value: object) -> None:
            if name == "source_cleanup_owner":
                raise AssertionError("callback descriptor was invoked")
            super().__setattr__(name, value)

    stop = HostileStop("injected security cleanup-owner stop")
    existing_owner = CleanupOwner()
    new_owner = CleanupOwner()
    BaseException.__setattr__(stop, "source_cleanup_owner", existing_owner)

    def settled_impl(*_args: object, **kwargs: object) -> None:
        callback = kwargs["check_cancelled"]
        assert callable(callback)
        try:
            callback()
        except BaseException as carrier:
            BaseException.__setattr__(
                carrier,
                "source_cleanup_owner",
                new_owner,
            )
            raise

    monkeypatch.setattr(
        security_module,
        "_assert_publishable_tree_reader_interruptibly_impl",
        settled_impl,
    )

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(HostileStop) as caught:
        security_module._assert_publishable_tree_reader_interruptibly(
            object(),  # type: ignore[arg-type]
            forbidden_paths=(),
            environ={},
            label="settled publication",
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    merged = BaseException.__getattribute__(stop, "source_cleanup_owner")
    assert merged.pending_sources == (existing_owner, new_owner)
    merged.close()
    assert existing_owner.closed
    assert new_owner.closed

    overlapping_owner = CleanupOwner()
    overlapping_stop = StopIteration("injected overlapping cleanup-owner stop")
    overlapping_carrier = security_module._CallbackIterationStop(overlapping_stop)
    BaseException.__setattr__(
        overlapping_stop,
        "source_cleanup_owner",
        security_module._SourceCleanupGroup(overlapping_owner),
    )
    BaseException.__setattr__(
        overlapping_carrier,
        "source_cleanup_owner",
        overlapping_owner,
    )
    security_module._transfer_callback_exception_settlement(
        overlapping_carrier,
        overlapping_stop,
    )
    overlapping_group = BaseException.__getattribute__(
        overlapping_stop,
        "source_cleanup_owner",
    )
    overlapping_group.close()
    assert overlapping_owner.close_calls == 1


def test_publishable_json_arbitrary_mapping_polls_before_poisoned_tail() -> None:
    stop = StopIteration("injected publishable mapping future stop")
    armed = False
    poison_touched = False
    iterations = 0

    class StatefulMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal poison_touched
            if iterations == 1:
                assert key == "safe"
                return "value"
            poison_touched = True
            raise AssertionError("publishable mapping touched a poisoned value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal iterations
            iterations += 1
            if iterations == 1:
                return iter(("safe",))

            def poisoned():  # type: ignore[no-untyped-def]
                nonlocal armed, poison_touched
                armed = True
                yield "safe"
                poison_touched = True
                raise AssertionError("publishable mapping consumed a poisoned tail")

            return poisoned()

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        assert_publishable_json_value(
            StatefulMapping(),
            forbidden_paths=(),
            environ={},
            label="stateful publication JSON",
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert iterations == 2
    assert not poison_touched


@pytest.mark.parametrize(
    ("key", "child", "expected_error"),
    [
        ("safe", "safe", StopIteration),
        ("api_key", "unused", secret_fields_module.SecretFieldError),
        ("description", "Bearer current-secret", secret_fields_module.SecretFieldError),
    ],
)
def test_secret_walk_separates_lazy_mapping_key_and_value_boundaries(
    key: str,
    child: str,
    expected_error: type[BaseException],
) -> None:
    stop = StopIteration("injected lazy secret-mapping stop")
    armed = False
    value_touched = False

    class LazyMapping(Mapping[str, object]):
        def __getitem__(self, current_key: str) -> object:
            nonlocal armed, value_touched
            value_touched = True
            if key == "safe":
                raise AssertionError("secret mapping touched a poisoned future value")
            armed = True
            assert current_key == key
            return child

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            if key != "description":
                armed = True
            yield key

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(expected_error) as caught:
        secret_fields_module.assert_no_secret_fields(
            LazyMapping(),
            source="lazy secret mapping",
            check_cancelled=check_cancelled,
        )
    if expected_error is StopIteration:
        assert caught.value is stop
        assert not value_touched
    else:
        assert caught.value is not stop
        assert key == "description" or not value_touched


def test_publishable_environment_polls_between_lazy_key_and_value() -> None:
    stop = StopIteration("injected lazy environment stop")
    armed = False
    value_touched = False

    class LazyEnvironment(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            nonlocal value_touched
            value_touched = True
            raise AssertionError("environment touched a poisoned future value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            armed = True
            yield "SAFE_VAR"

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        assert_publishable_json_value(
            "safe",
            forbidden_paths=(),
            environ=LazyEnvironment(),
            label="lazy environment publication",
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not value_touched


def test_secret_values_poll_between_lazy_environment_key_and_value() -> None:
    stop = SystemExit("injected secret-value environment stop")
    armed = False
    value_touched = False

    class LazyEnvironment(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            nonlocal value_touched
            value_touched = True
            raise AssertionError("secret values touched a poisoned future value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            armed = True
            yield "SAFE_VAR"

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(SystemExit) as caught:
        security_module._secret_values(
            LazyEnvironment(),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not value_touched


@pytest.mark.parametrize("container_kind", ["dict", "list", "tuple"])
def test_secret_walk_validates_current_exact_child_before_future_stop(
    container_kind: str,
) -> None:
    stop = StopIteration("injected current secret stop")
    polls = 0
    if container_kind == "dict":
        value: object = {
            "description": "Bearer current-secret",
            "future": "safe",
        }
    elif container_kind == "list":
        value = ["Bearer current-secret", "safe"]
    else:
        value = ("Bearer current-secret", "safe")

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls == 2:
            raise stop

    with pytest.raises(secret_fields_module.SecretFieldError) as caught:
        secret_fields_module.assert_no_secret_fields(
            value,
            source="current secret value",
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop
    assert polls == 1


@pytest.mark.parametrize("base", [list, tuple])
@pytest.mark.parametrize("current", ["safe", "Bearer current-secret"])
def test_secret_walk_ignores_subclass_length_before_poisoned_tail(
    base: type[list[object]] | type[tuple[object, ...]],
    current: object,
) -> None:
    stop = StopIteration("injected secret sequence future stop")
    state = SimpleNamespace(armed=False, poison_touched=False)

    class LyingSequence(base):  # type: ignore[misc, valid-type]
        def __len__(self) -> int:
            return 1

        def __iter__(self):  # type: ignore[no-untyped-def]
            def poisoned():  # type: ignore[no-untyped-def]
                state.armed = True
                yield current
                state.poison_touched = True
                raise AssertionError("secret walk consumed a poisoned tail")

            return poisoned()

    def check_cancelled() -> None:
        if state.armed:
            raise stop

    if current == "safe":
        with pytest.raises(StopIteration) as caught:
            secret_fields_module.assert_no_secret_fields(
                LyingSequence(("unused",)),
                source="lying secret sequence",
                check_cancelled=check_cancelled,
            )
        assert caught.value is stop
    else:
        with pytest.raises(secret_fields_module.SecretFieldError) as caught:
            secret_fields_module.assert_no_secret_fields(
                LyingSequence(("unused",)),
                source="lying current secret",
                check_cancelled=check_cancelled,
            )
        assert caught.value is not stop
    assert not state.poison_touched


@pytest.mark.parametrize("base", [list, tuple])
def test_secret_walk_none_preserves_sequence_tail_protocol(
    base: type[list[object]] | type[tuple[object, ...]],
) -> None:
    class PoisonedSequence(base):  # type: ignore[misc, valid-type]
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield "Bearer current-secret"
            raise AssertionError("legacy secret walk consumed the sequence tail")

    with pytest.raises(AssertionError, match="legacy secret walk"):
        secret_fields_module.assert_no_secret_fields(
            PoisonedSequence(("unused",)),
            source="legacy secret sequence",
        )


@pytest.mark.parametrize("base", [list, tuple])
@pytest.mark.parametrize("current", ["safe", object()])
def test_publishable_second_walk_polls_lying_sequence_before_poisoned_tail(
    base: type[list[object]] | type[tuple[object, ...]],
    current: object,
) -> None:
    stop = StopIteration("injected publishable sequence future stop")
    state = SimpleNamespace(iterations=0, armed=False, poison_touched=False)

    class StatefulSequence(base):  # type: ignore[misc, valid-type]
        def __len__(self) -> int:
            return 1

        def __iter__(self):  # type: ignore[no-untyped-def]
            state.iterations += 1
            if state.iterations == 1:
                return base.__iter__(self)

            def poisoned():  # type: ignore[no-untyped-def]
                state.armed = True
                yield current
                state.poison_touched = True
                raise AssertionError("publishable walk consumed a poisoned tail")

            return poisoned()

    def check_cancelled() -> None:
        if state.armed:
            raise stop

    value = StatefulSequence(("safe",))
    if current == "safe":
        with pytest.raises(StopIteration) as caught:
            assert_publishable_json_value(
                value,
                forbidden_paths=(),
                environ={},
                label="stateful publication sequence",
                check_cancelled=check_cancelled,
            )
        assert caught.value is stop
    else:
        with pytest.raises(ValueError, match="unsupported JSON value") as caught:
            assert_publishable_json_value(
                value,
                forbidden_paths=(),
                environ={},
                label="stateful publication sequence",
                check_cancelled=check_cancelled,
            )
        assert caught.value is not stop
    assert state.iterations == 2
    assert not state.poison_touched


@pytest.mark.parametrize("base", [list, tuple])
def test_publishable_second_walk_none_preserves_sequence_tail_protocol(
    monkeypatch: pytest.MonkeyPatch,
    base: type[list[object]] | type[tuple[object, ...]],
) -> None:
    class PoisonedSequence(base):  # type: ignore[misc, valid-type]
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield object()
            raise AssertionError("legacy publishable walk consumed the sequence tail")

    monkeypatch.setattr(
        security_module,
        "assert_no_credential_fields",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(AssertionError, match="legacy publishable walk"):
        assert_publishable_json_value(
            PoisonedSequence(("unused",)),
            forbidden_paths=(),
            environ={},
            label="legacy publication sequence",
        )


@pytest.mark.parametrize("base", [list, tuple])
def test_forbidden_paths_ignore_sequence_subclass_length_before_future(
    base: type[list[Path]] | type[tuple[Path, ...]],
) -> None:
    stop = StopIteration("injected forbidden-path future stop")
    state = SimpleNamespace(armed=False, poison_touched=False)

    class LyingPaths(base):  # type: ignore[misc, valid-type]
        def __len__(self) -> int:
            return 1

        def __iter__(self):  # type: ignore[no-untyped-def]
            def poisoned():  # type: ignore[no-untyped-def]
                state.armed = True
                yield Path("relative-safe-path")
                state.poison_touched = True
                raise AssertionError("forbidden paths consumed a poisoned tail")

            return poisoned()

    def check_cancelled() -> None:
        if state.armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        assert_publishable_json_value(
            "safe",
            forbidden_paths=LyingPaths((Path("unused"),)),
            environ={},
            label="lying forbidden paths",
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not state.poison_touched
