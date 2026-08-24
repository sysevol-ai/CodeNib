# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from codenib import LocalWorkspaceProvider
from codenib import cli as cli_module
from codenib._captured_directory import UnsupportedWorkspaceCreation
from codenib.artifacts import query_context_artifact
from codenib.compiler.manifest import MANIFEST_FILENAME
from codenib.mcp import server as server_module
from codenib.mcp.context import ServerContext
from codenib.mcp.retained_context import RetainedServerContextOwner
from codenib.mcp.tools.search import search_bm25_impl
from codenib.mcp.tools.source import read_source_impl
from codenib.paths import repo_index_dir
from codenib.storage import LocalCAS, SQLiteCatalog, StorageIntegrityError

_REPOSITORY_KEY = "owner/retained-mcp-route"
_MARKER = "PRODUCTION_RETAINED_MCP_ROUTE"
_SOURCE_TEXT = f"def retained_mcp_marker():\n    return {_MARKER!r}\n"


def _git_commit(repository: Path) -> str:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", os.fspath(repository)],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            os.fspath(repository),
            "config",
            "user.email",
            "test@example.test",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            os.fspath(repository),
            "config",
            "user.name",
            "CodeNib Test",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repository), "add", "sample.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repository), "commit", "-qm", "fixture"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", os.fspath(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _capture_retained_mcp_cli_state() -> dict[str, object]:
    """Retain CLI-owned resources while the synchronous transport is active."""

    required = (
        "runtime_owner",
        "repository_authority_owner",
        "topology_owner",
        "object_store_owner",
        "catalog_owner",
        "topology",
        "object_store",
        "catalog",
    )
    frame = inspect.currentframe()
    captured: dict[str, object] | None = None
    try:
        while frame is not None:
            if (
                frame.f_code.co_name == "_run_mcp_retained"
                and frame.f_globals.get("__name__") == cli_module.__name__
            ):
                assert all(name in frame.f_locals for name in required)
                captured = {name: frame.f_locals[name] for name in required}
                break
            frame = frame.f_back
    finally:
        del frame
    assert captured is not None, "retained MCP CLI frame was not active"
    return captured


@pytest.mark.parametrize(
    "source_bound",
    (False, True),
    ids=("query-only", "source-bound"),
)
def test_index_publish_retained_then_mcp_cold_starts_real_bm25(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_bound: bool,
) -> None:
    # Other MCP tests exercise the legacy module-global startup path.  Keep
    # this cold-start lifecycle isolated while still requiring the retained
    # route to detach its own exact context before returning.
    monkeypatch.setattr(server_module, "_ctx", None)
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / "sample.py").write_text(_SOURCE_TEXT, encoding="utf-8")
    commit = _git_commit(repository)

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    provider = LocalWorkspaceProvider(workspace)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")

    catalog_path = tmp_path / "catalog.sqlite"
    with SQLiteCatalog(catalog_path):
        pass
    cas_root = tmp_path / "cas"
    with LocalCAS.provision(cas_root):
        pass
    codenib_home = tmp_path / "codenib-home"
    codenib_home.mkdir(mode=0o700)
    monkeypatch.setenv("CODENIB_HOME", os.fspath(codenib_home))

    parser = cli_module.build_parser()
    index_args = parser.parse_args(
        [
            "index",
            os.fspath(repository),
            "--preset",
            "fast",
            "--publish-retained",
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(workspace),
            "--repository",
            _REPOSITORY_KEY,
        ]
    )
    assert index_args.handler is cli_module._run_index
    assert index_args.handler(index_args) == 0

    index_output = capsys.readouterr().out
    assert "Retained Generation: 1" in index_output
    assert "Retained Ref:        main" in index_output
    assert "Retained Views:      bm25" in index_output
    cache = repo_index_dir(repository)
    assert (cache / MANIFEST_FILENAME).is_file()
    assert (cache / "bm25").is_dir()

    materialized_output = workspace / "retained-mcp-context"
    captured: dict[str, object] = {}
    installed_contexts: list[ServerContext] = []
    installed_sources: list[object] = []

    def run_stdio(*, transport: str) -> None:
        assert transport == "stdio"
        context = server_module.get_context()
        assert type(context) is ServerContext
        assert server_module._ctx is context
        assert context.manifest.commit == commit
        assert context.loaded_views == frozenset({"bm25"})
        assert context.errors == {}
        assert context.artifact is not None
        assert context.artifact["verified"] is True
        assert context.source_verified is source_bound
        assert context.source_verification_scope == (
            "content-bytes" if source_bound else None
        )
        assert context.commit_verified is False

        run_state = _capture_retained_mcp_cli_state()
        captured.update(run_state)
        for name in ("topology_owner", "object_store_owner", "catalog_owner"):
            assert run_state[name].closed  # type: ignore[attr-defined]
        assert run_state["repository_authority_owner"].closed  # type: ignore[attr-defined]
        assert run_state["topology"].closed  # type: ignore[attr-defined]
        repository_authority = run_state["topology"].repository_authority  # type: ignore[attr-defined]
        if source_bound:
            assert repository_authority is not None
            assert repository_authority.closed
        else:
            assert repository_authority is None
        with pytest.raises(StorageIntegrityError, match="closed"):
            run_state["object_store"].has("0" * 64)  # type: ignore[attr-defined]
        with pytest.raises(sqlite3.ProgrammingError):
            run_state["catalog"]._connection.execute(  # type: ignore[attr-defined]
                "SELECT 1"
            )

        runtime_owner = run_state["runtime_owner"]
        assert type(runtime_owner) is RetainedServerContextOwner
        assert runtime_owner.state == "active"
        assert runtime_owner.context is context
        assert runtime_owner._source_owner.closed is (not source_bound)  # noqa: SLF001
        if source_bound:
            assert context._source_binding is not None  # noqa: SLF001
            assert not context._source_binding.closed  # noqa: SLF001
            installed_sources.append(context._source_binding)  # noqa: SLF001
        else:
            assert context._source_binding is None  # noqa: SLF001
        results = search_bm25_impl(context, _MARKER, top_k=5)
        assert results
        assert results[0]["file"] == "sample.py"
        if source_bound:
            assert results[0]["content"] == _SOURCE_TEXT
        else:
            assert results[0].get("content") is None
        assert context.bm25 is not None
        normalized_marker = _MARKER.lower().replace("_", " ")
        assert any(
            normalized_marker in document.page_content
            for document in context.bm25.documents
        )
        assert search_bm25_impl(context, "retained_mcp_marker", top_k=1)
        if source_bound:
            source = read_source_impl(context, "sample.py", 1, 2)
            assert source["file"] == "sample.py"
            assert source["content"] == _SOURCE_TEXT
            assert source["source"] == {
                "repository": _REPOSITORY_KEY,
                "commit": commit,
                "source_fingerprint": context.manifest.source_fingerprint,
                "verified": True,
                "verification_scope": "content-bytes",
                "commit_verified": False,
                "checkout_state": "not-attested",
            }
        else:
            with pytest.raises(RuntimeError, match="source reads are unavailable"):
                read_source_impl(context, "sample.py", 1, 2)
        assert server_module.get_context() is context
        assert runtime_owner.state == "active"
        assert runtime_owner.context is context

        installed_contexts.append(context)

    monkeypatch.setattr(server_module.mcp, "run", run_stdio)
    assert server_module._ctx is None
    mcp_command = [
        "mcp",
        "--catalog",
        os.fspath(catalog_path),
        "--cas-root",
        os.fspath(cas_root),
        "--workspace-root",
        os.fspath(workspace),
        "--repository",
        _REPOSITORY_KEY,
        "--ref",
        "main",
        "--expected-generation",
        "1",
        "--output",
        os.fspath(materialized_output),
    ]
    if source_bound:
        mcp_command.extend(("--repo", os.fspath(repository)))
    mcp_args = parser.parse_args(mcp_command)
    assert mcp_args.handler is cli_module._run_mcp
    assert mcp_args.handler(mcp_args) == 0

    assert len(installed_contexts) == 1
    assert server_module._ctx is None
    with pytest.raises(RuntimeError, match="not initialized"):
        server_module.get_context()

    runtime_owner = captured["runtime_owner"]
    assert type(runtime_owner) is RetainedServerContextOwner
    assert runtime_owner.closed
    assert runtime_owner._context_close_complete  # noqa: SLF001
    assert runtime_owner._source_owner.closed  # noqa: SLF001
    assert runtime_owner._receipt_owner.closed  # noqa: SLF001
    assert len(installed_sources) == int(source_bound)
    assert all(source.closed for source in installed_sources)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        runtime_owner.context

    for name in ("topology_owner", "object_store_owner", "catalog_owner"):
        owner = captured[name]
        assert type(owner) is cli_module._RetainedMaterializationResourceOwner
        assert owner.closed
    assert captured["repository_authority_owner"].closed  # type: ignore[attr-defined]
    assert captured["topology"].closed  # type: ignore[attr-defined]

    object_store = captured["object_store"]
    assert type(object_store) is LocalCAS
    with pytest.raises(StorageIntegrityError, match="closed"):
        object_store.has("0" * 64)
    catalog = captured["catalog"]
    assert type(catalog) is SQLiteCatalog
    with pytest.raises(sqlite3.ProgrammingError):
        catalog._connection.execute("SELECT 1")

    assert materialized_output.is_dir()
    binding = query_context_artifact(
        materialized_output,
        expected_repository=_REPOSITORY_KEY,
        expected_commit=commit,
    )
    try:
        assert binding.artifact.root == materialized_output.resolve()
        assert binding.artifact.views == ("bm25",)
        assert tuple(binding.manifest.indexes) == ("bm25",)
    finally:
        binding.close()
    assert materialized_output.is_dir()
