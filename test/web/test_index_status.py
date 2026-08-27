# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib.web.app as web_app
from codenib.compiler.manifest import LEGACY_MANIFEST_VERSION, IndexEntry, RepoManifest
from codenib.web.index_status import IndexUpdateCapability, build_repo_index_status

_INDEXED = "a" * 40
_CURRENT = "b" * 40


def _entry(
    index_type: str,
    *,
    status: str = "fresh",
    commit: str = _INDEXED,
    metadata: dict | None = None,
) -> IndexEntry:
    return IndexEntry(
        index_type=index_type,
        path=f"/artifacts/{index_type}",
        built_at="2026-08-26T00:00:00+00:00",
        built_at_epoch=1.0,
        status=status,
        metadata=dict(metadata or {}),
        commit=commit,
    )


def _bundle(indexes: dict[str, IndexEntry]):
    manifest = RepoManifest(
        version=LEGACY_MANIFEST_VERSION,
        commit=_INDEXED,
        last_indexed_commit=_INDEXED,
        indexes=indexes,
    )
    return SimpleNamespace(
        entry=SimpleNamespace(instance_id="repo", repo_dir="/repo"),
        manifest=manifest,
    )


def test_status_returns_exact_primary_surfaces_with_safe_defaults() -> None:
    status = build_repo_index_status(
        _bundle(
            {
                "bm25": _entry("bm25"),
                "symbol_graph": _entry("symbol_graph", status="failed"),
            }
        ),
        current_head_resolver=lambda path: _INDEXED,
    )

    assert status.repo_id == "repo"
    assert status.last_indexed_commit == _INDEXED
    assert status.current_head == _INDEXED
    assert status.stale is False
    assert [index.index_type for index in status.indexes] == [
        "bm25",
        "vector",
        "symbol_graph",
    ]
    assert [index.state for index in status.indexes] == [
        "built",
        "missing",
        "failed",
    ]
    assert all(index.update_mode == "unavailable" for index in status.indexes)
    assert all(index.updates_enabled is False for index in status.indexes)
    assert all(index.update_reason for index in status.indexes)


def test_status_marks_fresh_manifest_view_stale_when_head_moves() -> None:
    status = build_repo_index_status(
        _bundle({"bm25": _entry("bm25")}),
        current_head_resolver=lambda path: _CURRENT,
    )

    assert status.current_head == _CURRENT
    assert status.stale is True
    assert status.indexes[0].state == "stale"
    assert status.indexes[0].stale is True


def test_status_accepts_an_indexed_commit_prefix() -> None:
    bundle = _bundle({"bm25": _entry("bm25", commit=_INDEXED[:12])})
    bundle.manifest.commit = _INDEXED[:12]
    bundle.manifest.last_indexed_commit = _INDEXED[:12]
    status = build_repo_index_status(
        bundle,
        current_head_resolver=lambda path: _INDEXED,
    )

    assert status.stale is False
    assert status.indexes[0].state == "built"


def test_status_projects_explicit_capabilities_and_valid_metrics() -> None:
    status = build_repo_index_status(
        _bundle(
            {
                "vector": _entry(
                    "vector",
                    metadata={
                        "changed_files": 4,
                        "chunks_reembedded": 3,
                        "chunks_from_cache": 2,
                        "cache_hit_rate": 0.4,
                        "new_commit": _INDEXED,
                    },
                )
            }
        ),
        update_capabilities={
            "bm25": IndexUpdateCapability(mode="rebuild", enabled=True, reason=""),
            "vector": IndexUpdateCapability(
                mode="incremental",
                enabled=True,
                reason="",
            ),
            "symbol_graph": IndexUpdateCapability(
                mode="patch",
                enabled=False,
                reason="No admitted verifier is configured.",
            ),
        },
        current_head_resolver=lambda path: _INDEXED,
    )

    vector = status.indexes[1]
    assert status.indexes[0].update_mode == "rebuild"
    assert vector.update_mode == "incremental"
    assert vector.updates_enabled is True
    assert vector.metrics.model_dump() == {
        "changed_files": 4,
        "chunks_reembedded": 3,
        "chunks_from_cache": 2,
        "cache_hit_rate": 0.4,
        "new_commit": _INDEXED,
    }
    assert status.indexes[2].update_mode == "patch"
    assert status.indexes[2].updates_enabled is False


def test_status_omits_malformed_optional_metrics() -> None:
    status = build_repo_index_status(
        _bundle(
            {
                "vector": _entry(
                    "vector",
                    metadata={
                        "changed_files": True,
                        "chunks_reembedded": -1,
                        "chunks_from_cache": "2",
                        "cache_hit_rate": float("nan"),
                        "new_commit": "",
                    },
                )
            }
        ),
        current_head_resolver=lambda path: _INDEXED,
    )

    assert status.indexes[1].metrics is None


def test_status_rejects_unknown_or_incoherent_writer_capabilities() -> None:
    with pytest.raises(ValueError, match="unsupported index update capability"):
        build_repo_index_status(
            _bundle({}),
            update_capabilities={"zoekt": IndexUpdateCapability()},
            current_head_resolver=lambda path: _INDEXED,
        )
    with pytest.raises(ValueError, match="cannot be enabled"):
        IndexUpdateCapability(mode="unavailable", enabled=True, reason="")


def test_status_passes_repository_path_to_head_resolver() -> None:
    observed: list[Path] = []

    def resolve(path: Path) -> str:
        observed.append(path)
        return _INDEXED

    build_repo_index_status(_bundle({}), current_head_resolver=resolve)

    assert observed == [Path("/repo")]


def test_index_status_endpoint_pins_generation_through_projection(monkeypatch) -> None:
    events: list[str] = []
    bundle = _bundle({"bm25": _entry("bm25")})

    class Registry:
        @contextmanager
        def pin(self, repo_id: str):
            assert repo_id == "repo"
            events.append("pin-enter")
            try:
                yield bundle
            finally:
                events.append("pin-exit")

    def head(path: Path) -> str:
        assert path == Path("/repo")
        events.append("head")
        return _INDEXED

    async def inline(function, *args, **kwargs):
        events.append("thread")
        return function(*args, **kwargs)

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(web_app.app.state, "index_head_resolver", head, raising=False)
    monkeypatch.setattr(web_app, "_run_pinned_thread", inline)

    status = asyncio.run(web_app.index_status("repo"))

    assert status.indexes[0].state == "built"
    assert events == ["pin-enter", "thread", "head", "pin-exit"]


def test_index_status_endpoint_resolves_update_capabilities_per_repository(
    monkeypatch,
) -> None:
    bundle = _bundle({"bm25": _entry("bm25")})
    observed: list[str] = []

    class Registry:
        @contextmanager
        def pin(self, repo_id: str):
            assert repo_id == "repo"
            yield bundle

    def resolve(repo_id: str):
        observed.append(repo_id)
        return None

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    globally_enabled = {
        "bm25": IndexUpdateCapability(mode="rebuild", enabled=True, reason="")
    }
    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(
        web_app.app.state,
        "index_update_capabilities",
        globally_enabled,
        raising=False,
    )
    monkeypatch.setattr(
        web_app.app.state,
        "index_update_capabilities_resolver",
        resolve,
        raising=False,
    )
    monkeypatch.setattr(
        web_app.app.state,
        "index_head_resolver",
        lambda _path: _INDEXED,
        raising=False,
    )
    monkeypatch.setattr(web_app, "_run_pinned_thread", inline)

    status = asyncio.run(web_app.index_status("repo"))

    assert observed == ["repo"]
    assert status.indexes[0].updates_enabled is False
    assert status.indexes[0].update_mode == "unavailable"


def test_index_status_endpoint_retains_pin_until_cancelled_projection_settles(
    monkeypatch,
) -> None:
    events: list[str] = []
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    bundle = _bundle({"bm25": _entry("bm25")})

    class Registry:
        @contextmanager
        def pin(self, repo_id: str):
            assert repo_id == "repo"
            events.append("pin-enter")
            try:
                yield bundle
            finally:
                assert worker_finished.is_set()
                events.append("pin-exit")

    def head(path: Path) -> str:
        assert path == Path("/repo")
        events.append("head-start")
        worker_started.set()
        assert release_worker.wait(5)
        events.append("head-finish")
        worker_finished.set()
        return _INDEXED

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(web_app.app.state, "index_head_resolver", head, raising=False)

    async def cancel_projection() -> None:
        request = asyncio.create_task(web_app.index_status("repo"))
        try:
            assert await asyncio.to_thread(worker_started.wait, 5)
            request.cancel()
            await asyncio.sleep(0)
            assert not request.done()
        finally:
            release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await request

    asyncio.run(cancel_projection())

    assert events == ["pin-enter", "head-start", "head-finish", "pin-exit"]
