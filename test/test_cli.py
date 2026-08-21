# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import errno
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
import yaml

import codenib
import codenib.compiler.cache_import as cache_import_module
import codenib.compiler.manifest_materialization as materialization_module
import codenib.source_fingerprint as source_fingerprint_module
import codenib.storage as storage_module
import codenib.web.local as local_module
from codenib import cli
from codenib.source_fingerprint import RepositorySourceBinding
from codenib.web import launcher
from codenib.web.local import prepare_local_wiki


def _cleanup_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *tuple(getattr(error, "__notes__", ())),
        *tuple(getattr(error, "_codenib_cleanup_notes", ())),
    )


def test_parser_exposes_release_commands() -> None:
    parser = cli.build_parser()

    for command in ("index", "wiki", "export", "publish", "mcp", "doctor"):
        args = parser.parse_args([command])
        assert args.command == command

    toolchain = parser.parse_args(
        ["toolchain", "install", ".", "--scope", "all", "--dry-run"]
    )
    assert toolchain.command == "toolchain"
    assert toolchain.toolchain_command == "install"
    assert toolchain.scope == "all"
    assert toolchain.dry_run is True

    publish = parser.parse_args(["publish", "."])
    assert publish.preset == "auto"


def test_mcp_artifact_repo_is_explicit() -> None:
    parser = cli.build_parser()

    query_only = parser.parse_args(["mcp", "--artifact", "/tmp/context"])
    bound = parser.parse_args(
        ["mcp", "--artifact", "/tmp/context", "--repo", "/tmp/repo"]
    )

    assert query_only.repo is None
    assert bound.repo == "/tmp/repo"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--port", "0"),
        ("--port", "-1"),
        ("--port", "65536"),
        ("--api-port", "0"),
        ("--api-port", "70000"),
    ],
)
def test_wiki_parser_rejects_invalid_tcp_ports(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["wiki", ".", flag, value])

    assert exc_info.value.code == 2


def test_wiki_rejects_colliding_ports_before_repository_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(
        ["wiki", ".", "--port", "8123", "--api-port", "8123"]
    )
    monkeypatch.setattr(
        cli,
        "resolve_repo_path",
        lambda _value: pytest.fail("port preflight must run before repository work"),
    )

    with pytest.raises(cli.CLIError, match="must be different"):
        cli._run_wiki(args)


def test_export_parser_accepts_pages_mount_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "export",
            ".",
            "--output",
            "/tmp/wiki",
            "--base-path",
            "/project",
            "--frontend-dir",
            "/tmp/frontend",
        ]
    )

    assert args.output == "/tmp/wiki"
    assert args.base_path == "/project"
    assert args.frontend_dir == "/tmp/frontend"


def test_publish_and_artifact_parsers_expose_distribution_options() -> None:
    publish = cli.build_parser().parse_args(
        [
            "publish",
            ".",
            "--preset",
            "semantic",
            "--site-output",
            "/tmp/site",
            "--context-output",
            "/tmp/context",
            "--repository",
            "example/project",
            "--base-path",
            "/project",
            "--embedding-provider",
            "openai",
        ]
    )
    artifact = cli.build_parser().parse_args(
        [
            "artifact",
            "pack",
            ".",
            "--output",
            "/tmp/context",
            "--view",
            "bm25,vector",
        ]
    )

    assert publish.preset == "semantic"
    assert publish.site_output == "/tmp/site"
    assert publish.context_output == "/tmp/context"
    assert publish.repository == "example/project"
    assert publish.embedding_provider == "openai"
    assert artifact.artifact_command == "pack"
    assert artifact.view == ["bm25,vector"]


def test_artifact_import_cache_parser_exposes_existing_storage_inputs() -> None:
    base = [
        "artifact",
        "import-cache",
        "/src/repo",
        "--cache-dir",
        "/src/repo/.codenib_index",
        "--catalog",
        "/state/catalog.sqlite3",
        "--cas-root",
        "/state/cas",
        "--workspace-root",
        "/state/workspaces",
        "--repository",
        "owner/repo",
    ]
    args = cli.build_parser().parse_args(
        [
            *base,
            "--namespace",
            "production",
            "--ref",
            "release",
            "--expected-generation",
            "0",
        ]
    )
    defaults = cli.build_parser().parse_args(base)

    assert args.handler is cli._run_artifact_import_cache
    assert args.artifact_command == "import-cache"
    assert args.repo == "/src/repo"
    assert args.cache_dir == "/src/repo/.codenib_index"
    assert args.namespace == "production"
    assert args.ref == "release"
    assert args.expected_generation == 0
    assert defaults.namespace == "default"
    assert defaults.ref == "main"
    assert defaults.expected_generation == 0
    assert cli._compiler_cache_import_selection(
        ref_name=None,
        expected_generation=None,
    ) == ("main", 0)

    with pytest.raises(SystemExit) as negative:
        cli.build_parser().parse_args([*base, "--expected-generation", "-1"])
    assert negative.value.code == 2
    with pytest.raises(SystemExit) as overflow:
        cli.build_parser().parse_args([*base, "--expected-generation", str(2**63)])
    assert overflow.value.code == 2


def test_artifact_import_cache_freezes_missing_disjoint_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = repo / ".codenib_index"
    cache.mkdir()
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: "a" * 32)
    args = SimpleNamespace(
        repo=str(repo),
        cache_dir=str(cache),
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
    )

    paths = cli._compiler_cache_import_paths(args)
    topology = cli._require_compiler_cache_import_topology(paths)

    prefix = ".codenib-cache-import-" + "a" * 32
    assert topology.cache_dir == cache.resolve(strict=True)
    assert topology.catalog_path == catalog.resolve(strict=True)
    assert topology.cas_root == cas_root.resolve(strict=True)
    assert paths.bm25_destination == workspace_root / f"{prefix}-bm25"
    assert paths.context_destination == workspace_root / f"{prefix}-context"
    assert paths.suggested_materialization == (
        workspace_root / f"{prefix}-materialized"
    )

    paths.context_destination.mkdir()
    with pytest.raises(cli.CLIError, match="output must be missing"):
        cli._require_compiler_cache_import_topology(paths)
    paths.context_destination.rmdir()

    cache_alias = tmp_path / "cache-alias"
    cache_alias.symlink_to(workspace_root, target_is_directory=True)
    alias_paths = cli._compiler_cache_import_paths(
        SimpleNamespace(
            repo=str(repo),
            cache_dir=str(cache_alias),
            catalog=str(catalog),
            cas_root=str(cas_root),
            workspace_root=str(workspace_root),
        )
    )
    with pytest.raises(cli.CLIError, match="physically overlap the workspace"):
        cli._require_compiler_cache_import_topology(alias_paths)


def test_artifact_import_cache_rejects_directory_identity_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _cache_import_cli_test_args(tmp_path)
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: "e" * 32)
    paths = cli._compiler_cache_import_paths(args)
    workspace = Path(args.workspace_root).resolve(strict=True)
    workspace_identity = cli._retained_real_directory_identity(
        workspace,
        label="workspace root",
    )
    real_ancestry = cli._retained_directory_ancestry

    def aliased_ancestry(
        path: Path,
        *,
        label: str,
    ) -> tuple[tuple[Path, tuple[int, int]], ...]:
        ancestry = real_ancestry(path, label=label)
        if label == "cache directory":
            return ((ancestry[0][0], workspace_identity), *ancestry[1:])
        return ancestry

    monkeypatch.setattr(cli, "_retained_directory_ancestry", aliased_ancestry)

    with pytest.raises(
        cli.CLIError,
        match="cache directory must not physically alias the workspace root",
    ):
        cli._require_compiler_cache_import_topology(paths)


def _cache_import_cli_test_args(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = repo / ".codenib_index"
    cache.mkdir()
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    return SimpleNamespace(
        repo=str(repo),
        cache_dir=str(cache),
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        repository="owner/repo",
        namespace="default",
        ref="main",
        expected_generation=0,
    )


def test_artifact_import_cache_uses_frozen_authorities_and_closes_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _cache_import_cli_test_args(tmp_path)
    nonce = "b" * 32
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: nonce)
    workspace_root = Path(args.workspace_root)
    cache = Path(args.cache_dir)
    catalog_path = Path(args.catalog)
    cas_root = Path(args.cas_root)
    bm25_destination = workspace_root / f".codenib-cache-import-{nonce}-bm25"
    context_destination = workspace_root / f".codenib-cache-import-{nonce}-context"
    suggested = workspace_root / f".codenib-cache-import-{nonce}-materialized"
    events: list[str] = []
    bridge_calls: list[tuple[Path, dict[str, object]]] = []
    capture_calls: list[tuple[Path, tuple[Path, ...]]] = []

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root
            events.append("provider")

        def require_support(self) -> None:
            events.append("provider-support")

    class Closeable:
        def __init__(self, label: str) -> None:
            self.label = label
            self.closed = False

        def close(self) -> None:
            events.append(f"{self.label}-close")
            self.closed = True

    bm25_owner = Closeable("bm25")
    context_owner = Closeable("context")
    source = Closeable("source")
    store = Closeable("cas")
    catalog = Closeable("catalog")
    receipt_owners = iter((bm25_owner, context_owner))
    catalog_identity = cli._retained_catalog_file_identity(catalog_path)

    def capture(
        root: Path,
        *,
        exclude_roots: tuple[Path, ...],
        _source_owner,
    ) -> Closeable:
        events.append("capture")
        capture_calls.append((root, exclude_roots))
        _source_owner(source)
        return source

    def open_store(root: Path, **kwargs: object) -> Closeable:
        events.append("cas-open")
        assert root == cas_root.resolve(strict=True)
        assert kwargs == {"require_preprovisioned": True}
        return store

    def open_catalog(path: Path, **kwargs: object) -> Closeable:
        events.append("catalog-open")
        assert path == catalog_path.resolve(strict=True)
        assert kwargs == {
            "create": False,
            "expected_file_identity": catalog_identity,
        }
        return catalog

    def import_cache(cache_dir: Path, **kwargs: object) -> SimpleNamespace:
        events.append("bridge")
        bridge_calls.append((cache_dir, kwargs))
        bm25_destination.mkdir()
        context_destination.mkdir()
        return SimpleNamespace(
            import_result=SimpleNamespace(
                snapshot_id="snapshot-id",
                ref_name="main",
                generation=1,
            )
        )

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_new_retained_output_receipt_owner",
        lambda: next(receipt_owners),
    )
    monkeypatch.setattr(source_fingerprint_module, "capture_repository_source", capture)
    monkeypatch.setattr(storage_module, "LocalCAS", open_store)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", open_catalog)
    monkeypatch.setattr(
        cache_import_module,
        "import_compiler_cache_bm25",
        import_cache,
    )
    real_print = print

    def print_after_cleanup(*values: object, **kwargs: object) -> None:
        assert bm25_owner.closed
        assert context_owner.closed
        assert source.closed
        assert store.closed
        assert catalog.closed
        real_print(*values, **kwargs)

    monkeypatch.setattr("builtins.print", print_after_cleanup)

    assert cli._run_artifact_import_cache(args) == 0

    assert capture_calls == [
        (
            Path(args.repo),
            (cache, bm25_destination, context_destination, suggested),
        )
    ]
    assert len(bridge_calls) == 1
    passed_cache, passed = bridge_calls[0]
    assert passed_cache == cache.resolve(strict=True)
    assert passed["repository_source"] is source
    assert passed["bm25_output_owner"] is bm25_owner
    assert passed["context_output_owner"] is context_owner
    assert passed["bm25_destination"] == bm25_destination
    assert passed["context_destination"] == context_destination
    assert passed["repository_key"] == "owner/repo"
    assert passed["namespace_name"] == "default"
    assert passed["ref_name"] == "main"
    assert passed["expected_generation"] == 0
    forbidden = passed["forbidden_paths"]
    assert bm25_destination in forbidden
    assert context_destination in forbidden
    assert events.index("provider-support") < events.index("capture")
    assert events.index("capture") < events.index("cas-open")
    assert events.index("cas-open") < events.index("catalog-open")
    assert events.index("catalog-open") < events.index("bridge")
    assert events[-5:] == [
        "catalog-close",
        "cas-close",
        "context-close",
        "bm25-close",
        "source-close",
    ]
    assert bm25_destination.is_dir()
    assert context_destination.is_dir()
    assert not suggested.exists()
    output = capsys.readouterr().out
    assert "Snapshot:           snapshot-id" in output
    assert "Ref:                main" in output
    assert "Generation:         1" in output
    assert f"BM25 generation:    {bm25_destination}" in output
    assert f"Context generation: {context_destination}" in output
    assert f"Suggested output:   {suggested} (not reserved)" in output
    assert "codenib artifact materialize" in output
    assert "--snapshot snapshot-id" in output


def test_artifact_import_cache_failure_preserves_primary_and_published_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _cache_import_cli_test_args(tmp_path)
    nonce = "c" * 32
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: nonce)
    workspace_root = Path(args.workspace_root)
    bm25_destination = workspace_root / f".codenib-cache-import-{nonce}-bm25"
    context_destination = workspace_root / f".codenib-cache-import-{nonce}-context"
    primary = KeyboardInterrupt("import interrupted after publication")

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    class RetryableCloseable:
        def __init__(self) -> None:
            self.allow_close = False
            self.closed = False
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if not self.allow_close:
                raise OSError("injected cache import cleanup failure")
            self.closed = True

    bm25_owner = RetryableCloseable()
    context_owner = RetryableCloseable()
    source = RetryableCloseable()
    store = RetryableCloseable()
    catalog = RetryableCloseable()
    receipt_owners = iter((bm25_owner, context_owner))

    def capture(
        *_args: object,
        _source_owner,
        **_kwargs: object,
    ) -> RetryableCloseable:
        _source_owner(source)
        return source

    def fail_after_publication(*_args: object, **_kwargs: object) -> None:
        bm25_destination.mkdir()
        context_destination.mkdir()
        raise primary

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_new_retained_output_receipt_owner",
        lambda: next(receipt_owners),
    )
    monkeypatch.setattr(source_fingerprint_module, "capture_repository_source", capture)
    monkeypatch.setattr(storage_module, "LocalCAS", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(
        cache_import_module,
        "import_compiler_cache_bm25",
        fail_after_publication,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        cli._run_artifact_import_cache(args)

    assert raised.value is primary
    assert not bm25_owner.closed
    assert not context_owner.closed
    assert not source.closed
    assert not store.closed
    assert not catalog.closed
    assert bm25_owner.close_calls == 1
    assert context_owner.close_calls == 1
    assert source.close_calls == 1
    assert store.close_calls == 1
    assert catalog.close_calls == 1
    notes = _cleanup_notes(primary)
    assert any("SQLite catalog cleanup" in note for note in notes)
    assert any("local CAS cleanup" in note for note in notes)
    assert any("context receipt cleanup" in note for note in notes)
    assert any("BM25 receipt cleanup" in note for note in notes)
    assert any("repository source cleanup" in note for note in notes)
    assert bm25_destination.is_dir()
    assert context_destination.is_dir()
    warnings = capsys.readouterr().err
    assert "cache import BM25 generation now exists" in warnings
    assert "cache import context generation now exists" in warnings

    retained = primary.publication_cleanup_owners  # type: ignore[attr-defined]
    assert type(retained) is tuple
    assert len(retained) == 5
    for resource in (bm25_owner, context_owner, source, store, catalog):
        resource.allow_close = True
    for cleanup_owner in retained:
        cleanup_owner.close()
        assert cleanup_owner.closed
    assert bm25_owner.closed
    assert context_owner.closed
    assert source.closed
    assert store.closed
    assert catalog.closed


def test_artifact_import_cache_rejects_existing_destination_before_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _cache_import_cli_test_args(tmp_path)
    nonce = "d" * 32
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: nonce)
    destination = Path(args.workspace_root) / f".codenib-cache-import-{nonce}-bm25"
    destination.mkdir()

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_new_retained_output_receipt_owner",
        lambda: pytest.fail("receipt owners must not be created"),
    )
    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        lambda *_args, **_kwargs: pytest.fail("source must not be captured"),
    )
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: pytest.fail("CAS must not be opened"),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )

    with pytest.raises(cli.CLIError, match="output must be missing"):
        cli._run_artifact_import_cache(args)


def test_artifact_materialize_parser_exposes_strict_storage_inputs() -> None:
    base = [
        "artifact",
        "materialize",
        "--catalog",
        "/state/catalog.sqlite3",
        "--cas-root",
        "/state/cas",
        "--workspace-root",
        "/state/workspaces",
        "--repository",
        "owner/repo",
        "--output",
        "/state/workspaces/context",
    ]
    ref = cli.build_parser().parse_args(
        [*base, "--ref", "release", "--expected-generation", "7"]
    )
    snapshot = cli.build_parser().parse_args([*base, "--snapshot", "snap_123"])

    assert ref.handler is cli._run_artifact_materialize
    assert ref.ref == "release"
    assert ref.snapshot is None
    assert ref.expected_generation == 7
    assert snapshot.ref is None
    assert snapshot.snapshot == "snap_123"
    assert snapshot.expected_generation is None

    with pytest.raises(SystemExit) as both:
        cli.build_parser().parse_args(
            [*base, "--ref", "main", "--snapshot", "snap_123"]
        )
    assert both.value.code == 2
    with pytest.raises(SystemExit) as nonpositive:
        cli.build_parser().parse_args([*base, "--expected-generation", "0"])
    assert nonpositive.value.code == 2


def test_artifact_materialize_preflight_rejects_unsafe_selection_and_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(cli.CLIError, match="requires ref"):
        cli._retained_materialization_selector(
            ref_name=None,
            snapshot_id="snap_123",
            expected_generation=1,
        )

    args = cli.build_parser().parse_args(
        [
            "artifact",
            "materialize",
            "--catalog",
            str(tmp_path / "catalog.sqlite3"),
            "--cas-root",
            str(tmp_path / "cas"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
            "--repository",
            "owner/repo",
            "--output",
            str(tmp_path / "outside"),
        ]
    )
    with pytest.raises(cli.CLIError, match="below the workspace root"):
        cli._retained_materialization_paths(args)

    with pytest.raises(cli.CLIError, match="path is invalid"):
        cli._lexical_cli_path("bad\x00path", label="catalog")
    with pytest.raises(cli.CLIError, match="path is invalid"):
        cli._lexical_cli_path("~__codenib_missing_user__/catalog", label="catalog")


def test_artifact_materialize_rejects_physical_storage_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    catalog_target = workspace_root / "catalog.sqlite3"
    catalog_target.touch()
    catalog_link = tmp_path / "catalog-link.sqlite3"
    catalog_link.symlink_to(catalog_target)
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    output = workspace_root / "context"

    with pytest.raises(cli.CLIError, match="physically overlap the workspace"):
        cli._require_retained_materialization_topology(
            catalog_link,
            cas_root,
            workspace_root,
            output,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    redirected_parent = workspace_root / "redirected"
    redirected_parent.symlink_to(outside, target_is_directory=True)
    outside_catalog = tmp_path / "catalog.sqlite3"
    outside_catalog.touch()
    with pytest.raises(cli.CLIError, match="existing real directories"):
        cli._require_retained_materialization_topology(
            outside_catalog,
            cas_root,
            workspace_root,
            redirected_parent / "context",
        )

    real_parent = workspace_root / "real-parent"
    real_parent.mkdir()
    aliased_parent = workspace_root / "aliased-parent"
    aliased_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(cli.CLIError, match="existing real directories"):
        cli._require_retained_materialization_topology(
            outside_catalog,
            cas_root,
            workspace_root,
            aliased_parent / "context",
        )
    with pytest.raises(cli.CLIError, match="parent must already exist"):
        cli._require_retained_materialization_topology(
            outside_catalog,
            cas_root,
            workspace_root,
            workspace_root / "missing-parent" / "context",
        )

    hardlink = tmp_path / "catalog-hardlink.sqlite3"
    hardlink.hardlink_to(outside_catalog)
    with pytest.raises(cli.CLIError, match="single-linked regular file"):
        cli._require_retained_materialization_topology(
            outside_catalog,
            cas_root,
            workspace_root,
            output,
        )
    hardlink.unlink()

    support_calls = 0

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            nonlocal support_calls
            support_calls += 1

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: pytest.fail("CAS must not be opened"),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_link),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(output),
    )
    with pytest.raises(cli.CLIError, match="physically overlap the workspace"):
        cli._run_artifact_materialize(args)
    assert support_calls == 1


def test_artifact_materialize_opens_frozen_physical_storage_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    outside_catalog = tmp_path / "catalog.sqlite3"
    outside_catalog.touch()
    inside_catalog = workspace_root / "catalog.sqlite3"
    inside_catalog.touch()
    catalog_link = tmp_path / "catalog-link.sqlite3"
    catalog_link.symlink_to(outside_catalog)
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    catalog_identity = cli._retained_catalog_file_identity(outside_catalog)
    opened_catalogs: list[tuple[Path, dict[str, object]]] = []

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = Closeable()
    store = Closeable()

    def store_factory(root: Path, **_kwargs: object) -> Closeable:
        assert root == cas_root.resolve(strict=True)
        catalog_link.unlink()
        catalog_link.symlink_to(inside_catalog)
        return store

    def catalog_factory(path: Path, **_kwargs: object) -> None:
        opened_catalogs.append((path, _kwargs))
        raise RuntimeError("stop after catalog path capture")

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", store_factory)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", catalog_factory)
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_link),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )

    with pytest.raises(cli.CLIError, match="stop after catalog path capture"):
        cli._run_artifact_materialize(args)

    assert catalog_link.resolve(strict=True) == inside_catalog
    assert opened_catalogs == [
        (
            outside_catalog.resolve(strict=True),
            {
                "create": False,
                "expected_file_identity": catalog_identity,
            },
        )
    ]
    assert store.closed
    assert owner.closed


def test_artifact_materialize_rejects_catalog_leaf_rebinding_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    replacement = workspace_root / "replacement.sqlite3"
    replacement.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    catalog_opened = False

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = Closeable()
    store = Closeable()

    def store_factory(root: Path, **_kwargs: object) -> Closeable:
        assert root == cas_root.resolve(strict=True)
        catalog_path.unlink()
        catalog_path.symlink_to(replacement)
        return store

    def catalog_factory(*_args: object, **_kwargs: object) -> None:
        nonlocal catalog_opened
        catalog_opened = True
        pytest.fail("rebound catalog must not be opened")

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", store_factory)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", catalog_factory)
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_path),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )

    with pytest.raises(cli.CLIError, match="changed after physical topology"):
        cli._run_artifact_materialize(args)

    assert catalog_path.resolve(strict=True) == replacement
    assert not catalog_opened
    assert store.closed
    assert owner.closed


def test_artifact_materialize_rejects_catalog_rebinding_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    replacement = workspace_root / "replacement.sqlite3"
    replacement.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    catalog_identity = cli._retained_catalog_file_identity(catalog_path)

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = Closeable()
    store = Closeable()
    catalog = Closeable()

    def catalog_factory(path: Path, **kwargs: object) -> Closeable:
        assert path == catalog_path
        assert kwargs == {
            "create": False,
            "expected_file_identity": catalog_identity,
        }
        catalog_path.unlink()
        catalog_path.symlink_to(replacement)
        return catalog

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", catalog_factory)
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_path),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )

    with pytest.raises(cli.CLIError, match="changed after physical topology"):
        cli._run_artifact_materialize(args)

    assert catalog_path.resolve(strict=True) == replacement
    assert catalog.closed
    assert store.closed
    assert owner.closed


def test_artifact_materialize_rejects_cross_device_output_parent_before_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    mounted_parent = workspace_root / "mounted"
    mounted_parent.mkdir()
    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    mounted_metadata = mounted_parent.lstat()
    original_lstat = Path.lstat

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    def lstat_with_mount(path: Path):
        metadata = original_lstat(path)
        if path == mounted_parent:
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_dev=mounted_metadata.st_dev + 1,
            )
        return metadata

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(Path, "lstat", lstat_with_mount)
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: pytest.fail("CAS must not be opened"),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_path),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(mounted_parent / "context"),
    )

    with pytest.raises(cli.CLIError, match="workspace filesystem"):
        cli._run_artifact_materialize(args)


def test_artifact_materialize_rejects_directory_identity_alias_before_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    resolved_workspace = workspace_root.resolve(strict=True)
    resolved_cas = cas_root.resolve(strict=True)
    workspace_identity = cli._retained_real_directory_identity(
        resolved_workspace,
        label="workspace root",
    )
    real_identity = cli._retained_real_directory_identity

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    def aliased_identity(path: Path, *, label: str) -> tuple[int, int]:
        if path == resolved_cas:
            return workspace_identity
        return real_identity(path, label=label)

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_retained_real_directory_identity",
        aliased_identity,
    )
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: pytest.fail("CAS must not be opened"),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_path),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )

    with pytest.raises(cli.CLIError, match="physically alias the workspace root"):
        cli._run_artifact_materialize(args)


def test_artifact_materialize_rejects_nested_mount_before_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    mounted_parent = workspace_root / "mounted"
    mounted_parent.mkdir()
    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_retained_linux_mount_points",
        lambda: frozenset({mounted_parent.resolve(strict=True)}),
    )
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: pytest.fail("CAS must not be opened"),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )
    args = SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog_path),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(mounted_parent / "context"),
    )

    with pytest.raises(cli.CLIError, match="nested mount point"):
        cli._run_artifact_materialize(args)


def test_artifact_materialize_provider_failure_precedes_storage_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_output = tmp_path / "workspaces" / "context"
    existing_output.mkdir(parents=True)

    class UnsupportedProvider:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path / "workspaces"

        def require_support(self) -> None:
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", UnsupportedProvider)
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: pytest.fail("CAS must not be opened"),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )
    args = cli.build_parser().parse_args(
        [
            "artifact",
            "materialize",
            "--catalog",
            str(tmp_path / "catalog.sqlite3"),
            "--cas-root",
            str(tmp_path / "cas"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
            "--repository",
            "owner/repo",
            "--output",
            str(existing_output),
        ]
    )

    with pytest.raises(cli.CLIError, match="provider unavailable"):
        args.handler(args)


def _materialize_cli_test_args(tmp_path: Path) -> SimpleNamespace:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    return SimpleNamespace(
        repository="owner/repo",
        namespace="default",
        ref="main",
        snapshot=None,
        expected_generation=None,
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )


def test_artifact_materialize_resource_store_interruption_closes_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("resource result stored")

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = Closeable()
    store = Closeable()
    acquire = cli._RetainedMaterializationResourceOwner.acquire
    instructions = tuple(dis.get_instructions(acquire))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_resource"
    }
    assert len(store_indexes) == 1
    injection_offsets = {instructions[index + 1].offset for index in store_indexes}
    injected = False
    previous_trace = sys.gettrace()

    def trace(frame, event: str, _arg: object):
        nonlocal injected
        if event == "call" and frame.f_code is acquire.__code__:
            frame.f_trace_opcodes = True
            return trace
        if (
            not injected
            and event == "opcode"
            and frame.f_code is acquire.__code__
            and frame.f_lasti in injection_offsets
            and frame.f_locals["self"]._resource is store
        ):
            injected = True
            sys.settrace(None)
            raise interruption
        return trace

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )
    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            cli._run_artifact_materialize(_materialize_cli_test_args(tmp_path))
    finally:
        sys.settrace(previous_trace)

    assert injected
    assert raised.value is interruption
    assert store.closed
    assert owner.closed


def test_artifact_materialize_cleanup_failures_preserve_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("materialization primary")

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    class ReceiptOwner:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class RetryableResource:
        def __init__(self, label: str) -> None:
            self.label = label
            self.allow_close = False
            self.closed = False
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if not self.allow_close:
                raise OSError(f"{self.label} close failed")
            self.closed = True

    owner = ReceiptOwner()
    store = RetryableResource("CAS")
    catalog = RetryableResource("catalog")
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: catalog,
    )

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(
        materialization_module,
        "materialize_retained_repo_manifest_ref",
        fail_materialization,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        cli._run_artifact_materialize(_materialize_cli_test_args(tmp_path))

    assert raised.value is primary
    assert owner.closed
    assert store.close_calls == 1
    assert catalog.close_calls == 1
    notes = _cleanup_notes(primary)
    assert any("SQLite catalog cleanup" in note for note in notes)
    assert any("local CAS cleanup" in note for note in notes)
    retained = primary.publication_cleanup_owners  # type: ignore[attr-defined]
    assert type(retained) is tuple
    assert len(retained) == 2
    store.allow_close = True
    catalog.allow_close = True
    for cleanup_owner in retained:
        cleanup_owner.close()
        assert cleanup_owner.closed
    assert store.closed
    assert catalog.closed


def test_artifact_materialize_warning_failure_is_secondary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("materialization primary")
    warning_error = OSError("warning failed")

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = Closeable()
    store = Closeable()
    catalog = Closeable()
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(
        materialization_module,
        "materialize_retained_repo_manifest_ref",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(primary),
    )
    monkeypatch.setattr(
        cli,
        "_warn_possible_retained_publication",
        lambda _output: (_ for _ in ()).throw(warning_error),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        cli._run_artifact_materialize(_materialize_cli_test_args(tmp_path))

    assert raised.value is primary
    assert owner.closed
    assert store.closed
    assert catalog.closed
    assert any(
        "retained publication warning also failed" in note
        for note in _cleanup_notes(primary)
    )


def test_artifact_materialize_print_interruption_warns_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("success output interrupted")

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = Closeable()
    store = Closeable()
    catalog = Closeable()
    args = _materialize_cli_test_args(tmp_path)
    output = Path(args.output)

    def materialize(*_args: object, **_kwargs: object) -> SimpleNamespace:
        output.mkdir()
        return SimpleNamespace(
            export_receipt=SimpleNamespace(
                ref_name="main",
                ref_generation=1,
                snapshot_id="snapshot-id",
            ),
            artifact=SimpleNamespace(
                repository="owner/repo",
                commit="a" * 40,
                views=("bm25",),
                output_dir=output,
            ),
        )

    warning_outputs: list[Path] = []
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_new_retained_output_receipt_owner", lambda: owner)
    monkeypatch.setattr(storage_module, "LocalCAS", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(
        materialization_module,
        "materialize_retained_repo_manifest_ref",
        materialize,
    )
    monkeypatch.setattr(
        cli,
        "_warn_possible_retained_publication",
        warning_outputs.append,
    )

    def interrupt_print(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr("builtins.print", interrupt_print)

    with pytest.raises(KeyboardInterrupt) as raised:
        cli._run_artifact_materialize(args)

    assert raised.value is primary
    assert owner.closed
    assert store.closed
    assert catalog.closed
    assert output.is_dir()
    assert warning_outputs == [output]


def test_wiki_parser_accepts_headless_quality_audit() -> None:
    args = cli.build_parser().parse_args(["wiki", ".", "--audit", "--audit-json"])

    assert args.audit is True
    assert args.audit_json is True


def test_doctor_parser_accepts_model_backend_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--require",
            "agent",
            "--model",
            "openai/local-model",
            "--model-provider",
            "openai",
            "--api-base",
            "http://localhost:4000/v1",
            "--api-key-env",
            "LOCAL_LLM_KEY",
            "--model-option",
            "api_version=2025-01-01",
            "--model-option",
            "extra_body.reasoning.enabled=false",
            "--probe-model",
        ]
    )

    assert args.model == "openai/local-model"
    assert args.model_provider == "openai"
    assert args.api_base == "http://localhost:4000/v1"
    assert args.api_key_env == "LOCAL_LLM_KEY"
    assert args.model_option == [
        "api_version=2025-01-01",
        "extra_body.reasoning.enabled=false",
    ]
    assert args.probe_model is True


def test_index_parser_accepts_remote_embedding_route() -> None:
    args = cli.build_parser().parse_args(
        [
            "index",
            ".",
            "--preset",
            "semantic",
            "--embedding-provider",
            "openai",
            "--embedding-model",
            "text-embedding-3-small",
            "--embedding-dimension",
            "1536",
            "--embedding-endpoint",
            "https://inference.example.test/v1",
            "--embedding-api-key-env",
            "MODELS_TOKEN",
        ]
    )

    assert args.embedding_provider == "openai"
    assert args.embedding_model == "text-embedding-3-small"
    assert args.embedding_dimension == 1536
    assert args.embedding_endpoint == "https://inference.example.test/v1"
    assert args.embedding_api_key_env == "MODELS_TOKEN"


def test_doctor_parser_accepts_repository_graph_context() -> None:
    args = cli.build_parser().parse_args(
        [
            "doctor",
            ".",
            "--require",
            "graph",
            "--language",
            "python,typescript",
        ]
    )

    assert args.repo == "."
    assert args.require == ["graph"]
    assert args.language == ["python,typescript"]


@pytest.mark.parametrize(
    ("tools", "expected_code", "expected_status"),
    [
        ({"scip-go": "/tools/scip-go"}, 1, "[MISSING] Python (python)"),
        ({"scip-python": "/tools/scip-python"}, 0, "[OK     ] Python (python)"),
    ],
)
def test_doctor_requires_the_repository_language_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tools: dict[str, str],
    expected_code: int,
    expected_status: str,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    monkeypatch.setattr(cli, "_check_module", lambda _name: True)
    monkeypatch.setattr(cli.shutil, "which", lambda command: tools.get(command))

    code = cli.run(
        [
            "doctor",
            str(tmp_path),
            "--require",
            "graph",
            "--language",
            "python",
        ]
    )

    output = capsys.readouterr().out
    assert code == expected_code
    assert expected_status in output
    assert "scip; command=scip-python index" in output


def test_doctor_model_config_reports_missing_named_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_LLM_KEY", raising=False)
    args = SimpleNamespace(
        model="openai/local-model",
        api_base="http://localhost:4000/v1",
        api_key_env="MISSING_LLM_KEY",
    )

    label, ok, detail = cli._doctor_model_config(args)

    assert label == "Model configuration"
    assert ok is False
    assert "MISSING_LLM_KEY is unset" in detail


def test_doctor_model_config_uses_litellm_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    captured = {}

    def validate_environment(**kwargs):
        captured.update(kwargs)
        return {"keys_in_environment": True, "missing_keys": []}

    monkeypatch.setenv("LOCAL_LLM_KEY", "secret")
    monkeypatch.setattr(cli, "_check_module", lambda _name: True)
    monkeypatch.setattr(litellm, "validate_environment", validate_environment)
    args = SimpleNamespace(
        model="openai/local-model",
        api_base="http://localhost:4000/v1",
        api_key_env="LOCAL_LLM_KEY",
    )

    _, ok, detail = cli._doctor_model_config(args)

    assert ok is True
    assert "endpoint=http://localhost:4000/v1" in detail
    assert captured == {
        "model": "openai/local-model",
        "api_base": "http://localhost:4000/v1",
        "api_key": "secret",
    }


def test_doctor_model_probe_checks_product_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.llm.litellm_chat as chat_module

    class FakeStructured:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, _messages):
            return self.schema(status="ok")

    class FakeLLM:
        def __init__(self, **_kwargs):
            pass

        def complete(self, _messages):
            return "OK"

        def _call_raw(self, _messages, **_kwargs):
            call = SimpleNamespace(
                function=SimpleNamespace(name="report_backend_ready")
            )
            message = SimpleNamespace(tool_calls=[call])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        def with_structured_output(self, schema):
            return FakeStructured(schema)

    monkeypatch.setattr(chat_module, "LiteLLMChat", FakeLLM)
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--model",
            "ollama/qwen3:8b",
            "--api-base",
            "http://localhost:11434",
            "--probe-model",
        ]
    )

    checks = cli._probe_doctor_model(args)

    assert checks == [
        ("Model text probe", True, "response received"),
        ("Model tool probe", True, "function call received"),
        ("Model structured probe", True, "schema response received"),
    ]


def test_doctor_model_probe_stops_after_text_failure_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.llm.litellm_chat as chat_module

    class FailingLLM:
        def __init__(self, **_kwargs):
            pass

        def complete(self, _messages):
            raise RuntimeError("authentication failed for secret-value")

    monkeypatch.setattr(chat_module, "LiteLLMChat", FailingLLM)
    monkeypatch.setenv("MODEL_KEY", "secret-value")
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--model",
            "openai/model",
            "--api-key-env",
            "MODEL_KEY",
            "--probe-model",
        ]
    )

    checks = cli._probe_doctor_model(args)

    assert checks[0] == (
        "Model text probe",
        False,
        "authentication failed for ***",
    )
    assert checks[1][2] == "skipped: text completion failed"
    assert checks[2][2] == "skipped: text completion failed"


def test_cli_model_options_layer_environment_and_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CODENIB_DEMO_MODEL_OPTIONS",
        '{"timeout":20,"extra_body":{"reasoning":{"enabled":true}}}',
    )
    monkeypatch.setenv(
        "CODENIB_DEMO_WIKI_MODEL_OPTIONS",
        '{"timeout":90,"extra_body":{"wiki_only":true}}',
    )
    args = SimpleNamespace(
        model="openai/local-model",
        model_option=[
            "timeout=45",
            "extra_body.reasoning.enabled=false",
        ],
    )

    assert cli._model_options_for_args(args) == {
        "timeout": 45,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert cli._model_options_for_args(
        args,
        include_wiki_environment=True,
    ) == {
        "timeout": 45,
        "extra_body": {
            "reasoning": {"enabled": False},
            "wiki_only": True,
        },
    }


def test_openai_chat_route_maps_to_litellm_without_storing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELS_TOKEN", "runtime-secret")
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--model-provider",
            "openai",
            "--model",
            "gpt-4.1",
            "--api-base",
            "https://inference.example.test/v1",
            "--api-key-env",
            "MODELS_TOKEN",
        ]
    )

    backend = cli._model_backend_for_args(args)

    assert backend is not None
    assert backend.model == "openai/gpt-4.1"
    assert backend.api_base == "https://inference.example.test/v1"
    assert backend.api_key == "runtime-secret"
    assert backend.auth_source == "MODELS_TOKEN"
    assert "runtime-secret" not in repr(backend)


def test_remote_embedding_defaults_and_requires_dimension_for_custom_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")
    default_args = cli.build_parser().parse_args(
        ["index", "--embedding-provider", "openai"]
    )

    route = cli._embedding_route_for_args(default_args)

    assert route.model == "text-embedding-3-small"
    assert route.dimension == 1536
    custom_args = cli.build_parser().parse_args(
        [
            "index",
            "--embedding-provider",
            "openai",
            "--embedding-model",
            "vendor/custom-model",
        ]
    )
    with pytest.raises(cli.CLIError, match="embedding-dimension"):
        cli._embedding_route_for_args(custom_args)


def test_embedding_dimension_rejects_boolean_values() -> None:
    with pytest.raises(cli.CLIError, match="positive integer"):
        cli._optional_int(True, source="embedding dimension")


def test_openai_embedding_reports_missing_credential_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = cli.build_parser().parse_args(["index", "--embedding-provider", "openai"])

    route = cli._embedding_route_for_args(args)

    with pytest.raises(ValueError) as raised:
        route.credential()
    assert str(raised.value) == (
        "credential environment variable is unset or empty: OPENAI_API_KEY"
    )


def test_fast_index_does_not_resolve_unused_embedding_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    monkeypatch.setenv("CODENIB_EMBEDDING_PROVIDER", "not a provider")
    captured = {}

    def fake_index(repo_path, **kwargs):
        captured.update(repo_path=repo_path, **kwargs)
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=kwargs["languages"],
                indexes={"bm25": SimpleNamespace(status="fresh", metadata={})},
            ),
            [],
        )

    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert cli.run(["index", str(tmp_path), "--preset", "fast"]) == 0
    assert captured["views"] == ["bm25"]
    assert "embedding_provider" not in captured


def test_remote_semantic_dependency_check_does_not_require_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = []

    def check(module):
        checked.append(module)
        return module in {"faiss", "openai"}

    monkeypatch.setattr(cli, "_check_module", check)

    cli._check_view_dependencies(
        ["vector"],
        embedding_provider="openai",
    )

    assert "faiss" in checked
    assert "openai" in checked
    assert "sentence_transformers" not in checked


def test_doctor_reports_remote_embedding_route_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-secret")
    monkeypatch.setattr(
        cli, "_check_module", lambda module: module in {"faiss", "openai"}
    )
    args = cli.build_parser().parse_args(["doctor", "--embedding-provider", "openai"])

    semantic = cli._doctor_rows(args)["semantic"]
    checks = {label: (ok, detail) for label, ok, detail in semantic}

    assert checks["Embedding route"][0] is True
    assert "openai:text-embedding-3-small" in checks["Embedding route"][1]
    assert "auth=OPENAI_API_KEY" in checks["Embedding route"][1]
    assert "doctor-secret" not in str(semantic)
    assert checks["OpenAI SDK"] == (True, "installed")
    assert "sentence-transformers" not in checks


def test_doctor_embedding_probe_uses_resolved_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.index.embedding.vector_store as vector_module

    captured = {}

    class FakeStore:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.dimension = kwargs["dimension"]

        def close(self):
            captured["closed"] = True

    monkeypatch.setenv("OPENAI_API_KEY", "probe-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(vector_module, "CodeVectorStore", FakeStore)
    args = cli.build_parser().parse_args(
        ["doctor", "--embedding-provider", "openai", "--probe-embedding"]
    )

    check = cli._probe_doctor_embedding(args)

    assert check == ("Embedding probe", True, "vector received; dimension=1536")
    assert captured["embedding_provider"] == "openai"
    assert "base_url" not in captured
    assert captured["api_key"] == "probe-secret"
    assert captured["closed"] is True


def test_detect_languages_orders_by_file_count_and_skips_generated_dirs(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("def one():\n    pass\n")
    (tmp_path / "src" / "two.py").write_text("def two():\n    pass\n")
    (tmp_path / "main.go").write_text("package main\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.ts").write_text("export {};\n")
    (tmp_path / ".next").mkdir()
    (tmp_path / ".next" / "ignored.js").write_text("export {};\n")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "ignored.cpp").write_text("int generated = 1;\n")

    assert cli.detect_languages(tmp_path) == ["python", "go"]


def test_normalize_languages_accepts_aliases_and_commas() -> None:
    assert cli.normalize_languages(["py,typescript", "c++"]) == [
        "python",
        "typescript",
        "cpp",
    ]


def test_resolve_manifest_path_accepts_repository_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.paths import repo_index_dir

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    manifest = repo_index_dir(tmp_path) / "repo_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")

    assert cli.resolve_manifest_path(str(tmp_path)) == manifest


def test_resolve_manifest_path_falls_back_to_legacy_repository_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    manifest = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}")

    assert cli.resolve_manifest_path(str(tmp_path)) == manifest


def test_index_command_auto_preset_falls_back_without_dense_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "main.py").write_text("def main():\n    return 0\n")
    captured = {}
    monkeypatch.setattr(cli, "_check_module", lambda _module: False)

    def fake_index(repo_path, *, languages, views, rebuild):
        captured.update(
            repo_path=repo_path,
            languages=languages,
            views=views,
            rebuild=rebuild,
        )
        entry = SimpleNamespace(
            status="fresh",
            metadata={"build_duration_seconds": 0.1},
        )
        manifest = SimpleNamespace(
            repo_path=str(repo_path),
            languages=list(languages),
            indexes={"bm25": entry},
        )
        return manifest, []

    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert cli.run(["index", str(tmp_path)]) == 0
    assert captured == {
        "repo_path": tmp_path,
        "languages": ["python"],
        "views": ["bm25"],
        "rebuild": False,
    }


def test_auto_preset_selects_hybrid_views_when_semantic_extra_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(["index", "."])
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)

    assert args.preset == "auto"
    assert cli._selected_views_for_args(args) == ["bm25", "vector"]


def test_auto_preset_preserves_an_explicit_embedding_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(
        ["index", ".", "--embedding-provider", "openai"]
    )
    monkeypatch.setattr(cli, "_check_module", lambda _module: False)

    assert cli._selected_views_for_args(args) == ["bm25", "vector"]


def test_fast_index_builds_fresh_bm25_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.paths import repo_index_dir

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    (tmp_path / "sample.py").write_text(
        "def greet(name: str) -> str:\n"
        '    """Return a greeting."""\n'
        '    return f"hello {name}"\n'
    )

    manifest, failed = cli.index_repository(
        tmp_path,
        languages=["python"],
        views=["bm25"],
    )

    assert failed == []
    assert manifest.languages == ["python"]
    assert manifest.indexes["bm25"].status == "fresh"
    assert (repo_index_dir(tmp_path) / "repo_manifest.json").is_file()
    assert not (tmp_path / ".codenib_cache").exists()


def test_semantic_preset_reports_required_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    monkeypatch.setattr(
        cli,
        "_check_module",
        lambda module: module != "faiss",
    )

    assert cli.run(["index", str(tmp_path), "--preset", "semantic"]) == 2
    assert "codenib[semantic]" in capsys.readouterr().err


def test_semantic_index_passes_resolved_openai_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    captured = {}

    def fake_index(repo_path, **kwargs):
        captured.update(repo_path=repo_path, **kwargs)
        entries = {
            view: SimpleNamespace(status="fresh", metadata={})
            for view in kwargs["views"]
        }
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=kwargs["languages"],
                indexes=entries,
            ),
            [],
        )

    monkeypatch.setattr(cli, "index_repository", fake_index)

    result = cli.run(
        [
            "index",
            str(tmp_path),
            "--preset",
            "semantic",
            "--embedding-provider",
            "openai",
        ]
    )

    assert result == 0
    assert captured["embedding_provider"] == "openai"
    assert captured["embedding_model"] == "text-embedding-3-small"
    assert captured["embedding_dimension"] == 1536
    assert captured["embedding_endpoint"] is None
    assert captured["embedding_credential_env"] == "OPENAI_API_KEY"
    assert "runtime-secret" not in str(captured)


def test_graph_preset_selects_bm25_and_symbol_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    captured = {}

    def fake_index(repo_path, *, languages, views, rebuild):
        captured["views"] = views
        entries = {view: SimpleNamespace(status="fresh", metadata={}) for view in views}
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=list(languages),
                indexes=entries,
            ),
            [],
        )

    monkeypatch.setattr(cli, "_check_view_dependencies", lambda _views: None)
    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert cli.run(["index", str(tmp_path), "--preset", "graph"]) == 0
    assert captured["views"] == ["bm25", "symbol_graph"]


def test_index_summary_reports_partial_graph_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    entry = SimpleNamespace(
        status="fresh",
        metadata={
            "build_duration_seconds": 1.25,
            "partial": True,
            "available_languages": ["python", "typescript"],
            "failed_languages": {"cpp": "compilation database missing"},
        },
    )
    manifest = SimpleNamespace(
        repo_path=str(tmp_path),
        languages=["python", "typescript", "cpp"],
        indexes={"symbol_graph": entry},
    )

    cli._print_index_summary(manifest, ["symbol_graph"])

    output = capsys.readouterr().out
    assert "symbol_graph" in output
    assert "partial: python, typescript" in output
    assert "unavailable: cpp" in output


def test_mcp_command_reports_required_extra(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_check_module", lambda _module: False)

    assert cli.run(["mcp"]) == 2
    assert "codenib[mcp]" in capsys.readouterr().err


def test_mcp_parser_limits_tool_surface_and_defaults_to_full() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["mcp"]).tool_surface == "full"
    assert parser.parse_args(["mcp", "--tool-surface", "explore"]).tool_surface == (
        "explore"
    )
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["mcp", "--tool-surface", "hidden"])

    assert exc_info.value.code == 2


def test_mcp_runtime_probe_checks_the_complete_codegraph_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codenib.mcp import server

    required: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(
        cli,
        "_require_modules",
        lambda modules, **kwargs: required.append((tuple(modules), kwargs["extra"])),
    )
    monkeypatch.setattr(
        server,
        "main",
        lambda _command: pytest.fail("runtime probe must not start the MCP server"),
    )

    assert cli.run(["mcp", "--runtime-probe"]) == 0
    assert required == [(("mcp", "igraph", "google.protobuf"), "graph,mcp")]
    assert capsys.readouterr().out == "codenib codegraph mcp runtime ready\n"


def test_mcp_manifest_forwards_explore_tool_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.mcp import server

    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}")
    calls = []
    monkeypatch.setattr(cli, "_require_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest_path)
    monkeypatch.setattr(server, "main", calls.append)
    args = cli.build_parser().parse_args(
        [
            "mcp",
            str(tmp_path),
            "--log-level",
            "WARNING",
            "--tool-surface",
            "explore",
        ]
    )

    assert cli._run_mcp(args) == 0
    assert calls == [
        [
            str(manifest_path),
            "--log-level",
            "WARNING",
            "--tool-surface",
            "explore",
        ]
    ]


def test_mcp_portable_artifact_forwards_full_tool_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.mcp import server

    artifact_path = tmp_path / "portable-context"
    repo_path = tmp_path / "checkout"
    calls = []
    monkeypatch.setattr(cli, "_require_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "resolve_repo_path", lambda _value: repo_path)
    monkeypatch.setattr(server, "main", calls.append)
    args = cli.build_parser().parse_args(
        [
            "mcp",
            "--artifact",
            str(artifact_path),
            "--repo",
            "ignored-checkout",
            "--repository",
            "owner/repository",
            "--log-level",
            "ERROR",
        ]
    )

    assert cli._run_mcp(args) == 0
    assert calls == [
        [
            "--artifact",
            str(artifact_path.resolve()),
            "--repo",
            str(repo_path),
            "--log-level",
            "ERROR",
            "--tool-surface",
            "full",
            "--repository",
            "owner/repository",
        ]
    ]


def _test_source_fingerprint(repo: Path, cache: Path) -> str:
    from codenib.source_fingerprint import fingerprint_repository

    return fingerprint_repository(repo, exclude_roots=(cache,)).value


def test_prepare_local_wiki_writes_single_repo_registry(
    tmp_path: Path,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(manifest_path.parent / "bm25"),
                built_at="2026-07-25T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                commit="abc123",
                source_fingerprint=source,
            )
        },
    )
    manifest.save(str(manifest_path))

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
    )

    assert local.config_path.is_file()
    assert (local.data_dir / "qa_registry.json").is_file()
    assert local.repo_id == tmp_path.name.lower()


def test_prepare_local_wiki_capture_return_cancellation_closes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    captured: list[RepositorySourceBinding] = []
    real_capture = local_module.capture_repository_source
    interruption = SystemExit("injected after local wiki capture returned")

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        raise interruption

    monkeypatch.setattr(local_module, "capture_repository_source", capture)
    with pytest.raises(SystemExit) as caught:
        prepare_local_wiki(tmp_path, manifest_path, frontend_port=3000)

    assert caught.value is interruption
    assert len(captured) == 1
    assert captured[0].closed


def test_prepare_local_wiki_persistent_close_preserves_primary_and_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    source_path = tmp_path / "module.py"
    source_path.write_text("VALUE = 1\n")
    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    source_path.write_text("VALUE = 2\n")
    captured: list[RepositorySourceBinding] = []
    real_capture = local_module.capture_repository_source
    real_close = RepositorySourceBinding.close

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        return binding

    def persistent_eio(binding: RepositorySourceBinding) -> None:
        if binding in captured:
            raise OSError(errno.EIO, "injected local wiki source close EIO")
        real_close(binding)

    monkeypatch.setattr(local_module, "capture_repository_source", capture)
    monkeypatch.setattr(RepositorySourceBinding, "close", persistent_eio)
    with pytest.raises(ValueError, match="does not match the manifest") as caught:
        prepare_local_wiki(tmp_path, manifest_path, frontend_port=3000)

    assert len(captured) == 1
    owner = caught.value.source_cleanup_owner
    assert owner.pending_sources == (captured[0],)
    assert not captured[0].closed

    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    owner.close()
    assert owner.closed
    assert captured[0].closed


def test_prepare_local_wiki_treats_manifest_commit_as_indexed_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        commit="a" * 40,
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setattr(
        "codenib.compiler.checkout_identity.checkout_commit",
        lambda _repo_path: pytest.fail(
            "local content authentication must not claim a Git HEAD attestation"
        ),
    )

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
    )

    registry = json.loads((local.data_dir / "qa_registry.json").read_text())
    assert registry[0]["base_commit"] == "a" * 40


def test_prepare_local_wiki_rejects_changed_source_at_same_commit(
    tmp_path: Path,
) -> None:
    from codenib.compiler.manifest import RepoManifest
    from codenib.source_fingerprint import fingerprint_repository

    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    fingerprint = fingerprint_repository(
        tmp_path,
        exclude_roots=(manifest_path.parent,),
    ).value
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=fingerprint,
        languages=["python"],
    ).save(str(manifest_path))
    source.write_text("VALUE = 2\n")

    with pytest.raises(
        ValueError,
        match="repository source content does not match the manifest",
    ):
        prepare_local_wiki(
            tmp_path,
            manifest_path,
            frontend_port=3000,
        )


def test_prepare_local_wiki_uses_github_origin_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setattr(
        "codenib.web.local._origin_url",
        lambda _repo_path: "git@github.com:Example/Project.git",
    )

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
    )

    registry = json.loads((local.data_dir / "qa_registry.json").read_text())
    assert local.repo_id == "project"
    assert registry[0]["repo"] == "example/project"


def test_prepare_generated_wiki_keeps_secret_out_of_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(manifest_path.parent / "bm25"),
                built_at="2026-07-25T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                commit="abc123",
                source_fingerprint=source,
            )
        },
    ).save(str(manifest_path))
    monkeypatch.setenv("LOCAL_LLM_KEY", "super-secret")

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
        agent_wiki=True,
        model="openai/local-model",
        api_base="http://localhost:4000/v1",
        api_key_env="LOCAL_LLM_KEY",
        model_options={
            "api_version": "2025-01-01",
            "extra_body": {"reasoning": {"enabled": False}},
        },
    )

    config = yaml.safe_load(local.config_path.read_text())
    assert config["wiki_agent"] is True
    assert config["model"] == "openai/local-model"
    assert config["model_api_base"] == "http://localhost:4000/v1"
    assert config["model_options"] == {
        "api_version": "2025-01-01",
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert "key" not in local.config_path.read_text().lower()
    assert local.runtime_env == {
        "CODENIB_DEMO_MODEL": "openai/local-model",
        "CODENIB_DEMO_API_BASE": "http://localhost:4000/v1",
        "CODENIB_DEMO_MODEL_OPTIONS": (
            '{"api_version":"2025-01-01","extra_body":'
            '{"reasoning":{"enabled":false}}}'
        ),
        "CODENIB_DEMO_API_KEY": "super-secret",
    }


def test_prepare_local_wiki_keeps_embedding_secret_process_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setenv("EMBEDDING_KEY", "embedding-secret")

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
        embedding_api_key_env="EMBEDDING_KEY",
    )

    assert "embedding-secret" not in local.config_path.read_text()
    assert local.runtime_env["CODENIB_EMBEDDING_API_KEY"] == "embedding-secret"


def test_wiki_audit_injects_local_native_authority_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    config = SimpleNamespace(
        model="model",
        model_api_base=None,
        model_api_key=None,
        embedding_api_key=None,
        wiki_generation_model="model",
        wiki_generation_api_base=None,
        wiki_generation_api_key=None,
        wiki_generation_options={},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo="owner/sample", base_commit="abc123")
    )

    def resolver(_repo_entry, _manifest, _vector_entry):
        return object()

    class Registry:
        def __init__(self, supplied_config, **kwargs):
            captured["config"] = supplied_config
            captured.update(kwargs)

        def load_all(self):
            captured["loaded"] = True

        def get(self, repo_id):
            assert repo_id == "sample"
            return bundle

    monkeypatch.setattr("codenib.web.config.load_config", lambda _path: config)
    monkeypatch.setattr(
        "codenib.web.native_authority.authorize_local_manifest_vector",
        resolver,
    )
    monkeypatch.setattr("codenib.web.repo_registry.RepoRegistry", Registry)
    monkeypatch.setitem(
        sys.modules,
        "codenib.llm.litellm_chat",
        SimpleNamespace(LiteLLMChat=lambda **_kwargs: object()),
    )
    monkeypatch.setitem(
        sys.modules,
        "codenib.wiki.agent_wiki",
        SimpleNamespace(AgentWiki=lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setitem(
        sys.modules,
        "codenib.wiki.quality",
        SimpleNamespace(audit_wiki=lambda _builder: {"passed": True}),
    )
    local = SimpleNamespace(
        config_path=tmp_path / "config.yaml",
        runtime_env={},
        repo_id="sample",
        data_dir=tmp_path,
    )

    report = cli._audit_local_wiki(local)

    assert report["passed"] is True
    assert captured == {
        "config": config,
        "native_index_authorization_resolver": resolver,
        "loaded": True,
    }


def test_wiki_audit_exits_without_starting_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    manifest = tmp_path / "repo_manifest.json"
    manifest.write_text("{}")
    prepared = SimpleNamespace(repo_id="sample")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest)
    monkeypatch.setattr(
        "codenib.web.local.prepare_local_wiki",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        cli,
        "_audit_local_wiki",
        lambda _local: {
            "repository": "owner/sample",
            "passed": True,
            "expected_pages": 2,
            "generated_pages": 2,
            "ready_pages": 2,
            "grounding_valid": 2,
            "structural_valid": 2,
            "narrative_valid": 2,
            "fallbacks": 0,
            "details": [],
        },
    )
    monkeypatch.setattr(
        "codenib.web.launcher.launch_local_wiki",
        lambda *_args, **_kwargs: pytest.fail("frontend should not start"),
    )

    result = cli.run(["wiki", str(tmp_path), "--no-index", "--audit"])

    output = capsys.readouterr().out
    assert result == 0
    assert "Ready:      2/2" in output
    assert "Result: PASS" in output


def test_wiki_generate_resolves_openai_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(tmp_path),
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "bm25"),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
            )
        },
    ).save(manifest_path)
    captured = {}
    local = SimpleNamespace(runtime_env={})
    monkeypatch.setenv("MODELS_TOKEN", "runtime-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest_path)

    def prepare(*_args, **kwargs):
        captured.update(kwargs)
        return local

    monkeypatch.setattr("codenib.web.local.prepare_local_wiki", prepare)
    monkeypatch.setattr(
        "codenib.web.launcher.launch_local_wiki",
        lambda *_args, **_kwargs: 0,
    )

    result = cli.run(
        [
            "wiki",
            str(tmp_path),
            "--no-index",
            "--generate",
            "--model-provider",
            "openai",
            "--model",
            "gpt-4.1",
            "--api-base",
            "https://inference.example.test/v1",
            "--api-key-env",
            "MODELS_TOKEN",
            "--no-open",
        ]
    )

    assert result == 0
    assert captured["model"] == "openai/gpt-4.1"
    assert captured["api_base"] == "https://inference.example.test/v1"
    assert captured["api_key_env"] == "MODELS_TOKEN"
    assert "runtime-secret" not in str(captured)


def test_wiki_rejects_embedding_route_that_disagrees_with_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codenib.compiler.index_builders import VectorIndexBuilder
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(tmp_path),
        languages=["python"],
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(tmp_path / "vector"),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                config=VectorIndexBuilder().artifact_identity(),
            )
        },
    ).save(manifest_path)
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest_path)

    result = cli.run(
        [
            "wiki",
            str(tmp_path),
            "--no-index",
            "--embedding-provider",
            "openai",
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert "does not match the current vector artifact" in error
    assert "runtime-secret" not in error


def test_installed_package_frontend_is_prebuilt(
    tmp_path: Path,
) -> None:
    packaged = tmp_path / "site-packages" / "codenib" / "web" / "frontend"
    packaged.mkdir(parents=True)
    (packaged / "index.html").write_text("<title>CodeNib Wiki</title>\n")

    assert launcher.is_prebuilt_frontend(packaged) is True


def test_vite_source_checkout_is_not_treated_as_prebuilt(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text('<div id="root"></div>\n')
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}\n')

    assert launcher.is_prebuilt_frontend(tmp_path) is False


def test_doctor_does_not_require_node_for_prebuilt_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<title>CodeNib Wiki</title>\n")
    monkeypatch.setattr(launcher, "find_frontend_dir", lambda: frontend)
    monkeypatch.setattr(launcher, "node_runtime_status", lambda: (False, "missing"))
    monkeypatch.setattr("codenib.cli.shutil.which", lambda _command: None)

    rows = cli._doctor_rows()
    wiki = {name: (ok, detail) for name, ok, detail in rows["wiki"]}

    assert wiki["Node.js"] == (True, "not required (prebuilt frontend)")
    assert wiki["npm"] == (True, "not required (prebuilt frontend)")


@pytest.mark.parametrize(
    ("version", "expected_ok"),
    [
        ("v18.17.1", False),
        ("v20.18.1", False),
        ("v20.19.0", True),
        ("v21.7.3", False),
        ("v22.11.0", False),
        ("v22.12.0", True),
        ("v24.0.0", True),
    ],
)
def test_node_runtime_status_matches_vite_requirement(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    expected_ok: bool,
) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, f"{version}\n", ""),
    )

    ok, detail = launcher.node_runtime_status()

    assert ok is expected_ok
    if expected_ok:
        assert detail == version
    else:
        assert detail == f"{version} (requires ^20.19.0 or >=22.12.0)"
