# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import errno
import io
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path, PurePosixPath
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
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import RepositorySourceBinding
from codenib.web import launcher
from codenib.web.local import prepare_local_wiki

_TEST_COMMIT = "c" * 40
_TEST_SOURCE_FINGERPRINT = "sha256-v2:" + ("d" * 64)


def _current_manifest_with_fresh_index(repo: Path, entry) -> object:
    from codenib.compiler.manifest import RepoManifest

    selection = RepositorySourceSelection()
    entry.commit = _TEST_COMMIT
    entry.source_fingerprint = _TEST_SOURCE_FINGERPRINT
    entry.source_selection_digest = selection.digest
    return RepoManifest(
        repo_path=str(repo),
        commit=_TEST_COMMIT,
        last_indexed_commit=_TEST_COMMIT,
        source_fingerprint=_TEST_SOURCE_FINGERPRINT,
        last_indexed_source_fingerprint=_TEST_SOURCE_FINGERPRINT,
        source_selection=selection,
        last_indexed_source_selection_digest=selection.digest,
        languages=["python"],
        indexes={entry.index_type: entry},
    )


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


def test_index_parser_accepts_exact_source_exclusions() -> None:
    args = cli.build_parser().parse_args(
        [
            "index",
            ".",
            "--exclude-dir",
            "ios/Pods",
            "--exclude-dir",
            "generated/api",
        ]
    )

    assert args.exclude_dir == ["ios/Pods", "generated/api"]
    assert args.clear_exclude_dirs is False


def test_index_parser_exposes_retained_publication_as_an_explicit_group() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "index",
            "/src/repo",
            "--preset",
            "fast",
            "--publish-retained",
            "--catalog",
            "/state/catalog.sqlite3",
            "--cas-root",
            "/state/cas",
            "--workspace-root",
            "/state/workspaces",
            "--repository",
            "owner/repo",
            "--namespace",
            "production",
            "--ref",
            "release",
            "--expected-generation",
            "7",
        ]
    )
    defaults = parser.parse_args(["index", "/src/repo", "--preset", "fast"])

    assert args.publish_retained is True
    assert args.catalog == "/state/catalog.sqlite3"
    assert args.cas_root == "/state/cas"
    assert args.workspace_root == "/state/workspaces"
    assert args.repository == "owner/repo"
    assert args.namespace == "production"
    assert args.ref == "release"
    assert args.expected_generation == 7
    assert defaults.publish_retained is False
    assert defaults.catalog is None
    assert defaults.cas_root is None
    assert defaults.workspace_root is None
    assert defaults.repository is None
    assert defaults.namespace is None
    assert defaults.ref is None
    assert defaults.expected_generation is None


@pytest.mark.parametrize(
    "argv",
    [
        ["index", ".", "--catalog", "/state/catalog.sqlite3"],
        ["index", ".", "--publish-retained", "--catalog", "/state/catalog.sqlite3"],
    ],
)
def test_index_retained_arguments_are_all_or_none_before_repository_work(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(argv)
    monkeypatch.setattr(
        cli,
        "resolve_repo_path",
        lambda _value: pytest.fail("retained option validation must run first"),
    )

    with pytest.raises(cli.CLIError, match="--publish-retained"):
        cli._run_index(args)


def test_publish_repository_identity_does_not_enable_retained_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib import artifacts as artifacts_module
    from codenib.compiler import manifest as manifest_module
    from codenib.web import static_export as static_export_module

    repo = tmp_path / "repo"
    repo.mkdir()
    args = cli.build_parser().parse_args(
        [
            "publish",
            str(repo),
            "--preset",
            "fast",
            "--repository",
            "owner/repo",
        ]
    )
    selection = RepositorySourceSelection()
    manifest = SimpleNamespace(
        repo_path=str(repo),
        languages=["python"],
        source_selection=selection,
        indexes={"bm25": SimpleNamespace(status="fresh", metadata={})},
        commit="a" * 40,
    )
    indexed: list[dict[str, object]] = []

    monkeypatch.setattr(cli, "resolve_repo_path", lambda _value: repo)
    monkeypatch.setattr(
        cli, "_validate_publish_outputs", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        cli,
        "_resolve_repository_source_selection",
        lambda *_args, **_kwargs: SimpleNamespace(selection=selection),
    )
    monkeypatch.setattr(
        cli,
        "_require_clean_artifact_checkout",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(cli, "_selected_views_for_args", lambda _args: ["bm25"])
    monkeypatch.setattr(
        cli, "_selected_languages", lambda *_args, **_kwargs: ["python"]
    )
    monkeypatch.setattr(cli, "_check_view_dependencies", lambda *_args, **_kwargs: None)

    def index_repository(_repo: Path, **kwargs: object):
        indexed.append(kwargs)
        return manifest, []

    monkeypatch.setattr(cli, "index_repository", index_repository)
    monkeypatch.setattr(
        cli,
        "resolve_manifest_path",
        lambda _value: tmp_path / "repo_manifest.json",
    )

    class FakeRepoManifest:
        @staticmethod
        def load(_path: Path) -> object:
            return manifest

    monkeypatch.setattr(manifest_module, "RepoManifest", FakeRepoManifest)
    monkeypatch.setattr(
        static_export_module,
        "export_static_wiki",
        lambda *_args, **_kwargs: SimpleNamespace(output_dir=tmp_path / "wiki"),
    )
    monkeypatch.setattr(
        artifacts_module,
        "stage_context_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(
            output_dir=tmp_path / "context",
            repository="owner/repo",
            commit=manifest.commit,
            views=("bm25",),
        ),
    )
    monkeypatch.setattr(cli, "_publication_environment", lambda *_args: {})

    assert args.handler is cli._run_publish
    assert args.handler(args) == 0
    assert len(indexed) == 1
    assert indexed[0]["views"] == ["bm25"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--rebuild"],
        ["--view", "symbol_graph"],
        ["--view", "zoekt"],
    ],
)
def test_index_retained_rejects_unsupported_build_modes_before_repository_work(
    extra: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(
        [
            "index",
            ".",
            "--publish-retained",
            "--catalog",
            "/state/catalog.sqlite3",
            "--cas-root",
            "/state/cas",
            "--workspace-root",
            "/state/workspaces",
            "--repository",
            "owner/repo",
            *extra,
        ]
    )
    monkeypatch.setattr(
        cli,
        "resolve_repo_path",
        lambda _value: pytest.fail("unsupported retained mode must fail first"),
    )

    with pytest.raises(cli.CLIError, match="not supported|supports only"):
        cli._run_index(args)


def test_source_selection_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(
            [
                "codegraph",
                "init",
                ".",
                "--exclude-dir",
                "ios",
                "--clear-exclude-dirs",
            ]
        )

    assert exc_info.value.code == 2


def test_source_selection_reuses_the_persisted_manifest_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
    from codenib.paths import repo_index_dir

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    selection = RepositorySourceSelection(("generated/api", "ios/Pods"))
    RepoManifest(repo_path=str(repo), source_selection=selection).save(
        repo_index_dir(repo) / MANIFEST_FILENAME
    )
    args = cli.build_parser().parse_args(["index", str(repo), "--preset", "fast"])

    resolved = cli._resolve_repository_source_selection(repo, args)

    assert resolved.action == "reuse"
    assert resolved.selection == selection
    assert resolved.selection is not selection


def test_source_selection_migrates_legacy_manifest_without_aliasing_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import (
        LEGACY_MANIFEST_VERSION,
        MANIFEST_FILENAME,
        RepoManifest,
    )
    from codenib.paths import repo_index_dir

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    RepoManifest(
        version=LEGACY_MANIFEST_VERSION,
        repo_path=str(repo),
        source_selection=None,
    ).save(repo_index_dir(repo) / MANIFEST_FILENAME)
    args = cli.build_parser().parse_args(["index", str(repo), "--preset", "fast"])

    resolved = cli._resolve_repository_source_selection(repo, args)

    assert resolved.action == "migrate"
    assert resolved.selection == RepositorySourceSelection()


def test_source_selection_rejects_noncanonical_cli_paths(tmp_path: Path) -> None:
    args = cli.build_parser().parse_args(
        ["index", str(tmp_path), "--exclude-dir", "ios/../secrets"]
    )

    with pytest.raises(cli.CLIError, match="invalid --exclude-dir"):
        cli._resolve_repository_source_selection(tmp_path, args)


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
            "--exclude-dir",
            "ios/Pods",
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
    assert publish.exclude_dir == ["ios/Pods"]
    assert publish.clear_exclude_dirs is False
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
            "--view",
            "vector,bm25",
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
    assert args.view == ["vector,bm25"]
    assert defaults.namespace == "default"
    assert defaults.ref == "main"
    assert defaults.expected_generation == 0
    assert defaults.view == []
    assert cli._compiler_cache_import_views(defaults.view) == ("bm25",)
    assert cli._compiler_cache_import_views(args.view) == ("bm25", "vector")
    with pytest.raises(cli.CLIError, match="unsupported compiler cache view"):
        cli._compiler_cache_import_views(["graph"])
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


def test_jobs_run_once_parser_exposes_bounded_worker_inputs() -> None:
    base = [
        "jobs",
        "run-once",
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
    defaults = cli.build_parser().parse_args(base)
    configured = cli.build_parser().parse_args(
        [
            *base,
            "--namespace",
            "production",
            "--lease-duration-ms",
            "60000",
            "--heartbeat-interval-ms",
            "10000",
            "--scan-limit",
            "128",
            "--json",
        ]
    )

    assert defaults.handler is cli._run_jobs_run_once
    assert defaults.jobs_command == "run-once"
    assert defaults.namespace == "default"
    assert defaults.lease_duration_ms == 30_000
    assert defaults.heartbeat_interval_ms == 5_000
    assert defaults.scan_limit == 64
    assert defaults.json is False
    assert defaults.cache_dir == "/src/repo/.codenib_index"
    assert defaults.source_bm25 is False
    assert defaults.reclaim_quiescent_attempts is False
    assert defaults.language == []
    assert defaults.exclude_dir is None
    assert defaults.clear_exclude_dirs is False
    assert configured.namespace == "production"
    assert configured.lease_duration_ms == 60_000
    assert configured.heartbeat_interval_ms == 10_000
    assert configured.scan_limit == 128
    assert configured.json is True

    with pytest.raises(SystemExit) as invalid:
        cli.build_parser().parse_args([*base, "--scan-limit", "0"])
    assert invalid.value.code == 2
def test_jobs_worker_parser_requires_one_explicit_input_mode() -> None:
    topology = [
        "--catalog",
        "/state/catalog.sqlite3",
        "--cas-root",
        "/state/cas",
        "--workspace-root",
        "/state/workspaces",
        "--repository",
        "owner/repo",
    ]
    parser = cli.build_parser()

    source = parser.parse_args(
        [
            "jobs",
            "run-once",
            "/src/repo",
            "--source-bm25",
            "--reclaim-quiescent-attempts",
            "--language",
            "python,go",
            "--exclude-dir",
            "generated",
            *topology,
        ]
    )

    assert source.cache_dir is None
    assert source.source_bm25 is True
    assert source.reclaim_quiescent_attempts is True
    assert source.language == ["python,go"]
    assert source.exclude_dir == ["generated"]
    assert source.clear_exclude_dirs is False

    with pytest.raises(SystemExit) as missing:
        parser.parse_args(["jobs", "run-once", "/src/repo", *topology])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as mixed:
        parser.parse_args(
            [
                "jobs",
                "run-once",
                "/src/repo",
                "--cache-dir",
                "/src/repo/.codenib_index",
                "--source-bm25",
                *topology,
            ]
        )
    assert mixed.value.code == 2


@pytest.mark.parametrize(
    "source_option",
    (
        ("language", ["python"]),
        ("exclude_dir", ["generated"]),
        ("clear_exclude_dirs", True),
        ("reclaim_quiescent_attempts", True),
    ),
)
def test_cache_job_worker_rejects_source_only_options(source_option) -> None:
    name, value = source_option
    args = SimpleNamespace(
        source_bm25=False,
        language=[],
        exclude_dir=None,
        clear_exclude_dirs=False,
    )
    setattr(args, name, value)

    with pytest.raises(cli.CLIError, match="requires? --source-bm25"):
        cli._run_with_local_index_job_worker(
            args,
            lambda _worker: pytest.fail("worker must not be created"),
        )


def test_source_job_worker_rejects_programmatic_cache_input() -> None:
    args = SimpleNamespace(
        source_bm25=True,
        cache_dir="/state/compiler-cache",
    )

    with pytest.raises(cli.CLIError, match="cannot be combined"):
        cli._run_with_local_index_job_worker(
            args,
            lambda _worker: pytest.fail("worker must not be created"),
        )


def test_jobs_worker_rejects_nonboolean_reclamation_state() -> None:
    args = SimpleNamespace(
        source_bm25=True,
        reclaim_quiescent_attempts=1,
        cache_dir=None,
    )

    with pytest.raises(cli.CLIError, match="state must be an exact boolean"):
        cli._run_with_local_index_job_worker(
            args,
            lambda _worker: pytest.fail("worker must not be created"),
        )


@pytest.mark.parametrize("reclaim", (False, True))
def test_bm25_source_operation_reclaims_once_only_after_successful_return(
    monkeypatch: pytest.MonkeyPatch,
    reclaim: bool,
) -> None:
    events: list[str] = []
    result = object()

    def operation(worker: object) -> object:
        assert worker == "worker"
        events.extend(("page-one", "page-two", "operation-return"))
        return result

    monkeypatch.setattr(
        cli,
        "_reclaim_local_bm25_source_attempt_pool",
        lambda target, route: events.append(f"reclaim:{target}:{route}"),
    )

    observed = cli._run_local_bm25_source_worker_operation(
        SimpleNamespace(reclaim_quiescent_attempts=reclaim),
        operation,
        "worker",
        "target",
        "route",
        lambda: events.append("verify"),
    )

    assert observed is result
    assert events == [
        "page-one",
        "page-two",
        "operation-return",
        "verify",
        *(("reclaim:target:route", "verify") if reclaim else ()),
    ]


def test_bm25_source_operation_does_not_reclaim_exceptional_unwind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("worker failed")
    events: list[str] = []

    def fail(_worker: object) -> None:
        events.append("operation")
        raise failure

    monkeypatch.setattr(
        cli,
        "_reclaim_local_bm25_source_attempt_pool",
        lambda _target, _route: pytest.fail("exceptional worker must not reclaim"),
    )

    with pytest.raises(RuntimeError) as caught:
        cli._run_local_bm25_source_worker_operation(
            SimpleNamespace(reclaim_quiescent_attempts=True),
            fail,
            object(),
            object(),
            object(),
            lambda: events.append("verify"),
        )

    assert caught.value is failure
    assert events == ["operation"]


def test_bm25_attempt_pool_summary_is_bounded_stderr_only(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.compiler.job_resources as job_resources_module

    target = object()
    reaper_route = object()

    class Coordinator:
        def __init__(
            self,
            candidate: object,
            *,
            reaper_route=None,
            _legacy_workspace: bool = False,
        ) -> None:
            assert candidate is target
            self.reaper_route = reaper_route
            self.legacy_workspace = _legacy_workspace

        def reclaim(self, *, caller_asserts_quiescence: bool):
            assert caller_asserts_quiescence is True
            if self.reaper_route is None:
                assert self.legacy_workspace is True
                return job_resources_module.BM25AttemptPoolReclamation(
                    scanned_children=3,
                    reclaimed_children=1,
                    current_children=0,
                    legacy_children=1,
                    discarded_children=1,
                    retained_unrelated_children=2,
                )
            assert self.reaper_route is reaper_route
            assert self.legacy_workspace is False
            return job_resources_module.BM25AttemptPoolReclamation(
                scanned_children=7,
                reclaimed_children=5,
                current_children=3,
                legacy_children=2,
                discarded_children=4,
                retained_unrelated_children=2,
            )

    class ReaperRoute:
        def _verify(self) -> None:
            return None

    reaper_route = ReaperRoute()

    monkeypatch.setattr(
        job_resources_module,
        "LocalBM25AttemptPoolCoordinator",
        Coordinator,
    )

    cli._reclaim_local_bm25_source_attempt_pool(target, reaper_route)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "BM25 attempt-pool reclamation: scanned=9 reclaimed=6 "
        "current=3 legacy=3 discarded=5 retained=3\n"
    )


def test_source_job_topology_loss_stops_continuous_scheduler_before_claim() -> None:
    considered: list[object] = []
    page_calls: list[tuple[object | None, object]] = []
    waits: list[float | None] = []

    def topology_lost() -> None:
        raise cli.CLIError("workspace identity changed")

    guard = cli._bm25_source_job_infrastructure_guard(topology_lost)
    candidate_filter = cli._index_job_candidate_filter_with_guard(
        guard,
        lambda candidate: considered.append(candidate) or True,
    )

    class GuardedWorker:
        def begin_cycle(self):
            return storage_module.IndexJobRunnableCycle(1)

        def run_page(self, *, cursor=None, cycle):
            page_calls.append((cursor, cycle))
            candidate_filter(object())
            pytest.fail("topology loss must terminate the scheduler page")

    class NeverStop:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout)
            return False

    scheduler = storage_module.IndexJobWorkerScheduler(worker=GuardedWorker())

    with pytest.raises(
        storage_module.StorageIntegrityError,
        match="retained topology changed",
    ) as caught:
        scheduler.run(NeverStop())

    assert isinstance(caught.value.__cause__, cli.CLIError)
    assert page_calls == [(None, storage_module.IndexJobRunnableCycle(1))]
    assert considered == []
    assert waits == []


def test_source_job_worker_guard_preserves_healthy_candidate_filter() -> None:
    verified: list[bool] = []
    candidate = object()
    guard = cli._bm25_source_job_infrastructure_guard(lambda: verified.append(True))
    candidate_filter = cli._index_job_candidate_filter_with_guard(
        guard,
        lambda value: value is candidate,
    )

    assert candidate_filter(candidate) is True
    assert verified == [True]


def test_source_job_worker_pins_repository_before_provider_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = tmp_path / "candidate-repository"
    candidate.mkdir()
    (candidate / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    displaced = tmp_path / "displaced-repository"
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    args = SimpleNamespace(
        repo=os.fspath(repository),
        source_bm25=True,
        cache_dir=None,
        language=["python"],
        exclude_dir=None,
        clear_exclude_dirs=False,
        catalog=os.fspath(catalog),
        cas_root=os.fspath(cas_root),
        workspace_root=os.fspath(workspace_root),
        repository="owner/repo",
        namespace="default",
    )
    authorities: list[object] = []
    real_pin = source_fingerprint_module.pin_repository_source_root

    def capture_pin(*values: object, **kwargs: object):
        authority = real_pin(*values, **kwargs)
        authorities.append(authority)
        return authority

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            repository.rename(displaced)
            candidate.rename(repository)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("replaced repository must fail before storage or worker setup")

    monkeypatch.setattr(
        source_fingerprint_module,
        "pin_repository_source_root",
        capture_pin,
    )
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        unexpected,
    )
    monkeypatch.setattr(storage_module, "LocalCAS", unexpected)

    with pytest.raises(cli.CLIError, match="repository root"):
        cli._run_with_local_index_job_worker(
            args,
            lambda _worker: pytest.fail("worker operation must not run"),
        )

    assert len(authorities) == 1
    assert authorities[0].closed  # type: ignore[attr-defined]


def test_jobs_run_parser_exposes_continuous_scheduler_inputs() -> None:
    base = [
        "jobs",
        "run",
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
    defaults = cli.build_parser().parse_args(base)
    configured = cli.build_parser().parse_args(
        [
            *base,
            "--initial-idle-delay-ms",
            "100",
            "--max-idle-delay-ms",
            "2000",
            "--max-cycles",
            "3",
            "--scan-limit",
            "128",
            "--json",
        ]
    )

    assert defaults.handler is cli._run_jobs_continuous
    assert defaults.jobs_command == "run"
    assert defaults.initial_idle_delay_ms == 250
    assert defaults.max_idle_delay_ms == 5_000
    assert defaults.max_cycles is None
    assert defaults.scan_limit == 64
    assert defaults.json is False
    assert defaults.reclaim_quiescent_attempts is False
    assert configured.initial_idle_delay_ms == 100
    assert configured.max_idle_delay_ms == 2_000
    assert configured.max_cycles == 3
    assert configured.scan_limit == 128
    assert configured.json is True

    with pytest.raises(SystemExit) as invalid:
        cli.build_parser().parse_args([*base, "--max-cycles", "0"])
    assert invalid.value.code == 2


def test_jobs_scheduler_sigterm_handler_requests_stop_and_restores() -> None:
    previous = signal.getsignal(signal.SIGTERM)

    with cli._jobs_scheduler_stop_signal() as stop_signal:
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)
        installed(signal.SIGTERM, None)
        assert stop_signal.is_set()

    assert signal.getsignal(signal.SIGTERM) is previous


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
    assert paths.view_destinations == (("bm25", workspace_root / f"{prefix}-bm25"),)
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
        view=[],
    )


def _retained_index_cli_test_args(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("VALUE = 1\n")
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    return SimpleNamespace(
        repo=str(repo),
        preset="fast",
        language=[],
        view=[],
        rebuild=False,
        exclude_dir=None,
        clear_exclude_dirs=False,
        publish_retained=True,
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        repository="owner/repo",
        namespace=None,
        ref=None,
        expected_generation=None,
    )


def test_index_retained_uses_one_core_route_and_ordered_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codenib.paths import repo_index_dir

    args = _retained_index_cli_test_args(tmp_path)
    repo = Path(args.repo)
    catalog_path = Path(args.catalog).resolve(strict=True)
    cas_root = Path(args.cas_root).resolve(strict=True)
    workspace_root = Path(args.workspace_root).resolve(strict=True)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    canonical_cache = Path(os.path.abspath(os.fspath(repo_index_dir(repo))))
    canonical_cache.parent.mkdir(parents=True)
    nonce = "1" * 32
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: nonce)
    bm25_destination = workspace_root / f".codenib-cache-import-{nonce}-bm25"
    vector_destination = workspace_root / f".codenib-cache-import-{nonce}-vector"
    context_destination = workspace_root / f".codenib-cache-import-{nonce}-context"
    suggested = workspace_root / f".codenib-cache-import-{nonce}-materialized"
    events: list[str] = []
    topology_modes: list[bool] = []

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
    vector_owner = Closeable("vector")
    context_owner = Closeable("context")
    source = Closeable("source")
    store = Closeable("cas")
    catalog = Closeable("catalog")
    receipt_owners = iter((bm25_owner, vector_owner, context_owner))
    catalog_identity = cli._retained_catalog_file_identity(catalog_path)
    storage_topology = cli._CompilerRetainedStorageTopology(
        repo_path=repo.resolve(strict=True),
        repository_identity=cli._retained_real_directory_identity(
            repo.resolve(strict=True),
            label="repository",
        ),
        planned_cache_path=canonical_cache,
        cache_parent_path=canonical_cache.parent,
        cache_parent_identity=cli._retained_real_directory_identity(
            canonical_cache.parent,
            label="cache parent",
        ),
        catalog_path=catalog_path,
        catalog_identity=catalog_identity,
        cas_root=cas_root,
        cas_identity=(1, 20),
        workspace_root=workspace_root,
        workspace_identity=(1, 30),
    )
    cache_topology = cli._CompilerCacheImportTopology(
        cache_dir=canonical_cache,
        cache_identity=(1, 10),
        catalog_path=catalog_path,
        catalog_identity=catalog_identity,
        cas_root=cas_root,
        cas_identity=(1, 20),
        workspace_root=workspace_root,
        workspace_identity=(1, 30),
    )

    def capture(
        root: Path,
        *,
        exclude_roots: tuple[Path, ...],
        selection: RepositorySourceSelection,
        _source_owner,
    ) -> Closeable:
        events.append("capture")
        assert root == repo
        assert canonical_cache in exclude_roots
        assert selection == RepositorySourceSelection()
        _source_owner(source)
        return source

    def prepare(root: Path, **kwargs: object) -> tuple[object, Path]:
        events.append("prepare")
        assert root == repo
        assert kwargs["views"] == ["bm25", "vector"]
        return object(), canonical_cache

    def observe_topology(
        _paths: object,
        *,
        allow_published_outputs: bool = False,
    ) -> object:
        topology_modes.append(allow_published_outputs)
        return cache_topology

    def compile_and_import(
        _compiler: object,
        root: Path,
        *,
        cache_dir: Path,
        cache_topology_guard: object,
        **kwargs: object,
    ) -> SimpleNamespace:
        events.append("core")
        assert root == repo
        assert cache_dir == canonical_cache
        assert kwargs["views"] == ("bm25", "vector")
        assert kwargs["repository_source"] is source
        assert kwargs["view_output_owners"] == {
            "bm25": bm25_owner,
            "vector": vector_owner,
        }
        assert kwargs["context_output_owner"] is context_owner
        assert kwargs["repository_key"] == "owner/repo"
        assert kwargs["namespace_name"] == "default"
        assert kwargs["ref_name"] == "main"
        assert kwargs["expected_generation"] == 0
        canonical_cache.mkdir()
        cache_topology_guard.capture(canonical_cache)
        cache_topology_guard.verify(canonical_cache)
        bm25_destination.mkdir()
        vector_destination.mkdir()
        context_destination.mkdir()
        cache_topology_guard.verify(canonical_cache)
        entry = SimpleNamespace(status="fresh", metadata={})
        return SimpleNamespace(
            manifest=SimpleNamespace(
                repo_path=str(repo),
                languages=["python"],
                source_selection=RepositorySourceSelection(),
                indexes={"bm25": entry, "vector": entry},
            ),
            retained_import=SimpleNamespace(
                import_result=SimpleNamespace(
                    snapshot_id="snapshot-id",
                    ref_name="main",
                    generation=1,
                )
            ),
        )

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_require_compiler_retained_storage_topology",
        lambda _paths: storage_topology,
    )
    monkeypatch.setattr(
        cli,
        "_require_compiler_cache_import_topology",
        observe_topology,
    )
    monkeypatch.setattr(
        cli,
        "_new_retained_output_receipt_owner",
        lambda: next(receipt_owners),
    )
    monkeypatch.setattr(source_fingerprint_module, "capture_repository_source", capture)
    monkeypatch.setattr(cli, "_prepare_index_compiler", prepare)
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: events.append("cas-open") or store,
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: events.append("catalog-open") or catalog,
    )
    monkeypatch.setattr(
        cache_import_module,
        "compile_and_import_repo",
        compile_and_import,
    )

    result = cli._run_index_retained(
        args,
        repo_path=repo,
        selection=cli._RetainedIndexSelection("owner/repo", "default", "main", 0),
        compiler_kwargs={
            "languages": ["python"],
            "views": ["bm25", "vector"],
            "source_selection": RepositorySourceSelection(),
        },
    )

    assert result == 0
    assert topology_modes == [False, False, True]
    assert events.index("cas-open") < events.index("catalog-open")
    assert events.index("catalog-open") < events.index("capture")
    assert events.index("capture") < events.index("prepare") < events.index("core")
    assert events[-6:] == [
        "catalog-close",
        "cas-close",
        "context-close",
        "vector-close",
        "bm25-close",
        "source-close",
    ]
    assert not suggested.exists()
    output = capsys.readouterr().out
    assert "Retained Snapshot:   snapshot-id" in output
    assert "Retained Ref:        main" in output
    assert "Retained Generation: 1" in output
    assert "Retained Views:      bm25,vector" in output
    assert f"BM25 evidence:       {bm25_destination}" in output
    assert f"Vector evidence:     {vector_destination}" in output
    assert f"Context evidence:    {context_destination}" in output
    assert "Materialize snapshot: codenib artifact materialize" in output
    assert "--snapshot snapshot-id" in output


def test_index_retained_rejects_missing_cache_parent_symlink_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.paths import repo_index_dir

    args = _retained_index_cli_test_args(tmp_path)
    repo = Path(args.repo)
    cas_root = Path(args.cas_root)
    aliased_home = tmp_path / "aliased-state"
    aliased_home.symlink_to(cas_root, target_is_directory=True)
    monkeypatch.setenv("CODENIB_HOME", str(aliased_home))
    canonical_cache = Path(os.path.abspath(os.fspath(repo_index_dir(repo))))
    assert not canonical_cache.exists()
    before = tuple(cas_root.iterdir())
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: "2" * 32)
    monkeypatch.setattr(
        cli,
        "_prepare_index_compiler",
        lambda *_args, **_kwargs: pytest.fail("compiler must not be prepared"),
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

    with pytest.raises(
        cli.CLIError,
        match="cache path must contain only existing real directories",
    ):
        cli._run_index_retained(
            args,
            repo_path=repo,
            selection=cli._RetainedIndexSelection(
                "owner/repo",
                "default",
                "main",
                0,
            ),
            compiler_kwargs={
                "languages": ["python"],
                "views": ["bm25"],
                "source_selection": RepositorySourceSelection(),
            },
        )

    assert tuple(cas_root.iterdir()) == before
    assert not canonical_cache.exists()


def test_artifact_import_cache_retains_failed_topology_cleanup_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _cache_import_cli_test_args(tmp_path)
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: "f" * 32)
    created: list[object] = []

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    class RetryableTopology:
        def __init__(self, output: Path) -> None:
            self.catalog_path = Path(args.catalog).resolve(strict=True)
            self.catalog_identity = cli._retained_catalog_file_identity(
                self.catalog_path
            )
            self.cas_root = Path(args.cas_root).resolve(strict=True)
            self.workspace_root = Path(args.workspace_root).resolve(strict=True)
            self.output = output
            self.cas_binding = SimpleNamespace(identity=(1, 11))
            self.workspace_binding = SimpleNamespace(identity=(1, 12))
            self.output_parent_binding = SimpleNamespace(identity=(1, 12))
            self.allow_close = False
            self.closed = False

        def verify(self) -> None:
            pass

        def close(self) -> None:
            if not self.allow_close:
                raise OSError("injected topology close failure")
            self.closed = True

    def topology_factory(
        _catalog: Path,
        _cas: Path,
        _workspace: Path,
        output: Path,
    ) -> RetryableTopology:
        topology = RetryableTopology(output)
        created.append(topology)
        return topology

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        topology_factory,
    )

    with pytest.raises(cli.CLIError, match="topology close failure") as caught:
        cli._run_artifact_import_cache(args)

    assert len(created) == 1
    topology = created[0]
    retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
    assert type(retained) is tuple
    assert len(retained) == 1
    assert not retained[0].closed
    topology.allow_close = True  # type: ignore[attr-defined]
    retained[0].close()
    assert retained[0].closed
    assert topology.closed  # type: ignore[attr-defined]


def test_artifact_import_cache_uses_frozen_authorities_and_closes_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _cache_import_cli_test_args(tmp_path)
    args.view = ["vector,bm25"]
    nonce = "b" * 32
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: nonce)
    workspace_root = Path(args.workspace_root)
    cache = Path(args.cache_dir)
    catalog_path = Path(args.catalog)
    cas_root = Path(args.cas_root)
    bm25_destination = workspace_root / f".codenib-cache-import-{nonce}-bm25"
    vector_destination = workspace_root / f".codenib-cache-import-{nonce}-vector"
    context_destination = workspace_root / f".codenib-cache-import-{nonce}-context"
    suggested = workspace_root / f".codenib-cache-import-{nonce}-materialized"
    events: list[str] = []
    bridge_calls: list[tuple[Path, dict[str, object]]] = []
    source_selection = RepositorySourceSelection(("generated",))
    capture_calls: list[tuple[Path, tuple[Path, ...], RepositorySourceSelection]] = []

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
    vector_owner = Closeable("vector")
    context_owner = Closeable("context")
    source = Closeable("source")
    store = Closeable("cas")
    catalog = Closeable("catalog")
    receipt_owners = iter((bm25_owner, vector_owner, context_owner))
    catalog_identity = cli._retained_catalog_file_identity(catalog_path)

    def capture(
        root: Path,
        *,
        exclude_roots: tuple[Path, ...],
        selection: RepositorySourceSelection,
        _source_owner,
    ) -> Closeable:
        events.append("capture")
        capture_calls.append((root, exclude_roots, selection))
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
        vector_destination.mkdir()
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
        "import_compiler_cache",
        import_cache,
    )
    monkeypatch.setattr(
        cache_import_module,
        "compiler_cache_source_selection",
        lambda _cache: source_selection,
    )
    real_print = print

    def print_after_cleanup(*values: object, **kwargs: object) -> None:
        assert bm25_owner.closed
        assert vector_owner.closed
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
            (
                cache,
                bm25_destination,
                vector_destination,
                context_destination,
                suggested,
            ),
            source_selection,
        )
    ]
    assert len(bridge_calls) == 1
    passed_cache, passed = bridge_calls[0]
    assert passed_cache == cache.resolve(strict=True)
    assert passed["views"] == ("bm25", "vector")
    assert passed["repository_source"] is source
    assert passed["view_output_owners"] == {
        "bm25": bm25_owner,
        "vector": vector_owner,
    }
    assert passed["context_output_owner"] is context_owner
    assert passed["view_destinations"] == {
        "bm25": bm25_destination,
        "vector": vector_destination,
    }
    assert passed["context_destination"] == context_destination
    assert passed["repository_key"] == "owner/repo"
    assert passed["namespace_name"] == "default"
    assert passed["ref_name"] == "main"
    assert passed["expected_generation"] == 0
    forbidden = passed["forbidden_paths"]
    assert bm25_destination in forbidden
    assert vector_destination in forbidden
    assert context_destination in forbidden
    assert events.index("provider-support") < events.index("capture")
    assert events.index("capture") < events.index("cas-open")
    assert events.index("cas-open") < events.index("catalog-open")
    assert events.index("catalog-open") < events.index("bridge")
    assert events[-6:] == [
        "catalog-close",
        "cas-close",
        "context-close",
        "vector-close",
        "bm25-close",
        "source-close",
    ]
    assert bm25_destination.is_dir()
    assert vector_destination.is_dir()
    assert context_destination.is_dir()
    assert not suggested.exists()
    output = capsys.readouterr().out
    assert "Snapshot:           snapshot-id" in output
    assert "Ref:                main" in output
    assert "Generation:         1" in output
    assert "Views:              bm25,vector" in output
    assert f"BM25 generation:    {bm25_destination}" in output
    assert f"Vector generation:  {vector_destination}" in output
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
    args.view = ["bm25,vector"]
    nonce = "c" * 32
    monkeypatch.setattr(cli, "_new_compiler_cache_import_nonce", lambda: nonce)
    workspace_root = Path(args.workspace_root)
    bm25_destination = workspace_root / f".codenib-cache-import-{nonce}-bm25"
    vector_destination = workspace_root / f".codenib-cache-import-{nonce}-vector"
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
    vector_owner = RetryableCloseable()
    context_owner = RetryableCloseable()
    source = RetryableCloseable()
    store = RetryableCloseable()
    catalog = RetryableCloseable()
    receipt_owners = iter((bm25_owner, vector_owner, context_owner))

    def capture(
        *_args: object,
        _source_owner,
        **_kwargs: object,
    ) -> RetryableCloseable:
        _source_owner(source)
        return source

    def fail_after_publication(*_args: object, **_kwargs: object) -> None:
        bm25_destination.mkdir()
        vector_destination.mkdir()
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
        "import_compiler_cache",
        fail_after_publication,
    )
    monkeypatch.setattr(
        cache_import_module,
        "compiler_cache_source_selection",
        lambda _cache: RepositorySourceSelection(),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        cli._run_artifact_import_cache(args)

    assert raised.value is primary
    assert not bm25_owner.closed
    assert not vector_owner.closed
    assert not context_owner.closed
    assert not source.closed
    assert not store.closed
    assert not catalog.closed
    assert bm25_owner.close_calls == 1
    assert vector_owner.close_calls == 1
    assert context_owner.close_calls == 1
    assert source.close_calls == 1
    assert store.close_calls == 1
    assert catalog.close_calls == 1
    notes = _cleanup_notes(primary)
    assert any("SQLite catalog cleanup" in note for note in notes)
    assert any("local CAS cleanup" in note for note in notes)
    assert any("context receipt cleanup" in note for note in notes)
    assert any("BM25 receipt cleanup" in note for note in notes)
    assert any("Vector receipt cleanup" in note for note in notes)
    assert any("repository source cleanup" in note for note in notes)
    assert bm25_destination.is_dir()
    assert vector_destination.is_dir()
    assert context_destination.is_dir()
    warnings = capsys.readouterr().err
    assert "cache import BM25 generation now exists" in warnings
    assert "cache import Vector generation now exists" in warnings
    assert "cache import context generation now exists" in warnings

    retained = primary.publication_cleanup_owners  # type: ignore[attr-defined]
    assert type(retained) is tuple
    assert len(retained) == 6
    for resource in (
        bm25_owner,
        vector_owner,
        context_owner,
        source,
        store,
        catalog,
    ):
        resource.allow_close = True
    for cleanup_owner in retained:
        cleanup_owner.close()
        assert cleanup_owner.closed
    assert bm25_owner.closed
    assert vector_owner.closed
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


def test_retained_materialization_topology_keeps_independent_authorities(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    output_parent = workspace_root / "nested"
    output_parent.mkdir()

    topology = cli._require_retained_materialization_topology(
        catalog,
        cas_root,
        workspace_root,
        output_parent / "context",
    )
    try:
        topology.verify()
        assert topology.cas_binding.identity != topology.workspace_binding.identity
        assert len(topology.cas_binding.identity) > 2
        assert not topology.closed
    finally:
        topology.close()

    assert topology.closed


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict mount-table authentication is Linux-specific",
)
@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ("", "mount table is empty"),
        (
            "1 0 0:1 / / rw shared:1 ext4 /dev/root rw\n",
            "malformed entry",
        ),
    ),
)
def test_retained_linux_mount_mappings_fail_closed_on_untrusted_table(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.BytesIO(payload.encode()),
    )

    with pytest.raises(cli.CLIError, match=message):
        cli._retained_linux_mount_mappings()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict mount-table authentication is Linux-specific",
)
def test_retained_linux_mount_mappings_accept_non_utf8_filesystem_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_point = b"/mnt/non-utf8-\xff"
    payload = b"1 0 8:1 / " + mount_point + b" rw - ext4 /dev/root rw\n"
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.BytesIO(payload),
    )

    mappings = cli._retained_linux_mount_mappings()

    assert len(mappings) == 1
    assert os.fsencode(mappings[0].mount_point) == mount_point


def test_retained_linux_physical_path_rejects_stacked_mount_mapping() -> None:
    mount_point = Path("/workspace")
    mappings = (
        cli._RetainedLinuxMountMapping(
            mount_id=20,
            device="8:1",
            root=PurePosixPath("/first"),
            mount_point=mount_point,
        ),
        cli._RetainedLinuxMountMapping(
            mount_id=10,
            device="8:1",
            root=PurePosixPath("/second"),
            mount_point=mount_point,
        ),
    )

    with pytest.raises(cli.CLIError, match="ambiguous stacked mapping"):
        cli._retained_linux_physical_path(mount_point / "child", mappings)


def test_retained_materialization_topology_partial_open_closes_all_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._atomic_directory as atomic_directory

    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    owners: list[object] = []
    open_calls = 0
    original_owner = atomic_directory._PublicationAuthorityOwner
    original_open = atomic_directory._open_publication_authority

    class CapturingOwner(original_owner):
        def __init__(self) -> None:
            super().__init__()
            owners.append(self)

    def fail_second_open(*args: object, **kwargs: object):
        nonlocal open_calls
        open_calls += 1
        if open_calls == 2:
            raise OSError("second authority open failed")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(
        atomic_directory,
        "_PublicationAuthorityOwner",
        CapturingOwner,
    )
    monkeypatch.setattr(
        atomic_directory,
        "_open_publication_authority",
        fail_second_open,
    )

    with pytest.raises(OSError, match="second authority open failed"):
        cli._require_retained_materialization_topology(
            catalog,
            cas_root,
            workspace_root,
            workspace_root / "context",
        )

    assert open_calls == 2
    assert len(owners) == 3
    assert all(owner.closed for owner in owners)  # type: ignore[attr-defined]


def test_retained_materialization_topology_retains_partial_open_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._atomic_directory as atomic_directory

    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    owners: list[object] = []
    open_calls = 0
    original_owner = atomic_directory._PublicationAuthorityOwner
    original_open = atomic_directory._open_publication_authority

    class RetryableOwner(original_owner):
        allow_close = False

        def __init__(self) -> None:
            super().__init__()
            owners.append(self)

        def close(self, *args: object, **kwargs: object) -> None:
            if self.authority is not None and not self.allow_close:
                raise OSError("injected authority close failure")
            super().close(*args, **kwargs)

    def fail_second_open(*args: object, **kwargs: object):
        nonlocal open_calls
        open_calls += 1
        if open_calls == 2:
            raise OSError("second authority open failed")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(atomic_directory, "_PublicationAuthorityOwner", RetryableOwner)
    monkeypatch.setattr(
        atomic_directory, "_open_publication_authority", fail_second_open
    )

    with pytest.raises(OSError, match="second authority open failed") as caught:
        cli._require_retained_materialization_topology(
            catalog,
            cas_root,
            workspace_root,
            workspace_root / "context",
        )

    retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
    assert retained == (owners[0],)
    owners[0].allow_close = True  # type: ignore[attr-defined]
    retained[0].close()
    assert retained[0].closed


def test_retained_materialization_topology_close_retries_every_authority(
    tmp_path: Path,
) -> None:
    class RetryableOwner:
        def __init__(self, label: str) -> None:
            self.label = label
            self.closed = False
            self.allow_close = False
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if not self.allow_close:
                raise OSError(f"{self.label} authority close failed")
            self.closed = True

    owners = tuple(RetryableOwner(label) for label in ("CAS", "workspace", "output"))
    bindings = tuple(
        cli._RetainedDirectoryBinding(
            label=owner.label,
            path=tmp_path / owner.label,
            identity=(1, index + 1),
            owner=owner,
        )
        for index, owner in enumerate(owners)
    )
    topology = cli._RetainedMaterializationTopology(
        catalog_path=tmp_path / "catalog.sqlite3",
        catalog_identity=(1, 10, 1),
        cas_root=tmp_path / "CAS",
        workspace_root=tmp_path / "workspace",
        output=tmp_path / "workspace" / "context",
        cas_binding=bindings[0],
        workspace_binding=bindings[1],
        output_parent_binding=bindings[2],
    )

    with pytest.raises(OSError, match="output authority close failed"):
        topology.close()

    assert not topology.closed
    assert tuple(owner.close_calls for owner in owners) == (1, 1, 1)
    for owner in owners:
        owner.allow_close = True
    topology.close()

    assert topology.closed
    assert tuple(owner.close_calls for owner in owners) == (2, 2, 2)


def test_retained_materialization_topology_rechecks_open_identity_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    topology = cli._require_retained_materialization_topology(
        catalog,
        cas_root,
        workspace_root,
        workspace_root / "context",
    )
    delegate_called = False
    original_identity = cli._retained_open_directory_identity

    class Delegate:
        def run_workspace(self, *_args: object, **_kwargs: object) -> None:
            nonlocal delegate_called
            delegate_called = True

    def regressed_identity(authority: object) -> tuple[int, ...]:
        observed = original_identity(authority)
        if authority.display_parent == topology.cas_root:  # type: ignore[attr-defined]
            return observed[:1] + (observed[1] + 1,) + observed[2:]
        return observed

    monkeypatch.setattr(cli, "_retained_open_directory_identity", regressed_identity)
    provider = cli._RetainedTopologyWorkspaceProvider(Delegate(), topology)
    try:
        with pytest.raises(cli.CLIError, match="authority identity changed"):
            provider.run_workspace(
                object(),
                receipt_owner=object(),
                operation=object(),
            )
    finally:
        topology.close()

    assert not delegate_called


def test_retained_provider_passes_output_parent_identity_to_delegate(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    topology = cli._require_retained_materialization_topology(
        catalog,
        cas_root,
        workspace_root,
        workspace_root / "context",
    )
    observed: list[tuple[int, ...] | None] = []

    class Delegate:
        def run_workspace(
            self,
            _request: object,
            *,
            receipt_owner: object,
            operation: object,
            _expected_parent_identity: tuple[int, ...] | None = None,
        ) -> str:
            assert receipt_owner is not None
            assert operation is not None
            observed.append(_expected_parent_identity)
            return "bound"

    provider = cli._RetainedTopologyWorkspaceProvider(Delegate(), topology)
    try:
        assert (
            provider.run_workspace(
                object(),
                receipt_owner=object(),
                operation=object(),
            )
            == "bound"
        )
    finally:
        topology.close()

    assert observed == [topology.output_parent_binding.identity]


def test_retained_provider_forwards_exact_cancellation_callback(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    topology = cli._require_retained_materialization_topology(
        catalog,
        cas_root,
        workspace_root,
        workspace_root / "context",
    )
    callback_calls = 0
    observed_callback: object | None = None

    def check_cancelled() -> None:
        nonlocal callback_calls
        callback_calls += 1

    class Delegate:
        def run_workspace(
            self,
            _request: object,
            *,
            receipt_owner: object,
            operation: object,
            check_cancelled: object,
            _expected_parent_identity: tuple[int, ...] | None = None,
        ) -> str:
            nonlocal observed_callback
            assert receipt_owner is not None
            assert operation is not None
            assert _expected_parent_identity == topology.output_parent_binding.identity
            assert callable(check_cancelled)
            observed_callback = check_cancelled
            check_cancelled()
            return "interruptible"

    provider = cli._RetainedTopologyWorkspaceProvider(Delegate(), topology)
    try:
        assert (
            provider.run_workspace(
                object(),
                receipt_owner=object(),
                operation=object(),
                check_cancelled=check_cancelled,
            )
            == "interruptible"
        )
    finally:
        topology.close()

    assert observed_callback is check_cancelled
    assert callback_calls == 1


def test_artifact_materialize_rejects_catalog_file_mount_before_storage_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    catalog_mount = os.path.normpath(os.path.realpath(catalog))
    catalog_device = catalog.stat().st_dev
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_retained_linux_mount_mappings",
        lambda: (
            cli._RetainedLinuxMountMapping(
                mount_id=1,
                device=f"{os.major(catalog_device)}:{os.minor(catalog_device)}",
                root=PurePosixPath("/catalog-source.sqlite3"),
                mount_point=Path(catalog_mount),
            ),
        ),
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
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )

    with pytest.raises(cli.CLIError, match="catalog must not be a file mount point"):
        cli._run_artifact_materialize(args)


def test_artifact_materialize_rejects_simulated_catalog_identity_below_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    catalog_identity = cli._retained_catalog_file_identity(catalog)
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    device = f"{os.major(catalog_identity[0])}:{os.minor(catalog_identity[0])}"
    mappings = (
        cli._RetainedLinuxMountMapping(
            mount_id=1,
            device=device,
            root=PurePosixPath("/"),
            mount_point=Path("/"),
        ),
        cli._RetainedLinuxMountMapping(
            mount_id=2,
            device=device,
            root=PurePosixPath(catalog.parent.as_posix()),
            mount_point=cas_root,
        ),
    )
    inspected_aliases: list[Path] = []

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    def alias_identity(path: Path) -> tuple[int, int]:
        inspected_aliases.append(path)
        return catalog_identity[:2]

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(cli, "_retained_linux_mount_mappings", lambda: mappings)
    monkeypatch.setattr(cli, "_retained_alias_file_identity", alias_identity)
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
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(workspace_root / "context"),
    )

    with pytest.raises(cli.CLIError, match="physically reachable below the CAS"):
        cli._run_artifact_materialize(args)

    assert inspected_aliases == [cas_root / catalog.name]


def test_retained_materialization_accepts_disjoint_same_device_bind_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    catalog_identity = cli._retained_catalog_file_identity(catalog)
    device = f"{os.major(catalog_identity[0])}:{os.minor(catalog_identity[0])}"
    mappings = (
        cli._RetainedLinuxMountMapping(
            mount_id=1,
            device=device,
            root=PurePosixPath("/"),
            mount_point=Path("/"),
        ),
        cli._RetainedLinuxMountMapping(
            mount_id=2,
            device=device,
            root=PurePosixPath("/backing/cas"),
            mount_point=cas_root,
        ),
        cli._RetainedLinuxMountMapping(
            mount_id=3,
            device=device,
            root=PurePosixPath("/backing/workspaces"),
            mount_point=workspace_root,
        ),
    )
    monkeypatch.setattr(cli, "_retained_linux_mount_mappings", lambda: mappings)

    def binding(path: Path, label: str) -> cli._RetainedDirectoryBinding:
        metadata = path.lstat()
        return cli._RetainedDirectoryBinding(
            label=label,
            path=path,
            identity=(int(metadata.st_dev), int(metadata.st_ino)),
            owner=object(),
        )

    cli._require_retained_mount_topology(
        catalog,
        catalog_identity,
        binding(cas_root, "CAS root"),
        binding(workspace_root, "workspace root"),
        binding(workspace_root, "output parent"),
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="bind-mount topology requires Linux mount namespaces",
)
@pytest.mark.parametrize(
    "scenario",
    (
        "root-alias",
        "root-reentry",
        "output-parent",
        "catalog-file",
        "catalog-dir-cas",
        "catalog-dir-workspace",
    ),
)
def test_retained_materialization_rejects_linux_bind_mount_aliases(
    tmp_path: Path,
    scenario: str,
) -> None:
    unshare = shutil.which("unshare")
    mount = shutil.which("mount")
    if unshare is None or mount is None:
        pytest.skip("Linux unshare/mount tools are unavailable")
    probe = subprocess.run(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            "true",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip() or "permission denied"
        pytest.skip(f"Linux user/mount namespace is unavailable: {detail}")

    catalog = tmp_path / "catalog.sqlite3"
    catalog.touch()
    catalog_source = tmp_path / "catalog-source.sqlite3"
    catalog_source.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    source = tmp_path / "bind-source"
    source.mkdir()
    (source / "nested").mkdir()
    source_catalog = source / "catalog.sqlite3"
    source_catalog.touch()
    mounted_parent = workspace_root / "mounted"
    mounted_parent.mkdir()
    script = r"""
import subprocess
import sys
from pathlib import Path

from codenib import cli

(
    scenario,
    mount,
    catalog,
    catalog_source,
    cas_root,
    workspace_root,
    source,
    source_catalog,
    mounted_parent,
) = sys.argv[1:]
try:
    if scenario == "root-alias":
        subprocess.run([mount, "--bind", source, cas_root], check=True)
        subprocess.run([mount, "--bind", source, workspace_root], check=True)
        output = Path(workspace_root) / "context"
    elif scenario == "root-reentry":
        subprocess.run([mount, "--bind", source, workspace_root], check=True)
        subprocess.run(
            [mount, "--bind", str(Path(source) / "nested"), cas_root],
            check=True,
        )
        output = Path(workspace_root) / "context"
    elif scenario == "output-parent":
        subprocess.run([mount, "--bind", source, mounted_parent], check=True)
        output = Path(mounted_parent) / "context"
    elif scenario == "catalog-file":
        subprocess.run([mount, "--bind", catalog_source, catalog], check=True)
        output = Path(workspace_root) / "context"
    elif scenario == "catalog-dir-cas":
        subprocess.run([mount, "--bind", source, cas_root], check=True)
        catalog = source_catalog
        output = Path(workspace_root) / "context"
    else:
        subprocess.run([mount, "--bind", source, workspace_root], check=True)
        catalog = source_catalog
        output = Path(workspace_root) / "context"
except (OSError, subprocess.CalledProcessError) as error:
    print(f"BIND_UNAVAILABLE: {error}", file=sys.stderr)
    raise SystemExit(77)

try:
    cli._require_retained_materialization_topology(
        Path(catalog),
        Path(cas_root),
        Path(workspace_root),
        output,
    )
except cli.CLIError as error:
    message = str(error)
    if scenario == "root-alias" and not (
        "same physical object" in message or "same-device mount alias" in message
    ):
        raise
    if scenario == "root-reentry" and "same-device mount alias" not in message:
        raise
    if scenario == "output-parent" and "cross a mount point" not in message:
        raise
    if scenario == "catalog-file" and "file mount point" not in message:
        raise
    if scenario in {"catalog-dir-cas", "catalog-dir-workspace"} and not (
        "catalog is physically reachable" in message
    ):
        raise
else:
    raise AssertionError("bind-mounted storage alias was accepted")
"""
    result = subprocess.run(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            sys.executable,
            "-c",
            script,
            scenario,
            mount,
            str(catalog),
            str(catalog_source),
            str(cas_root),
            str(workspace_root),
            str(source),
            str(source_catalog),
            str(mounted_parent),
        ],
        capture_output=True,
        check=False,
        cwd=Path(__file__).parents[1],
        text=True,
    )
    if result.returncode == 77:
        detail = (result.stderr or result.stdout).strip()
        pytest.skip(f"Linux bind mount is unavailable: {detail}")
    assert result.returncode == 0, result.stderr or result.stdout


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


def test_artifact_materialize_storage_failure_closes_topology_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[cli._RetainedMaterializationTopology] = []
    original_topology = cli._require_retained_materialization_topology

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

    receipt_owner = ReceiptOwner()

    def capture_topology(*args: object, **kwargs: object):
        topology = original_topology(*args, **kwargs)
        captured.append(topology)
        return topology

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_new_retained_output_receipt_owner",
        lambda: receipt_owner,
    )
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        capture_topology,
    )
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after topology acquisition")
        ),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )

    with pytest.raises(cli.CLIError, match="stop after topology acquisition"):
        cli._run_artifact_materialize(_materialize_cli_test_args(tmp_path))

    assert len(captured) == 1
    topology = captured[0]
    assert topology.closed
    assert all(
        binding.owner.closed  # type: ignore[attr-defined]
        for binding in topology._bindings
    )
    assert receipt_owner.closed


def test_artifact_materialize_inherits_failed_topology_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_close = cli._RetainedMaterializationTopology.close
    allow_close = False

    class Provider:
        def __init__(self, _root: Path) -> None:
            pass

        def require_support(self) -> None:
            pass

    def interrupt_topology_close(self) -> None:
        if not allow_close:
            raise OSError("injected topology close failure")
        original_close(self)

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli._RetainedMaterializationTopology,
        "close",
        interrupt_topology_close,
    )
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after topology acquisition")
        ),
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: pytest.fail("catalog must not be opened"),
    )

    with pytest.raises(cli.CLIError, match="stop after topology acquisition") as caught:
        cli._run_artifact_materialize(_materialize_cli_test_args(tmp_path))

    retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
    assert len(retained) == 1
    assert not retained[0].closed
    allow_close = True
    retained[0].close()
    assert retained[0].closed


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
    store_index = next(iter(store_indexes))
    # A line event at the next source line is the portable trace boundary after
    # STORE_ATTR and before acquire() returns.  Some supported CPython builds do
    # not emit the optional per-opcode trace events.
    post_store_instruction = next(
        instruction
        for instruction in instructions[store_index + 1 :]
        if instruction.starts_line
    )
    post_store_line = getattr(post_store_instruction, "line_number", None)
    if type(post_store_line) is not int:
        post_store_line = post_store_instruction.starts_line
    assert type(post_store_line) is int
    injected = False
    previous_trace = sys.gettrace()

    def trace(frame, event: str, _arg: object):
        nonlocal injected
        if event == "call" and frame.f_code is acquire.__code__:
            return trace
        if (
            not injected
            and event == "line"
            and frame.f_code is acquire.__code__
            and frame.f_lineno == post_store_line
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


def test_detect_languages_uses_exact_repository_source_selection(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "kept.py").write_text("VALUE = 1\n")
    (tmp_path / "ios" / "Pods").mkdir(parents=True)
    (tmp_path / "ios" / "Pods" / "excluded.go").write_text("package hidden\n")
    (tmp_path / "packages" / "ios" / "Pods").mkdir(parents=True)
    (tmp_path / "packages" / "ios" / "Pods" / "kept.go").write_text("package kept\n")

    assert cli.detect_languages(
        tmp_path,
        source_selection=RepositorySourceSelection(("ios/Pods",)),
    ) == ["go", "python"]


def test_index_command_resolves_selection_before_language_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "kept.py").write_text("VALUE = 1\n")
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "excluded.go").write_text("package hidden\n")
    captured: dict[str, object] = {}

    def fake_index(repo_path, **kwargs):
        captured.update(repo_path=repo_path, **kwargs)
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=kwargs["languages"],
                source_selection=kwargs["source_selection"],
                indexes={
                    "bm25": SimpleNamespace(status="fresh", metadata={}),
                },
            ),
            [],
        )

    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert (
        cli.run(
            [
                "index",
                str(tmp_path),
                "--preset",
                "fast",
                "--exclude-dir",
                "ios",
            ]
        )
        == 0
    )
    assert captured["languages"] == ["python"]
    assert captured["source_selection"] == RepositorySourceSelection(("ios",))


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

    def fake_index(repo_path, *, languages, views, source_selection, rebuild):
        captured.update(
            repo_path=repo_path,
            languages=languages,
            views=views,
            source_selection=source_selection,
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
        "source_selection": RepositorySourceSelection(),
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
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(
        "def greet(name: str) -> str:\n"
        '    """Return a greeting."""\n'
        '    return f"hello {name}"\n'
    )

    manifest, failed = cli.index_repository(
        repo,
        languages=["python"],
        views=["bm25"],
    )

    assert failed == []
    assert manifest.languages == ["python"]
    assert manifest.indexes["bm25"].status == "fresh"
    assert (repo_index_dir(repo) / "repo_manifest.json").is_file()
    assert not (repo / ".codenib_cache").exists()


def test_fast_index_accepts_expo_style_contained_absolute_directory_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX Expo-style absolute symlink fixture")
    from codenib.paths import repo_index_dir

    repo = tmp_path / "repo"
    package = repo / "node_modules" / ".pnpm" / "dependency" / "package"
    pods = repo / "ios" / "Pods"
    package.mkdir(parents=True)
    pods.mkdir(parents=True)
    (repo / "sample.py").write_text("def sample():\n    return 1\n")
    (package / "generated.py").write_text("raise AssertionError('ignored')\n")
    (pods / "Dependency").symlink_to(package, target_is_directory=True)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))

    manifest, failed = cli.index_repository(
        repo,
        languages=["python"],
        views=["bm25"],
    )

    assert failed == []
    assert manifest.indexes["bm25"].status == "fresh"
    assert (repo_index_dir(repo) / "repo_manifest.json").is_file()


def test_index_exclusion_handles_expo_pods_symlink_and_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX Expo-style absolute symlink fixture")
    from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
    from codenib.paths import repo_index_dir

    repo = tmp_path / "repo"
    package = repo / "node_modules" / ".pnpm" / "dependency" / "package"
    pods = repo / "ios" / "Pods"
    package.mkdir(parents=True)
    pods.mkdir(parents=True)
    (repo / "sample.py").write_text("def sample():\n    return 1\n")
    (package / "generated.py").write_text("GENERATED = 1\n")
    (pods / "Dependency").symlink_to(package, target_is_directory=True)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))

    first = cli.run(
        [
            "index",
            str(repo),
            "--preset",
            "fast",
            "--language",
            "python",
            "--exclude-dir",
            "ios/Pods",
        ]
    )
    second = cli.run(
        [
            "index",
            str(repo),
            "--preset",
            "fast",
            "--language",
            "python",
        ]
    )
    manifest = RepoManifest.load(repo_index_dir(repo) / MANIFEST_FILENAME)

    assert first == 0
    assert second == 0
    assert manifest.source_selection == RepositorySourceSelection(("ios/Pods",))
    assert manifest.indexes["bm25"].status == "fresh"


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

    def fake_index(repo_path, *, languages, views, source_selection, rebuild):
        assert source_selection == RepositorySourceSelection()
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
    assert parser.parse_args(["mcp"]).path is None
    assert parser.parse_args(["mcp", "--tool-surface", "explore"]).tool_surface == (
        "explore"
    )
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["mcp", "--tool-surface", "hidden"])

    assert exc_info.value.code == 2


def _retained_mcp_argv(
    tmp_path: Path,
    *,
    repo: Path | None = None,
) -> list[str]:
    arguments = [
        "mcp",
        "--catalog",
        str(tmp_path / "catalog.sqlite3"),
        "--cas-root",
        str(tmp_path / "cas"),
        "--workspace-root",
        str(tmp_path / "workspaces"),
        "--output",
        str(tmp_path / "workspaces" / "runtime"),
        "--repository",
        "owner/repo",
    ]
    if repo is not None:
        arguments.extend(("--repo", str(repo)))
    return arguments


def test_mcp_retained_parser_exposes_ref_and_snapshot_selection(
    tmp_path: Path,
) -> None:
    parser = cli.build_parser()
    ref = parser.parse_args(
        [
            *_retained_mcp_argv(tmp_path),
            "--ref",
            "release",
            "--expected-generation",
            "7",
        ]
    )
    snapshot = parser.parse_args(
        [*_retained_mcp_argv(tmp_path), "--snapshot", "snap_123"]
    )

    assert ref.path is None
    assert ref.ref == "release"
    assert ref.snapshot is None
    assert ref.expected_generation == 7
    assert snapshot.ref is None
    assert snapshot.snapshot == "snap_123"
    assert snapshot.expected_generation is None

    with pytest.raises(SystemExit) as both:
        parser.parse_args(
            [*_retained_mcp_argv(tmp_path), "--ref", "main", "--snapshot", "snap"]
        )
    assert both.value.code == 2


def test_mcp_retained_mode_accepts_only_an_explicit_source_checkout(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    parser = cli.build_parser()
    retained = parser.parse_args(_retained_mcp_argv(tmp_path, repo=repo))
    artifact = parser.parse_args(
        ["mcp", "--artifact", "/tmp/context", "--repo", str(repo)]
    )

    assert cli._mcp_context_mode(retained) == "retained"
    assert retained.repo == str(repo)
    assert cli._mcp_context_mode(artifact) == "artifact"

    for context_input in ((".",), ("--artifact", "/tmp/context")):
        mixed = parser.parse_args(
            [*_retained_mcp_argv(tmp_path, repo=repo), *context_input]
        )
        with pytest.raises(cli.CLIError, match="manifest|--artifact"):
            cli._mcp_context_mode(mixed)


def test_mcp_repo_without_artifact_or_retained_selection_is_rejected() -> None:
    args = cli.build_parser().parse_args(["mcp", "--repo", "/src/repo"])

    with pytest.raises(cli.CLIError, match="requires --artifact or retained MCP"):
        cli._mcp_context_mode(args)


def test_mcp_repo_cannot_bind_a_positional_manifest() -> None:
    args = cli.build_parser().parse_args(["mcp", ".", "--repo", "/src/repo"])

    with pytest.raises(cli.CLIError, match="requires --artifact or retained MCP"):
        cli._mcp_context_mode(args)


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--catalog", "catalog.sqlite3"), "retained MCP storage requires"),
        (("--repository", "owner/repo"), "require --artifact"),
        (
            ("--runtime-probe", "--catalog", "catalog.sqlite3"),
            "--runtime-probe cannot be combined",
        ),
        (
            ("--runtime-probe", "--repo", "/src/repo"),
            "--runtime-probe cannot be combined",
        ),
    ),
)
def test_mcp_context_modes_fail_before_dependency_probe(
    arguments: tuple[str, ...],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_require_modules",
        lambda *_args, **_kwargs: pytest.fail("dependency probe must not run"),
    )

    assert cli.run(["mcp", *arguments]) == 2
    assert message in capsys.readouterr().err


def test_mcp_retained_rejects_mixed_context_and_snapshot_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_require_modules",
        lambda *_args, **_kwargs: pytest.fail("dependency probe must not run"),
    )
    complete = _retained_mcp_argv(tmp_path)

    assert cli.run(["mcp", ".", *complete[1:]]) == 2
    assert "cannot be combined with a manifest" in capsys.readouterr().err
    assert cli.run([*complete, "--snapshot", "snap", "--expected-generation", "2"]) == 2
    assert "requires retained ref selection" in capsys.readouterr().err


def test_mcp_retained_source_still_requires_a_repository_key_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_require_modules",
        lambda *_args, **_kwargs: pytest.fail("dependency probe must not run"),
    )
    arguments = _retained_mcp_argv(tmp_path, repo=tmp_path / "repo")
    repository_option = arguments.index("--repository")
    del arguments[repository_option : repository_option + 2]

    assert cli.run(arguments) == 2
    assert "--repository" in capsys.readouterr().err


def test_mcp_retained_dispatches_only_after_dependency_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        cli,
        "_require_modules",
        lambda modules, **_kwargs: calls.append(tuple(modules)),
    )
    monkeypatch.setattr(
        cli,
        "_run_mcp_retained",
        lambda args: calls.append(args) or 0,
    )
    args = cli.build_parser().parse_args(_retained_mcp_argv(tmp_path))

    assert cli._run_mcp(args) == 0
    assert calls == [("mcp",), args]


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("empty", "repository source path is required"),
        ("nul", "repository source path is invalid"),
        ("missing", "repository source must resolve to one existing real directory"),
        ("file", "repository source must resolve to one existing real directory"),
        ("symlink", "repository source must resolve to one existing real directory"),
    ),
)
def test_mcp_retained_rejects_invalid_source_before_any_mutation(
    tmp_path: Path,
    case: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.retained_context as retained_context_module

    target = tmp_path / "repository"
    target.mkdir()
    if case == "empty":
        value = ""
    elif case == "nul":
        value = "bad\x00repository"
    elif case == "missing":
        value = str(tmp_path / "missing")
    elif case == "file":
        source_file = tmp_path / "source.py"
        source_file.touch()
        value = str(source_file)
    else:
        source_link = tmp_path / "repository-link"
        source_link.symlink_to(target, target_is_directory=True)
        value = str(source_link)

    args = cli.build_parser().parse_args(
        [*_retained_mcp_argv(tmp_path), "--repo", value]
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid source preflight must precede retained mutation")

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", unexpected)
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        unexpected,
    )
    monkeypatch.setattr(storage_module, "LocalCAS", unexpected)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", unexpected)
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_ref",
        unexpected,
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_snapshot",
        unexpected,
    )

    with pytest.raises(cli.CLIError, match=message):
        cli._run_mcp_retained(args)


def _retained_source_topology_paths(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    catalog_parent = tmp_path / "catalog-state"
    catalog_parent.mkdir()
    catalog = catalog_parent / "catalog.sqlite3"
    catalog.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    output = workspace_root / "runtime"
    return repository, catalog, cas_root, workspace_root, output


@pytest.mark.parametrize(
    "protected",
    ("catalog", "CAS root", "workspace root", "output"),
)
def test_mcp_retained_rejects_lexical_source_storage_aliases(
    tmp_path: Path,
    protected: str,
) -> None:
    repository, catalog, cas_root, workspace_root, output = (
        _retained_source_topology_paths(tmp_path)
    )
    repository.rmdir()
    repository = {
        "catalog": catalog.parent,
        "CAS root": cas_root,
        "workspace root": workspace_root / "source",
        "output": output,
    }[protected]
    args = SimpleNamespace(
        catalog=str(catalog),
        cas_root=str(cas_root),
        workspace_root=str(workspace_root),
        output=str(output),
    )

    with pytest.raises(
        cli.CLIError,
        match=rf"repository source must not overlap the {protected}",
    ):
        cli._retained_materialization_paths(args, repo_path=repository)


def test_mcp_retained_rejects_lexical_source_alias_before_pin_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.retained_context as retained_context_module

    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    args = cli.build_parser().parse_args(_retained_mcp_argv(tmp_path, repo=cas_root))

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("lexical source alias must fail before authority or provider work")

    monkeypatch.setattr(
        source_fingerprint_module,
        "pin_repository_source_root",
        unexpected,
    )
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", unexpected)
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        unexpected,
    )
    monkeypatch.setattr(storage_module, "LocalCAS", unexpected)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", unexpected)
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_ref",
        unexpected,
    )

    with pytest.raises(
        cli.CLIError,
        match="repository source must not overlap the CAS root",
    ):
        cli._run_mcp_retained(args)


@pytest.mark.parametrize(
    "protected",
    ("catalog", "CAS root", "workspace root", "output"),
)
def test_mcp_retained_rejects_resolved_source_storage_aliases(
    tmp_path: Path,
    protected: str,
) -> None:
    repository, catalog, cas_root, workspace_root, output = (
        _retained_source_topology_paths(tmp_path)
    )
    if protected == "catalog":
        catalog.unlink()
        physical_catalog = repository / "catalog.sqlite3"
        physical_catalog.touch()
        catalog.symlink_to(physical_catalog)
    elif protected == "CAS root":
        cas_root.rmdir()
        cas_root.symlink_to(repository, target_is_directory=True)
    else:
        physical_workspace = tmp_path / "physical-workspace"
        physical_workspace.mkdir()
        workspace_root.rmdir()
        workspace_root.symlink_to(physical_workspace, target_is_directory=True)
        if protected == "workspace root":
            repository.rmdir()
            repository = physical_workspace / "source"
            repository.mkdir()
        else:
            repository.rmdir()
            repository = physical_workspace

    with pytest.raises(
        cli.CLIError,
        match=rf"repository source must not physically overlap the {protected}",
    ):
        with source_fingerprint_module.pin_repository_source_root(
            repository
        ) as repository_authority:
            cli._require_retained_materialization_topology(
                catalog,
                cas_root,
                workspace_root,
                output,
                repo_path=repository,
                repository_authority=repository_authority,
            )


@pytest.mark.parametrize(
    ("protected", "ancestry_label"),
    (
        ("catalog", "catalog parent"),
        ("CAS root", "CAS root"),
        ("workspace root", "workspace root"),
        ("output", "output parent"),
    ),
)
def test_mcp_retained_rejects_directory_identity_source_aliases(
    tmp_path: Path,
    protected: str,
    ancestry_label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, catalog, cas_root, workspace_root, output = (
        _retained_source_topology_paths(tmp_path)
    )
    real_ancestry = cli._retained_directory_ancestry
    repository_ancestry = real_ancestry(
        repository,
        label="repository source",
    )
    repository_identity = repository_ancestry[0][1]

    def aliased_ancestry(
        path: Path,
        *,
        label: str,
    ) -> tuple[tuple[Path, tuple[int, int]], ...]:
        observed = list(real_ancestry(path, label=label))
        if label == ancestry_label:
            observed[0] = (observed[0][0], repository_identity)
        return tuple(observed)

    monkeypatch.setattr(cli, "_retained_directory_ancestry", aliased_ancestry)

    with pytest.raises(
        cli.CLIError,
        match=rf"repository source must not physically alias the {protected}",
    ):
        cli._require_retained_repository_topology(
            repository,
            catalog,
            cas_root,
            workspace_root,
            output,
        )


@pytest.mark.parametrize(
    "protected",
    ("catalog", "CAS root", "workspace root", "output"),
)
def test_mcp_retained_rejects_linux_mapped_source_bind_aliases(
    tmp_path: Path,
    protected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, catalog, cas_root, workspace_root, output = (
        _retained_source_topology_paths(tmp_path)
    )
    protected_paths = {
        "catalog": catalog,
        "CAS root": cas_root,
        "workspace root": workspace_root,
        "output": output,
    }
    mappings = [
        cli._RetainedLinuxMountMapping(
            mount_id=1,
            device="0:1",
            root=PurePosixPath("/host"),
            mount_point=Path("/"),
        ),
        cli._RetainedLinuxMountMapping(
            mount_id=2,
            device="8:1",
            root=PurePosixPath("/shared/source"),
            mount_point=repository,
        ),
    ]
    for index, (label, path) in enumerate(protected_paths.items(), start=3):
        is_alias = label == protected
        mappings.append(
            cli._RetainedLinuxMountMapping(
                mount_id=index,
                device="8:1" if is_alias else f"9:{index}",
                root=(
                    PurePosixPath("/shared/source")
                    if is_alias
                    else PurePosixPath(f"/protected/{index}")
                ),
                mount_point=path,
            )
        )
    monkeypatch.setattr(
        cli,
        "_retained_linux_mount_mappings",
        lambda: tuple(mappings),
    )

    with pytest.raises(
        cli.CLIError,
        match=rf"repository source must not traverse a physical {protected} alias",
    ):
        cli._require_retained_repository_topology(
            repository,
            catalog,
            cas_root,
            workspace_root,
            output,
        )


@pytest.mark.parametrize(
    ("selector", "loader_name"),
    (
        (("--ref", "release", "--expected-generation", "4"), "ref"),
        (("--snapshot", "snapshot-4"), "snapshot"),
    ),
)
@pytest.mark.parametrize("source_bound", (False, True))
def test_mcp_retained_passes_only_the_topology_frozen_source_to_loaders(
    tmp_path: Path,
    selector: tuple[str, ...],
    loader_name: str,
    source_bound: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.retained_context as retained_context_module
    import codenib.mcp.server as server_module

    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    output = workspace_root / "runtime"
    arguments = [
        "mcp",
        "--catalog",
        str(catalog_path),
        "--cas-root",
        str(cas_root),
        "--workspace-root",
        str(workspace_root),
        "--output",
        str(output),
        "--repository",
        "owner/repo",
        *selector,
    ]
    if source_bound:
        arguments.extend(("--repo", str(repository / ".")))
    args = cli.build_parser().parse_args(arguments)

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    class Resource:
        def __init__(self, label: str) -> None:
            self.label = label
            self.closed = False

        def close(self) -> None:
            self.closed = True

    object_store = Resource("CAS")
    catalog = Resource("catalog")
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    authority_roots: list[Path | None] = []

    def record_loader(
        name: str,
        values: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        expected = kwargs["expected_root_authority"]
        if source_bound:
            assert expected is not None
            assert not expected.closed  # type: ignore[attr-defined]
            authority_roots.append(expected.root)  # type: ignore[attr-defined]
        else:
            assert expected is None
            authority_roots.append(None)
        calls.append((name, values, kwargs))

    def ref_loader(*values: object, **kwargs: object) -> None:
        record_loader("ref", values, kwargs)

    def snapshot_loader(*values: object, **kwargs: object) -> None:
        record_loader("snapshot", values, kwargs)

    served: list[object] = []

    def serve(owner: object, **kwargs: object) -> None:
        assert object_store.closed
        assert catalog.closed
        assert kwargs == {"log_level": "INFO", "tool_surface": "full"}
        served.append(owner)

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        storage_module, "LocalCAS", lambda *_args, **_kwargs: object_store
    )
    monkeypatch.setattr(
        storage_module, "SQLiteCatalog", lambda *_args, **_kwargs: catalog
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_ref",
        ref_loader,
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_snapshot",
        snapshot_loader,
    )
    monkeypatch.setattr(server_module, "serve_retained_context", serve)

    assert cli._run_mcp_retained(args) == 0

    assert len(calls) == 1
    selected, positional, kwargs = calls[0]
    assert selected == loader_name
    common_keys = {
        "namespace_name",
        "catalog",
        "object_store",
        "workspace_provider",
        "runtime_owner",
        "repo_path",
        "expected_root_authority",
    }
    if loader_name == "ref":
        assert positional == ("owner/repo", output)
        assert set(kwargs) == common_keys | {"ref_name", "expected_generation"}
        assert kwargs["ref_name"] == "release"
        assert kwargs["expected_generation"] == 4
    else:
        assert positional == ("owner/repo", "snapshot-4", output)
        assert set(kwargs) == common_keys
    expected_repo = (
        Path(os.path.abspath(os.fspath(repository))) if source_bound else None
    )
    assert kwargs["repo_path"] == expected_repo
    provider = kwargs["workspace_provider"]
    assert type(provider) is cli._RetainedTopologyWorkspaceProvider
    expected_authority = provider.topology.repository_authority
    assert kwargs["expected_root_authority"] is expected_authority
    if source_bound:
        assert expected_authority is not None
        assert expected_authority.closed
    else:
        assert expected_authority is None
    assert authority_roots == [expected_repo]
    assert kwargs["namespace_name"] == "default"
    assert kwargs["catalog"] is catalog
    assert kwargs["object_store"] is object_store
    assert provider.topology.repo_path == expected_repo
    assert provider.topology.closed
    assert kwargs["runtime_owner"] is served[0]
    assert object_store.closed
    assert catalog.closed


@pytest.mark.parametrize("replacement", ("root", "ancestor"))
def test_mcp_retained_rejects_identical_source_replacement_before_capture(
    tmp_path: Path,
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.retained_context as retained_context_module
    import codenib.mcp.server as server_module

    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    if replacement == "root":
        repository = tmp_path / "repository"
        repository.mkdir()
        candidate = tmp_path / "candidate-repository"
        candidate.mkdir()
        displaced = tmp_path / "displaced-repository"

        def replace_repository() -> None:
            repository.rename(displaced)
            candidate.rename(repository)

    else:
        source_parent = tmp_path / "source-parent"
        source_parent.mkdir()
        repository = source_parent / "repository"
        repository.mkdir()
        candidate_parent = tmp_path / "candidate-parent"
        candidate_parent.mkdir()
        displaced_parent = tmp_path / "displaced-parent"

        def replace_repository() -> None:
            source_parent.rename(displaced_parent)
            candidate_parent.rename(source_parent)
            (displaced_parent / "repository").rename(repository)

    source_text = "VALUE = 'IDENTICAL_SOURCE_REPLACEMENT'\n"
    (repository / "sample.py").write_text(source_text, encoding="utf-8")
    if replacement == "root":
        (candidate / "sample.py").write_text(source_text, encoding="utf-8")

    args = cli.build_parser().parse_args(_retained_mcp_argv(tmp_path, repo=repository))

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    object_store = Resource()
    catalog = Resource()
    authorities: list[object] = []
    topologies: list[object] = []
    real_topology = cli._require_retained_materialization_topology
    replacement_complete = False

    def capture_topology(*values: object, **kwargs: object):
        topology = real_topology(*values, **kwargs)
        topologies.append(topology)
        return topology

    def load_after_replacement(*_args: object, **kwargs: object) -> None:
        nonlocal replacement_complete
        expected = kwargs["expected_root_authority"]
        assert expected is not None
        assert not expected.closed
        assert kwargs["repo_path"] == repository
        authorities.append(expected)
        replace_repository()
        replacement_complete = True
        binding = source_fingerprint_module.capture_repository_source(
            kwargs["repo_path"],
            expected_root_authority=expected,
        )
        try:
            pytest.fail("replacement source capture unexpectedly succeeded")
        finally:
            binding.close()

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        capture_topology,
    )
    monkeypatch.setattr(
        storage_module,
        "LocalCAS",
        lambda *_args, **_kwargs: object_store,
    )
    monkeypatch.setattr(
        storage_module,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_ref",
        load_after_replacement,
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_snapshot",
        lambda *_args, **_kwargs: pytest.fail("snapshot loader must not run"),
    )
    monkeypatch.setattr(
        server_module,
        "serve_retained_context",
        lambda *_args, **_kwargs: pytest.fail("replaced source must not be served"),
    )

    with pytest.raises(
        cli.CLIError,
        match="expected repository root authority changed during capture",
    ):
        cli._run_mcp_retained(args)

    assert replacement_complete
    assert len(authorities) == 1
    assert authorities[0].closed  # type: ignore[attr-defined]
    assert object_store.closed
    assert catalog.closed
    assert len(topologies) == 1
    assert topologies[0].closed  # type: ignore[attr-defined]
    assert topologies[0].repository_authority.closed  # type: ignore[attr-defined]


def test_mcp_retained_source_replacement_during_provider_probe_blocks_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.retained_context as retained_context_module
    import codenib.mcp.server as server_module

    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    candidate = tmp_path / "candidate-repository"
    candidate.mkdir()
    displaced = tmp_path / "displaced-repository"
    source_text = "VALUE = 'IDENTICAL_PROVIDER_REPLACEMENT'\n"
    (repository / "sample.py").write_text(source_text, encoding="utf-8")
    (candidate / "sample.py").write_text(source_text, encoding="utf-8")
    args = cli.build_parser().parse_args(_retained_mcp_argv(tmp_path, repo=repository))
    captured_authorities: list[object] = []
    real_pin = source_fingerprint_module.pin_repository_source_root

    def capture_pin(*values: object, **kwargs: object):
        authority = real_pin(*values, **kwargs)
        captured_authorities.append(authority)
        return authority

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            repository.rename(displaced)
            candidate.rename(repository)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("replaced repository must fail before retained storage or loading")

    monkeypatch.setattr(
        source_fingerprint_module,
        "pin_repository_source_root",
        capture_pin,
    )
    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(storage_module, "LocalCAS", unexpected)
    monkeypatch.setattr(storage_module, "SQLiteCatalog", unexpected)
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_ref",
        unexpected,
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_snapshot",
        unexpected,
    )
    monkeypatch.setattr(server_module, "serve_retained_context", unexpected)

    with pytest.raises(
        cli.CLIError,
        match="repository source authority changed",
    ):
        cli._run_mcp_retained(args)

    assert len(captured_authorities) == 1
    assert captured_authorities[0].closed  # type: ignore[attr-defined]


def test_mcp_retained_cancellation_closes_source_storage_and_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.retained_context as retained_context_module
    import codenib.mcp.server as server_module

    catalog_path = tmp_path / "catalog.sqlite3"
    catalog_path.touch()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    args = cli.build_parser().parse_args(_retained_mcp_argv(tmp_path, repo=repository))
    primary = KeyboardInterrupt("retained MCP load cancelled")

    class Provider:
        def __init__(self, root: Path) -> None:
            assert root == workspace_root

        def require_support(self) -> None:
            pass

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Source(Resource):
        pass

    object_store = Resource()
    catalog = Resource()
    source = Source()
    owners: list[object] = []
    topologies: list[object] = []
    real_topology = cli._require_retained_materialization_topology

    def capture_topology(*values: object, **kwargs: object):
        topology = real_topology(*values, **kwargs)
        topologies.append(topology)
        return topology

    def cancel_load(*_args: object, **kwargs: object) -> None:
        owner = kwargs["runtime_owner"]
        owners.append(owner)
        owner._source_owner.retain(source)  # type: ignore[attr-defined]  # noqa: SLF001
        raise primary

    monkeypatch.setattr(codenib, "LocalWorkspaceProvider", Provider)
    monkeypatch.setattr(
        cli,
        "_require_retained_materialization_topology",
        capture_topology,
    )
    monkeypatch.setattr(
        storage_module, "LocalCAS", lambda *_args, **_kwargs: object_store
    )
    monkeypatch.setattr(
        storage_module, "SQLiteCatalog", lambda *_args, **_kwargs: catalog
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_ref",
        cancel_load,
    )
    monkeypatch.setattr(
        retained_context_module,
        "load_retained_server_context_snapshot",
        lambda *_args, **_kwargs: pytest.fail("snapshot loader must not run"),
    )
    monkeypatch.setattr(
        server_module,
        "serve_retained_context",
        lambda *_args, **_kwargs: pytest.fail("cancelled context must not be served"),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        cli._run_mcp_retained(args)

    assert caught.value is primary
    assert len(owners) == 1
    assert owners[0].closed  # type: ignore[attr-defined]
    assert source.closed
    assert object_store.closed
    assert catalog.closed
    assert len(topologies) == 1
    assert topologies[0].closed  # type: ignore[attr-defined]
    assert topologies[0].repository_authority.closed  # type: ignore[attr-defined]


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
    from codenib.compiler.manifest import RepoManifest

    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    manifest = tmp_path / "repo_manifest.json"
    RepoManifest(repo_path=str(tmp_path)).save(manifest)
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
    from codenib.compiler.manifest import IndexEntry

    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    manifest_path = tmp_path / "repo_manifest.json"
    _current_manifest_with_fresh_index(
        tmp_path,
        IndexEntry(
            index_type="bm25",
            path=str(tmp_path / "bm25"),
            built_at="2026-08-04T00:00:00+00:00",
            built_at_epoch=0.0,
            status="fresh",
        ),
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
    from codenib.compiler.manifest import IndexEntry

    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    manifest_path = tmp_path / "repo_manifest.json"
    _current_manifest_with_fresh_index(
        tmp_path,
        IndexEntry(
            index_type="vector",
            path=str(tmp_path / "vector"),
            built_at="2026-08-04T00:00:00+00:00",
            built_at_epoch=0.0,
            status="fresh",
            config=VectorIndexBuilder().artifact_identity(),
        ),
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
