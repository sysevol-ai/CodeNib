# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import codenib.storage as storage_module
from codenib import LocalWorkspaceProvider
from codenib import cli as cli_module
from codenib._captured_directory import (
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
)
from codenib.artifacts import query_context_artifact
from codenib.compiler.manifest import MANIFEST_FILENAME
from codenib.mcp.context import ServerContext
from codenib.paths import repo_index_dir
from codenib.storage import LocalCAS, SQLiteCatalog, StorageIntegrityError

_REPOSITORY_KEY = "owner/retained-index-route"
_MARKER = "PRODUCTION_RETAINED_INDEX_ROUTE"


def _git_commit(repository: Path) -> str:
    subprocess.run(["git", "init", "-q", os.fspath(repository)], check=True)
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


def test_index_publish_retained_materialize_snapshot_and_query_real_bm25(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / "sample.py").write_text(
        f"def retained_index_marker():\n    return {_MARKER!r}\n",
        encoding="utf-8",
    )
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

    nonce = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(cli_module, "_new_compiler_cache_import_nonce", lambda: nonce)

    owners: list[PublishedWorkspaceReceiptOwner] = []
    stores: list[LocalCAS] = []
    catalogs: list[SQLiteCatalog] = []
    real_owner = PublishedWorkspaceReceiptOwner
    real_store = LocalCAS
    real_catalog = SQLiteCatalog

    def owner_factory() -> PublishedWorkspaceReceiptOwner:
        owner = real_owner()
        owners.append(owner)
        return owner

    def store_factory(
        root: str | Path,
        *,
        require_preprovisioned: bool = False,
    ) -> LocalCAS:
        assert require_preprovisioned is True
        store = real_store(root, require_preprovisioned=True)
        stores.append(store)
        return store

    def catalog_factory(
        path: str | Path,
        *,
        create: bool = True,
        expected_file_identity: tuple[int, int, int] | None = None,
    ) -> SQLiteCatalog:
        assert create is False
        assert type(expected_file_identity) is tuple
        opened = real_catalog(
            path,
            create=False,
            expected_file_identity=expected_file_identity,
        )
        catalogs.append(opened)
        return opened

    monkeypatch.setattr(
        cli_module,
        "_new_retained_output_receipt_owner",
        owner_factory,
    )
    monkeypatch.setattr(storage_module, "LocalCAS", store_factory)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", catalog_factory)

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
    snapshot_id = next(
        line.partition(":")[2].strip()
        for line in index_output.splitlines()
        if line.startswith("Retained Snapshot:")
    )
    assert snapshot_id
    assert "Retained Generation: 1" in index_output
    assert "Retained Views:      bm25" in index_output

    prefix = f".codenib-cache-import-{nonce}"
    bm25_evidence = workspace / f"{prefix}-bm25"
    context_evidence = workspace / f"{prefix}-context"
    suggested_output = workspace / f"{prefix}-materialized"
    materialized_output = workspace / "materialized"
    cache = repo_index_dir(repository)
    assert (cache / MANIFEST_FILENAME).is_file()
    assert (cache / "bm25").is_dir()
    assert bm25_evidence.is_dir()
    assert context_evidence.is_dir()
    assert not suggested_output.exists()
    assert not materialized_output.exists()

    assert len(owners) == 2
    assert all(owner.closed for owner in owners)
    assert len(stores) == 1
    with pytest.raises(StorageIntegrityError, match="closed"):
        stores[0].has("0" * 64)
    assert len(catalogs) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        catalogs[0]._connection.execute("SELECT 1")

    materialize_args = parser.parse_args(
        [
            "artifact",
            "materialize",
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(workspace),
            "--repository",
            _REPOSITORY_KEY,
            "--snapshot",
            snapshot_id,
            "--output",
            os.fspath(materialized_output),
        ]
    )
    assert materialize_args.handler is cli_module._run_artifact_materialize
    assert materialize_args.handler(materialize_args) == 0

    materialize_output = capsys.readouterr().out
    assert f"Snapshot:         {snapshot_id}" in materialize_output
    assert "Views:            bm25" in materialize_output
    assert materialized_output.is_dir()
    assert (materialized_output / "views/bm25").is_dir()
    assert bm25_evidence.is_dir()
    assert context_evidence.is_dir()

    assert len(owners) == 3
    assert all(owner.closed for owner in owners)
    assert len(stores) == 2
    for store in stores:
        with pytest.raises(StorageIntegrityError, match="closed"):
            store.has("0" * 64)
    assert len(catalogs) == 2
    for catalog in catalogs:
        with pytest.raises(sqlite3.ProgrammingError):
            catalog._connection.execute("SELECT 1")

    binding = query_context_artifact(
        materialized_output,
        expected_repository=_REPOSITORY_KEY,
        expected_commit=commit,
    )
    context: ServerContext | None = None
    try:
        assert tuple(binding.manifest.indexes) == ("bm25",)
        context = ServerContext.load(
            binding.manifest,
            views=("bm25",),
            artifact_binding=binding,
        )
        assert context.errors == {}
        assert context.loaded_views == frozenset({"bm25"})
        assert context.bm25 is not None
        hits = context.bm25.search(
            _MARKER,
            top_k=5,
            return_code_content=True,
        )
        assert hits
        assert hits[0].file == "sample.py"
        normalized_marker = _MARKER.lower().replace("_", " ")
        assert any(
            normalized_marker in document.page_content
            for document in context.bm25.documents
        )
    finally:
        if context is not None:
            context.close()
        binding.close()

    assert materialized_output.is_dir()
    assert bm25_evidence.is_dir()
    assert context_evidence.is_dir()
