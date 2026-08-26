# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-repo skill-registry isolation, config, and the QA registry."""

import dis
import inspect
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from codenib.agent.skills.registry import SkillRegistry
from codenib.compiler.artifact_fingerprints import (
    bm25_artifact_file_fingerprints,
    regular_file_fingerprint,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.native_index_authorization import (
    InvalidNativeIndexAuthorizationError,
    MissingNativeIndexAuthorizationError,
    _mint_trusted_local_admin_authorization,
)
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import capture_repository_source, fingerprint_repository
from codenib.web.config import (
    QAConfig,
    RepoEntry,
    load_config,
    load_registry,
    save_registry,
)
from codenib.web.repo_registry import (
    _DEMO_SYSTEM_PROMPT,
    RepoBundle,
    RepoRegistry,
    _fresh_registry,
    _OwnedRepoBundle,
)


def _write_source_manifest(repo, manifest_path, *, selection=None):
    selected = selection or RepositorySourceSelection()
    source = fingerprint_repository(repo, selection=selected)
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="abc123",
        source_fingerprint=source.value,
        source_selection=selected,
        languages=["python"],
        file_count=source.file_count,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.save(manifest_path)
    return manifest


def _repo_entry(repo, manifest_path, *, instance_id="owner__repo-1"):
    return RepoEntry(
        instance_id=instance_id,
        repo="owner/repo",
        base_commit="abc123",
        language="python",
        repo_dir=str(repo),
        manifest_path=str(manifest_path),
    )


def _hostile_registry_id(registry, touched):
    class HostileId(str):
        def __hash__(self):
            touched.append("hash")
            registry.close()
            return str.__hash__(self)

        def __eq__(self, other):
            touched.append("eq")
            registry.close()
            return str.__eq__(self, other)

        def __str__(self):
            touched.append("str")
            registry.close()
            return str.__str__(self)

        def __bool__(self):
            touched.append("bool")
            registry.close()
            return True

    return HostileId("repo")


def _legacy_view_manifest(
    index_type,
    path,
    *,
    status="fresh",
    view_commit="",
    manifest_commit="",
    source_fingerprint="",
    config=None,
    metadata=None,
):
    entry = IndexEntry(
        index_type=index_type,
        path=path,
        built_at="2026-08-24T00:00:00Z",
        built_at_epoch=0.0,
        status=status,
        config=dict(config or {}),
        metadata=dict(metadata or {}),
        commit=view_commit,
        source_fingerprint=source_fingerprint,
    )
    return RepoManifest(
        version="1.1",
        source_selection=None,
        commit=manifest_commit,
        source_fingerprint=source_fingerprint,
        indexes={index_type: entry},
    )


@pytest.fixture
def native_authorization(monkeypatch):
    token = object()

    def require_view(root, authorization, semantic_contract):
        assert root
        assert authorization is token
        assert isinstance(semantic_contract, dict)

    monkeypatch.setattr(
        "codenib.index.embedding.artifact_integrity.require_authorized_vector_view",
        require_view,
    )
    return token


def test_fresh_registry_is_isolated_from_singleton():
    singleton = SkillRegistry()
    reg_a = _fresh_registry()
    reg_b = _fresh_registry()

    assert reg_a is not singleton
    assert reg_b is not singleton
    assert reg_a is not reg_b
    assert reg_a._skills is not reg_b._skills
    assert reg_a._skills is not singleton._skills


def test_ask_prompt_requires_resolving_discovered_identifiers():
    assert "targeted search" in _DEMO_SYSTEM_PROMPT
    assert "exact identifier and defining file" in _DEMO_SYSTEM_PROMPT
    assert "unresolved candidate identifier" in _DEMO_SYSTEM_PROMPT
    assert "combine every unresolved identifier" in _DEMO_SYSTEM_PROMPT
    assert "under 500 words" in _DEMO_SYSTEM_PROMPT


def test_registry_retains_manifest_selected_source_until_close(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "runtime.py").write_text("visible\n", encoding="utf-8")
    (repo / "private").mkdir()
    (repo / "private" / "secret.py").write_text("secret\n", encoding="utf-8")
    manifest_path = tmp_path / "artifacts" / "repo_manifest.json"
    _write_source_manifest(
        repo,
        manifest_path,
        selection=RepositorySourceSelection(("private",)),
    )
    entry = _repo_entry(repo, manifest_path)
    registry = RepoRegistry(QAConfig())

    bundle = registry._load_repo_metadata(entry)
    registry._bundles[entry.instance_id] = bundle
    reader = bundle.source_reader

    assert reader is not None
    assert reader.file_paths == frozenset({"src/runtime.py"})
    assert entry.instance_id in registry._source_bindings
    registry.close()

    assert bundle.source_reader is None
    with pytest.raises(RuntimeError, match="source binding is"):
        reader.read_prefix("src/runtime.py", max_bytes=32)


def test_registry_closes_captured_source_when_manifest_verification_fails(
    tmp_path, monkeypatch
):
    from codenib.compiler import manifest_source

    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "runtime.py"
    source.write_text("before\n", encoding="utf-8")
    manifest_path = tmp_path / "artifacts" / "repo_manifest.json"
    _write_source_manifest(repo, manifest_path)
    source.write_text("after\n", encoding="utf-8")
    entry = _repo_entry(repo, manifest_path)
    registry = RepoRegistry(QAConfig())
    captured = {}
    original_capture = manifest_source.capture_repository_source_for_manifest

    def observe_capture(*args, **kwargs):
        binding = original_capture(*args, **kwargs)
        captured["binding"] = binding
        return binding

    monkeypatch.setattr(
        manifest_source,
        "capture_repository_source_for_manifest",
        observe_capture,
    )

    with pytest.raises(ValueError, match="does not match the manifest"):
        registry._load_repo_metadata(entry)

    assert captured["binding"].closed is True
    assert registry._source_bindings == {}
    assert registry._source_cleanup_owners == {}


def test_registry_retains_failed_source_cleanup_for_retry(tmp_path, monkeypatch):
    from codenib.compiler import manifest_source

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("runtime\n", encoding="utf-8")
    manifest_path = tmp_path / "artifacts" / "repo_manifest.json"
    _write_source_manifest(repo, manifest_path)
    entry = _repo_entry(repo, manifest_path)
    registry = RepoRegistry(QAConfig())

    class Binding:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls >= 2

        def authenticated_identity_snapshot(self):
            raise ValueError("verification failed")

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("cleanup interrupted")

    binding = Binding()

    def capture(*_args, _source_owner, **_kwargs):
        _source_owner(binding)
        return binding

    monkeypatch.setattr(
        manifest_source,
        "capture_repository_source_for_manifest",
        capture,
    )

    with pytest.raises(ValueError, match="verification failed"):
        registry._load_repo_metadata(entry)

    assert set(registry._source_cleanup_owners) == {entry.instance_id}
    assert binding.close_calls == 1

    registry.close()

    assert binding.close_calls == 2
    assert registry._source_cleanup_owners == {}


def test_registry_later_publish_preserves_failed_owner_for_retry():
    class Owner:
        def __init__(self, *, fail_first=False):
            self.close_calls = 0
            self.fail_first = fail_first

        @property
        def closed(self):
            return self.close_calls >= (2 if self.fail_first else 1)

        def close(self):
            self.close_calls += 1
            if self.fail_first and self.close_calls == 1:
                raise RuntimeError("retry old owner")

    failed_owner = Owner(fail_first=True)
    published_owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["repo"] = failed_owner

    registry._publish_owned(
        "repo",
        _OwnedRepoBundle(bundle, None, published_owner),
    )

    assert registry.get("repo") is bundle
    assert registry._source_cleanup_owners["repo"] is published_owner
    assert registry._orphan_cleanup_owners == [failed_owner]
    assert failed_owner.close_calls == 1

    registry.close()

    assert failed_owner.close_calls == 2
    assert published_owner.close_calls == 1
    assert registry._orphan_cleanup_owners == []


def test_registry_complete_snapshot_retires_owner_without_bundle(monkeypatch):
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["removed"] = owner
    monkeypatch.setattr("codenib.web.repo_registry.load_registry", lambda _path: [])

    registry.load_all()

    assert owner.close_calls == 1
    assert registry._source_cleanup_owners == {}
    assert registry._orphan_cleanup_owners == []
    registry.close()
    assert owner.close_calls == 1


def test_registry_complete_snapshot_retries_failed_owner_cleanup(monkeypatch):
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls >= 2

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("retry removed owner")

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["removed"] = owner
    monkeypatch.setattr("codenib.web.repo_registry.load_registry", lambda _path: [])

    registry.load_all()
    assert registry._source_cleanup_owners == {}
    assert registry._orphan_cleanup_owners == [owner]

    registry.load_all()
    assert owner.close_calls == 2
    assert registry._orphan_cleanup_owners == []


def test_registry_absent_owner_transfer_interruption_keeps_authority():
    stop = KeyboardInterrupt("absent owner transfer interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["removed"] = owner
    source, first_line = inspect.getsourcelines(
        RepoRegistry._retire_entries_absent_from
    )
    transfer_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._orphan_cleanup_owners.append(cleanup_owner)" in line
    )
    triggered = False

    def interrupt_after_transfer(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry._retire_entries_absent_from.__code__
            and frame.f_lineno > transfer_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_transfer

    sys.settrace(interrupt_after_transfer)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            registry._retire_entries_absent_from(set())
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert owner.close_calls == 0
    assert owner in registry._orphan_cleanup_owners

    registry._retire_entries_absent_from(set())
    assert owner.close_calls == 1
    assert registry._source_cleanup_owners == {}
    assert registry._orphan_cleanup_owners == []


def test_registry_complete_snapshot_reports_reentrant_cleanup_shutdown():
    registry = RepoRegistry(QAConfig())

    class Owner:
        def __init__(self, *, close_registry=False):
            self.close_calls = 0
            self.close_registry = close_registry

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            if self.close_registry:
                registry.close()

    keep = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    removed = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    keep_owner = Owner()
    removed_owner = Owner(close_registry=True)
    registry._bundles.update({"keep": keep, "removed": removed})
    registry._source_cleanup_owners.update(
        {"keep": keep_owner, "removed": removed_owner}
    )

    with pytest.raises(RuntimeError, match="repository registry is closed"):
        registry._retire_entries_absent_from({"keep"})

    assert registry._closed is True
    assert registry.get("keep") is None
    assert removed_owner.close_calls == 1
    assert keep_owner.close_calls == 1
    assert registry._source_cleanup_owners == {}
    assert registry._retired_bundles == {}


def test_registry_load_all_reports_reentrant_build_shutdown(tmp_path, monkeypatch):
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )

    def build(_entry):
        registry.close()
        return _OwnedRepoBundle(candidate, None, owner)

    monkeypatch.setattr(registry, "_build_repo_metadata", build)

    with pytest.raises(RuntimeError, match="repository registry is closed"):
        registry.load_all()

    assert registry._closed is True
    assert owner.close_calls == 1
    assert registry._bundles == {}
    assert registry._source_cleanup_owners == {}
    assert registry._retired_bundles == {}


def test_registry_load_all_preserves_reentrant_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")
    cleanup_failure = RuntimeError("old generation cleanup failed")

    class Owner:
        def __init__(self, *, reentrant_failure=None):
            self.close_calls = 0
            self.reentrant_failure = reentrant_failure

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            if self.reentrant_failure is not None:
                registry.close()
                raise self.reentrant_failure

    old_owner = Owner(reentrant_failure=cleanup_failure)
    candidate_owner = Owner()
    old = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = old_owner
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, candidate_owner),
    )
    monkeypatch.setattr(registry, "_prepare_runtime_bundle", lambda _bundle: None)

    with pytest.raises(RuntimeError, match="repository registry is closed") as caught:
        registry.load_all()

    assert caught.value.__cause__ is cleanup_failure
    assert registry._closed is True
    assert old_owner.close_calls == 1
    assert candidate_owner.close_calls == 1
    assert registry._bundles == {}
    assert registry._source_cleanup_owners == {}
    assert registry._retired_bundles == {}


def test_registry_load_all_prepares_only_live_replacements(tmp_path, monkeypatch):
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")

    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    old = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    old_owner = Owner()
    candidate_owner = Owner()
    built = iter(
        (
            _OwnedRepoBundle(old, None, old_owner),
            _OwnedRepoBundle(candidate, None, candidate_owner),
        )
    )
    prepared = []
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(registry, "_build_repo_metadata", lambda _entry: next(built))

    def prepare(bundle):
        prepared.append(bundle)
        raise ValueError("candidate views are invalid")

    monkeypatch.setattr(registry, "_prepare_runtime_bundle", prepare)

    registry.load_all()
    assert registry.get("repo") is old
    assert prepared == []

    registry.load_all()

    assert prepared == [candidate]
    assert registry.get("repo") is old
    assert old_owner.closed is False
    assert candidate_owner.closed is True
    registry.close()
    assert old_owner.closed is True


def test_registry_load_all_detaches_hostile_entry_key(tmp_path, monkeypatch):
    touched = []
    registry = RepoRegistry(QAConfig())
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(
        tmp_path,
        manifest_path,
        instance_id=_hostile_registry_id(registry, touched),
    )
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, None),
    )

    registry.load_all()

    assert touched == []
    assert registry._closed is False
    assert registry.get("repo") is candidate
    registry.close()


def test_registry_close_waits_for_inflight_candidate_build(tmp_path, monkeypatch):
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")
    build_started = Event()
    release_build = Event()
    close_started = Event()
    close_finished = Event()

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )

    def build(_entry):
        build_started.set()
        assert release_build.wait(5)
        return _OwnedRepoBundle(candidate, None, owner)

    def close_registry():
        close_started.set()
        try:
            registry.close()
        finally:
            close_finished.set()

    monkeypatch.setattr(registry, "_build_repo_metadata", build)
    monkeypatch.setattr(registry, "_prepare_runtime_bundle", lambda _bundle: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh_future = pool.submit(registry.refresh, "repo")
        assert build_started.wait(5)
        close_future = pool.submit(close_registry)
        assert close_started.wait(5)
        assert close_finished.wait(0.1) is False
        release_build.set()
        refresh_future.result(timeout=5)
        close_future.result(timeout=5)

    assert registry.get("repo") is None
    assert owner.close_calls == 1


def test_registry_close_waits_for_legacy_metadata_build(monkeypatch):
    build_started = Event()
    release_build = Event()
    close_finished = Event()

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())

    def build(_entry):
        build_started.set()
        assert release_build.wait(5)
        return _OwnedRepoBundle(candidate, None, owner)

    def close_registry():
        try:
            registry.close()
        finally:
            close_finished.set()

    monkeypatch.setattr(registry, "_build_repo_metadata", build)

    with ThreadPoolExecutor(max_workers=2) as pool:
        load = pool.submit(
            registry._load_repo_metadata,
            SimpleNamespace(instance_id="repo"),
        )
        assert build_started.wait(5)
        closing = pool.submit(close_registry)
        assert close_finished.wait(0.1) is False
        release_build.set()
        assert load.result(timeout=5) is candidate
        closing.result(timeout=5)

    assert owner.close_calls == 1
    assert registry._source_cleanup_owners == {}


def test_registry_legacy_metadata_rejects_reentrant_close(monkeypatch):
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())

    def build(_entry):
        registry.close()
        return _OwnedRepoBundle(candidate, None, owner)

    monkeypatch.setattr(registry, "_build_repo_metadata", build)

    with pytest.raises(RuntimeError, match="repository registry is closed"):
        registry._load_repo_metadata(SimpleNamespace(instance_id="repo"))

    assert owner.close_calls == 1
    assert registry._source_bindings == {}
    assert registry._source_cleanup_owners == {}
    assert registry._retired_bundles == {}


def test_registry_legacy_metadata_detaches_hostile_string_key(monkeypatch):
    hash_calls = 0

    class ReentrantId(str):
        def __hash__(self):
            nonlocal hash_calls
            hash_calls += 1
            registry.close()
            return str.__hash__(self)

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, owner),
    )

    result = registry._load_repo_metadata(
        SimpleNamespace(instance_id=ReentrantId("repo"))
    )

    assert result is candidate
    assert hash_calls == 0
    assert registry._closed is False
    assert registry._source_cleanup_owners == {"repo": owner}

    registry.close()
    assert owner.close_calls == 1


def test_registry_concurrent_close_closes_owner_once():
    close_entered = Event()
    release_close = Event()

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            close_entered.set()
            assert release_close.wait(5)

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["repo"] = owner

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(registry.close)
        assert close_entered.wait(5)
        second = pool.submit(registry.close)
        assert second.done() is False
        release_close.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert owner.close_calls == 1


def test_registry_reentrant_close_claims_owner_only_once():
    stop = SystemExit("owner cleanup stopped")
    registry = RepoRegistry(QAConfig())

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            registry.close()
            raise stop

    owner = Owner()
    registry._source_cleanup_owners["repo"] = owner

    with pytest.raises(SystemExit) as caught:
        registry.close()

    assert caught.value is stop
    assert owner.close_calls == 1
    assert registry._source_cleanup_owners == {}
    assert registry._orphan_cleanup_owners == []

    registry.close()
    assert owner.close_calls == 1


def test_registry_close_owner_transfer_interruption_keeps_authority():
    stop = KeyboardInterrupt("owner transfer interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["repo"] = owner
    source, first_line = inspect.getsourcelines(RepoRegistry.close)
    transfer_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._orphan_cleanup_owners.append(cleanup_owner)" in line
    )
    triggered = False

    def interrupt_after_transfer(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry.close.__code__
            and frame.f_lineno > transfer_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_transfer

    sys.settrace(interrupt_after_transfer)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            registry.close()
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert owner.close_calls == 0
    assert owner in registry._orphan_cleanup_owners

    registry.close()
    assert owner.close_calls == 1
    assert registry._source_cleanup_owners == {}
    assert registry._orphan_cleanup_owners == []


def test_registry_cleanup_settles_closed_owner_before_raising():
    stop = KeyboardInterrupt("owner cleanup stopped")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            raise stop

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner

    with pytest.raises(KeyboardInterrupt) as caught:
        registry.close()

    assert caught.value is stop
    assert owner.close_calls == 1
    assert registry._retired_bundles == {}

    registry.close()
    assert owner.close_calls == 1


def test_registry_refresh_cannot_deadlock_reentrant_retired_cleanup(monkeypatch):
    cleanup_entered = Event()
    refresh_started = Event()

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            cleanup_entered.set()
            assert refresh_started.wait(5)
            registry.close()

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._retired_bundles[id(bundle)] = _OwnedRepoBundle(
        bundle,
        None,
        owner,
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: pytest.fail("closed refresh must not read the registry"),
    )

    def refresh():
        refresh_started.set()
        with pytest.raises(RuntimeError, match="repository registry is closed"):
            registry.refresh("repo")

    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup_future = pool.submit(registry._drain_retired)
        assert cleanup_entered.wait(5)
        refresh_future = pool.submit(refresh)
        assert cleanup_future.result(timeout=5) is None
        refresh_future.result(timeout=5)

    assert owner.close_calls == 1
    assert registry._closed is True
    assert registry._retired_bundles == {}


def test_registry_vector_close_settles_before_opcode_interruption():
    opcode_events = []

    def probe():
        return None

    def probe_trace(frame, event, _arg):
        if frame.f_code is probe.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode":
                opcode_events.append(frame.f_lasti)
        return probe_trace

    sys.settrace(probe_trace)
    try:
        probe()
    finally:
        sys.settrace(None)
    if not opcode_events:
        pytest.skip("this interpreter does not emit opcode trace events")

    stop = KeyboardInterrupt("vector settlement interrupted")

    class Vector:
        def __init__(self):
            self.close_calls = 0
            self.done = False

        @property
        def closed(self):
            return self.done

        def close(self):
            self.close_calls += 1
            if self.close_calls > 1:
                raise RuntimeError("vector closed twice")
            self.done = True

    vector = Vector()
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=vector,
    )
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    instructions = tuple(dis.get_instructions(RepoRegistry._close_owned))
    close_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == "close"
    )
    call_index = next(
        index
        for index in range(close_index + 1, len(instructions))
        if "CALL" in instructions[index].opname
    )
    interrupt_offset = instructions[call_index + 1].offset
    triggered = False

    def interrupt_after_close(frame, event, _arg):
        nonlocal triggered
        if frame.f_code is RepoRegistry._close_owned.__code__:
            frame.f_trace_opcodes = True
            if (
                not triggered
                and event == "opcode"
                and frame.f_lasti == interrupt_offset
            ):
                triggered = True
                sys.settrace(None)
                raise stop
        return interrupt_after_close

    sys.settrace(interrupt_after_close)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            registry.close()
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert vector.close_calls == 1
    assert bundle.vector_store is None
    assert registry._retired_bundles == {}

    registry.close()
    assert vector.close_calls == 1


def test_registry_vector_close_reconciles_completed_failure():
    stop = KeyboardInterrupt("vector close completed while interrupted")

    class Vector:
        def __init__(self):
            self.close_calls = 0
            self.done = False

        @property
        def closed(self):
            return self.done

        def close(self):
            self.close_calls += 1
            self.done = True
            raise stop

    vector = Vector()
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=vector,
    )
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle

    with pytest.raises(KeyboardInterrupt) as caught:
        registry.close()

    assert caught.value is stop
    assert vector.close_calls == 1
    assert bundle.vector_store is None
    assert registry._retired_bundles == {}

    registry.close()
    assert vector.close_calls == 1


def test_registry_cleanup_deduplicates_generation_and_orphan_owner():
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner
    registry._orphan_cleanup_owners.append(owner)

    registry.close()

    assert owner.close_calls == 1
    assert registry._retired_bundles == {}
    assert registry._orphan_cleanup_owners == []


def test_registry_final_lease_reconciles_orphan_owner_alias():
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner
    registry._orphan_cleanup_owners.append(owner)

    with registry.pin("repo"):
        registry.close()
        assert owner.close_calls == 0
        assert registry.retired_generation_count == 1
        assert registry._orphan_cleanup_owners == [owner]

    assert owner.close_calls == 1
    assert registry._retired_bundles == {}
    assert registry._orphan_cleanup_owners == []


def test_registry_shared_pending_owner_runs_once_per_cleanup_settlement():
    first_failure = RuntimeError("first cleanup failed")
    later_stop = SystemExit("later cleanup stopped")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return False

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise first_failure
            raise later_stop

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    for repo_id in ("one", "two"):
        registry._bundles[repo_id] = RepoBundle(
            entry=SimpleNamespace(),
            manifest=SimpleNamespace(),
        )
        registry._source_cleanup_owners[repo_id] = owner

    with pytest.raises(RuntimeError) as caught:
        registry.close()

    assert caught.value is first_failure
    assert owner.close_calls == 1
    assert registry.retired_generation_count == 1

    with pytest.raises(SystemExit) as caught:
        registry.close()
    assert caught.value is later_stop
    assert owner.close_calls == 2


def test_registry_shared_owner_still_closes_each_bundle_vector():
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    class Vector:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    vectors = [Vector(), Vector()]
    registry = RepoRegistry(QAConfig())
    for repo_id, vector in zip(("one", "two"), vectors, strict=True):
        registry._bundles[repo_id] = RepoBundle(
            entry=SimpleNamespace(),
            manifest=SimpleNamespace(),
            vector_store=vector,
        )
        registry._source_cleanup_owners[repo_id] = owner

    registry.close()

    assert owner.close_calls == 1
    assert [vector.close_calls for vector in vectors] == [1, 1]
    assert registry._retired_bundles == {}


def test_registry_shared_owner_waits_for_every_leased_alias():
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    class Vector:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    vectors = [Vector(), Vector()]
    bundles = [
        RepoBundle(
            entry=SimpleNamespace(),
            manifest=SimpleNamespace(),
            vector_store=vector,
        )
        for vector in vectors
    ]
    registry = RepoRegistry(QAConfig())
    for repo_id, bundle in zip(("one", "two"), bundles, strict=True):
        registry._bundles[repo_id] = bundle
        registry._source_cleanup_owners[repo_id] = owner

    with registry.pin("one") as pinned:
        registry.close()
        assert pinned is bundles[0]
        assert owner.close_calls == 0
        assert [vector.close_calls for vector in vectors] == [0, 1]
        assert registry.retired_generation_count == 1

    assert owner.close_calls == 1
    assert [vector.close_calls for vector in vectors] == [1, 1]
    assert registry._retired_bundles == {}


def test_registry_publish_does_not_close_active_shared_owner():
    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    class Vector:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    vector = Vector()
    old = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=vector,
    )
    new = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = owner

    registry._publish_owned("repo", _OwnedRepoBundle(new, None, owner))

    assert registry.get("repo") is new
    assert owner.close_calls == 0
    assert vector.close_calls == 1
    assert registry._retired_bundles == {}

    registry.close()
    assert owner.close_calls == 1


def test_registry_retired_claim_interruption_keeps_retry_state():
    stop = KeyboardInterrupt("retired claim interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    key = id(bundle)
    registry._retired_bundles[key] = _OwnedRepoBundle(bundle, None, owner)
    source, first_line = inspect.getsourcelines(RepoRegistry._drain_retired)
    claim_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._cleanup_in_progress[key] = (" in line
    )
    triggered = False

    def interrupt_after_claim(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry._drain_retired.__code__
            and frame.f_lineno > claim_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_claim

    sys.settrace(interrupt_after_claim)
    try:
        assert registry._drain_retired() is stop
    finally:
        sys.settrace(None)

    assert triggered is True
    assert owner.close_calls == 0
    assert registry._cleanup_in_progress == {}
    assert registry._orphan_cleanup_in_progress == set()
    assert registry._retired_bundles[key].source_cleanup_owner is owner

    assert registry._drain_retired() is None
    assert owner.close_calls == 1
    assert registry._retired_bundles == {}


def test_registry_orphan_claim_interruption_keeps_retry_state():
    stop = KeyboardInterrupt("orphan claim interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._orphan_cleanup_owners.append(owner)
    source, first_line = inspect.getsourcelines(RepoRegistry._drain_orphan_cleanup)
    claim_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._orphan_cleanup_in_progress.add(authority_id)" in line
    )
    triggered = False

    def interrupt_after_claim(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry._drain_orphan_cleanup.__code__
            and frame.f_lineno > claim_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_claim

    sys.settrace(interrupt_after_claim)
    try:
        assert registry._drain_orphan_cleanup() is stop
    finally:
        sys.settrace(None)

    assert triggered is True
    assert owner.close_calls == 0
    assert registry._orphan_cleanup_in_progress == set()
    assert registry._orphan_cleanup_owners == [owner]

    assert registry._drain_orphan_cleanup() is None
    assert owner.close_calls == 1
    assert registry._orphan_cleanup_owners == []


def test_registry_close_waits_for_an_inflight_cleanup_drain():
    cleanup_started = Event()
    release_cleanup = Event()
    close_finished = Event()

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            cleanup_started.set()
            assert release_cleanup.wait(5)

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._orphan_cleanup_owners.append(owner)

    def close_registry():
        try:
            registry.close()
        finally:
            close_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        drain = pool.submit(registry._drain_orphan_cleanup)
        assert cleanup_started.wait(5)
        closing = pool.submit(close_registry)
        assert close_finished.wait(0.1) is False
        release_cleanup.set()
        assert drain.result(timeout=5) is None
        closing.result(timeout=5)

    assert owner.close_calls == 1


def test_registry_cleanup_cancellation_precedes_ordinary_failure():
    ordinary = RuntimeError("vector cleanup failed")
    stop = SystemExit("source cleanup stopped")

    class Vector:
        def close(self):
            raise ordinary

    class Owner:
        @property
        def closed(self):
            return False

        def close(self):
            raise stop

    owned = _OwnedRepoBundle(
        RepoBundle(
            entry=SimpleNamespace(),
            manifest=SimpleNamespace(),
            vector_store=Vector(),
        ),
        None,
        Owner(),
    )

    with pytest.raises(SystemExit) as observed:
        RepoRegistry._close_owned(owned)

    assert observed.value is stop
    notes = getattr(stop, "__notes__", ()) or getattr(
        stop,
        "_codenib_cleanup_notes",
        (),
    )
    assert any("earlier repository cleanup" in note for note in notes)


def test_registry_unpublished_cleanup_cancellation_precedes_primary(monkeypatch):
    primary = ValueError("candidate invalid")
    stop = KeyboardInterrupt("cleanup stopped")

    class Owner:
        @property
        def closed(self):
            return False

        def close(self):
            raise stop

    owned = _OwnedRepoBundle(
        RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace()),
        None,
        Owner(),
    )
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(registry, "_build_repo_metadata", lambda _entry: owned)

    def reject(_bundle):
        raise primary

    monkeypatch.setattr(registry, "_prepare_runtime_bundle", reject)

    with pytest.raises(KeyboardInterrupt) as observed:
        registry._replace_entry(
            SimpleNamespace(instance_id="repo"), prepare_runtime=True
        )

    assert observed.value is stop
    assert observed.value.__cause__ is primary
    assert registry.retired_generation_count == 1


def test_registry_primary_cancellation_precedes_ordinary_cleanup(monkeypatch):
    primary = SystemExit("candidate stopped")
    cleanup = RuntimeError("cleanup failed")

    class Owner:
        @property
        def closed(self):
            return False

        def close(self):
            raise cleanup

    owned = _OwnedRepoBundle(
        RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace()),
        None,
        Owner(),
    )
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(registry, "_build_repo_metadata", lambda _entry: owned)

    def stop(_bundle):
        raise primary

    monkeypatch.setattr(registry, "_prepare_runtime_bundle", stop)

    with pytest.raises(SystemExit) as observed:
        registry._replace_entry(
            SimpleNamespace(instance_id="repo"), prepare_runtime=True
        )

    assert observed.value is primary
    assert observed.value.__cause__ is cleanup


def test_registry_cleanup_does_not_create_self_cause(monkeypatch):
    stop = SystemExit("shared cancellation")

    class Owner:
        @property
        def closed(self):
            return False

        def close(self):
            raise stop

    owned = _OwnedRepoBundle(
        RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace()),
        None,
        Owner(),
    )
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(registry, "_build_repo_metadata", lambda _entry: owned)
    monkeypatch.setattr(
        registry,
        "_prepare_runtime_bundle",
        lambda _bundle: (_ for _ in ()).throw(stop),
    )

    with pytest.raises(SystemExit) as observed:
        registry._replace_entry(
            SimpleNamespace(instance_id="repo"), prepare_runtime=True
        )

    assert observed.value is stop
    assert observed.value.__cause__ is not stop


def test_registry_hostile_cleanup_attribute_cannot_replace_primary(monkeypatch):
    lookup = KeyboardInterrupt("hostile attribute lookup")

    class HostileExit(SystemExit):
        def __getattribute__(self, name):
            if name == "source_cleanup_owner":
                raise lookup
            return super().__getattribute__(name)

    primary = HostileExit("primary")
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: (_ for _ in ()).throw(primary),
    )

    with pytest.raises(SystemExit) as observed:
        registry._replace_entry(
            SimpleNamespace(instance_id="repo"), prepare_runtime=False
        )

    assert observed.value is primary


def test_registry_hostile_cleanup_traceback_cannot_replace_publication():
    lookup = KeyboardInterrupt("hostile traceback lookup")

    class HostileFailure(RuntimeError):
        def __getattribute__(self, name):
            if name == "__traceback__":
                raise lookup
            return super().__getattribute__(name)

    failure = HostileFailure("cleanup remains pending")

    class Owner:
        @property
        def closed(self):
            return False

        def close(self):
            raise failure

    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._source_cleanup_owners["repo"] = Owner()

    registry._publish_owned(
        "repo",
        _OwnedRepoBundle(candidate, None, None),
    )

    assert registry.get("repo") is candidate
    assert len(registry._orphan_cleanup_owners) == 1


def test_registry_reload_closes_the_previous_source_authority(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("runtime\n", encoding="utf-8")
    manifest_path = tmp_path / "artifacts" / "repo_manifest.json"
    _write_source_manifest(repo, manifest_path)
    entry = _repo_entry(repo, manifest_path)
    config = QAConfig(data_dir=str(tmp_path / "data"))
    save_registry(config.registry_path, [entry])
    registry = RepoRegistry(config)

    registry.load_all()
    first_reader = registry.get(entry.instance_id).source_reader
    registry.load_all()
    second_reader = registry.get(entry.instance_id).source_reader

    assert first_reader is not second_reader
    with pytest.raises(RuntimeError, match="source binding is"):
        first_reader.read_prefix("runtime.py", max_bytes=32)
    assert second_reader.read_prefix("runtime.py", max_bytes=32) == b"runtime\n"
    registry.close()


def test_registry_reload_keeps_pinned_source_authority_until_release(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("runtime\n", encoding="utf-8")
    manifest_path = tmp_path / "artifacts" / "repo_manifest.json"
    _write_source_manifest(repo, manifest_path)
    entry = _repo_entry(repo, manifest_path)
    config = QAConfig(data_dir=str(tmp_path / "data"))
    save_registry(config.registry_path, [entry])
    registry = RepoRegistry(config)
    registry.load_all()

    with registry.pin(entry.instance_id) as first_bundle:
        first_reader = first_bundle.source_reader
        registry.load_all()

        assert registry.get(entry.instance_id) is not first_bundle
        assert first_reader.read_prefix("runtime.py", max_bytes=32) == b"runtime\n"
        assert registry.retired_generation_count == 1

    with pytest.raises(RuntimeError, match="source binding is"):
        first_reader.read_prefix("runtime.py", max_bytes=32)
    assert registry.retired_generation_count == 0
    registry.close()


def test_registry_reload_retires_entry_removed_from_snapshot(monkeypatch):
    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._bundles["removed"] = bundle
    registry._source_cleanup_owners["removed"] = owner
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [],
    )

    with registry.pin("removed") as pinned:
        registry.load_all()

        assert pinned is bundle
        assert registry.get("removed") is None
        assert owner.closed is False
        assert registry.retired_generation_count == 1

    assert owner.closed is True
    assert registry.retired_generation_count == 0
    registry.close()


def test_registry_reload_keeps_failed_declared_entry_and_retires_absent_entry(
    tmp_path,
    monkeypatch,
):
    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    declared_manifest = tmp_path / "declared.json"
    declared_manifest.write_text("{}", encoding="utf-8")
    declared = _repo_entry(
        tmp_path,
        declared_manifest,
        instance_id="declared",
    )
    owners = {}
    registry = RepoRegistry(QAConfig())
    for instance_id in ("declared", "absent"):
        bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
        owner = Owner()
        registry._bundles[instance_id] = bundle
        registry._source_cleanup_owners[instance_id] = owner
        owners[instance_id] = owner

    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [declared],
    )

    def reject(_entry, *, prepare_runtime):
        assert prepare_runtime is True
        raise ValueError("replacement rejected")

    monkeypatch.setattr(registry, "_replace_entry", reject)

    registry.load_all()

    assert registry.get("declared") is not None
    assert owners["declared"].closed is False
    assert registry.get("absent") is None
    assert owners["absent"].closed is True
    registry.close()


def test_registry_reload_serializes_refresh_after_snapshot_reconciliation(
    tmp_path,
    monkeypatch,
):
    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    owner = Owner()
    first_snapshot_read = Event()
    release_first_snapshot = Event()
    refresh_started = Event()
    second_snapshot_read = Event()
    call_lock = Lock()
    calls = 0

    def load_rows(_path):
        nonlocal calls
        with call_lock:
            calls += 1
            call = calls
        if call == 1:
            first_snapshot_read.set()
            assert release_first_snapshot.wait(5)
            return []
        assert call == 2
        second_snapshot_read.set()
        return [entry]

    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr("codenib.web.repo_registry.load_registry", load_rows)
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, owner),
    )
    monkeypatch.setattr(registry, "_prepare_runtime_bundle", lambda _bundle: None)

    def refresh():
        refresh_started.set()
        registry.refresh("repo")

    with ThreadPoolExecutor(max_workers=2) as pool:
        load_future = pool.submit(registry.load_all)
        assert first_snapshot_read.wait(5)
        refresh_future = pool.submit(refresh)
        assert refresh_started.wait(5)
        assert second_snapshot_read.wait(0.05) is False
        release_first_snapshot.set()
        load_future.result(timeout=5)
        refresh_future.result(timeout=5)

    assert second_snapshot_read.is_set()
    assert registry.get("repo") is candidate
    registry.close()
    assert owner.closed is True


def test_registry_rejects_duplicate_instance_ids_without_replacing_owner(tmp_path):
    entries = []
    for name in ("first", "second"):
        repo = tmp_path / name
        repo.mkdir()
        (repo / f"{name}.py").write_text(f"{name}\n", encoding="utf-8")
        manifest_path = tmp_path / "artifacts" / name / "repo_manifest.json"
        _write_source_manifest(repo, manifest_path)
        entries.append(_repo_entry(repo, manifest_path, instance_id="duplicate"))
    config = QAConfig(data_dir=str(tmp_path / "data"))
    save_registry(config.registry_path, entries)
    registry = RepoRegistry(config)

    registry.load_all()
    bundle = registry.get("duplicate")

    assert bundle.source_reader.file_paths == frozenset({"first.py"})
    assert len(registry._source_cleanup_owners) == 1
    registry.close()


def test_registry_retains_vector_cleanup_owner_for_retry():
    class Vector:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("retry cleanup")

    vector = Vector()
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=vector,
    )
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle

    with pytest.raises(RuntimeError, match="retry cleanup"):
        registry.close()

    assert registry._bundles == {}
    assert set(registry._retired_bundles) == {id(bundle)}
    assert bundle.vector_store is vector

    registry.close()

    assert vector.close_calls == 2
    assert registry._bundles == {}
    assert registry._retired_bundles == {}


def test_registry_swap_keeps_pinned_generation_alive_until_release():
    events = []

    class Vector:
        def close(self):
            events.append("old-vector-closed")

    class Owner:
        def __init__(self, label):
            self.label = label
            self.closed = False

        def close(self):
            self.closed = True
            events.append(f"{self.label}-source-closed")

    old_vector = Vector()
    old = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=old_vector,
    )
    new = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    old_owner = Owner("old")
    new_owner = Owner("new")
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = old_owner

    with registry.pin("repo") as pinned:
        registry._publish_owned(
            "repo",
            _OwnedRepoBundle(new, None, new_owner),
        )

        assert pinned is old
        assert registry.get("repo") is new
        assert old.vector_store is old_vector
        assert old_owner.closed is False
        assert registry.retired_generation_count == 1
        assert events == []

    assert old.vector_store is None
    assert old_owner.closed is True
    assert registry.retired_generation_count == 0
    assert events == ["old-vector-closed", "old-source-closed"]
    registry.close()


def test_registry_queries_detach_hostile_lookup_key():
    touched = []
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    key = _hostile_registry_id(registry, touched)

    assert registry.get(key) is bundle
    with registry.pin(key) as pinned:
        assert pinned is bundle

    assert touched == []
    assert registry._closed is False
    registry.close()


@pytest.mark.parametrize("repo_id", ["", None])
def test_registry_closed_state_precedes_invalid_lookup_key(repo_id):
    registry = RepoRegistry(QAConfig())
    registry.close()

    with pytest.raises(RuntimeError, match="repository registry is closed"):
        registry.refresh(repo_id)
    with pytest.raises(RuntimeError, match="repository registry is closed"):
        with registry.pin(repo_id):
            pass


@pytest.mark.parametrize("repo_id", ["", None, 17])
def test_registry_open_lookup_preserves_legacy_miss_semantics(repo_id):
    registry = RepoRegistry(QAConfig())

    assert registry.get(repo_id) is None
    with registry.pin(repo_id) as pinned:
        assert pinned is None


def test_registry_rejects_fake_string_without_descriptor_dispatch():
    touched = []

    class Masquerade:
        @property
        def __class__(self):
            touched.append("class")
            registry.close()
            return str

    registry = RepoRegistry(QAConfig())
    key = Masquerade()

    assert registry.get(key) is None
    with registry.pin(key) as pinned:
        assert pinned is None
    with pytest.raises(ValueError, match="instance_id must be non-empty text"):
        registry.refresh(key)

    assert touched == []
    assert registry._closed is False


def test_registry_pin_acquisition_interruption_releases_lease():
    stop = KeyboardInterrupt("pin acquisition interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner
    pin_impl = RepoRegistry.pin.__wrapped__
    source, first_line = inspect.getsourcelines(pin_impl)
    acquire_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._bundle_leases[lease_token] = keys" in line
    )
    triggered = False

    def interrupt_after_acquire(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is pin_impl.__code__
            and frame.f_lineno > acquire_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_acquire

    context = registry.pin("repo")
    sys.settrace(interrupt_after_acquire)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            context.__enter__()
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert registry._bundle_leases == {}

    registry.close()
    assert owner.close_calls == 1
    assert registry._retired_bundles == {}


def test_registry_pin_all_acquisition_is_atomic_under_interruption():
    stop = KeyboardInterrupt("pin-all acquisition interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owners = [Owner(), Owner()]
    registry = RepoRegistry(QAConfig())
    for repo_id, owner in zip(("one", "two"), owners, strict=True):
        registry._bundles[repo_id] = RepoBundle(
            entry=SimpleNamespace(),
            manifest=SimpleNamespace(),
        )
        registry._source_cleanup_owners[repo_id] = owner
    pin_impl = RepoRegistry.pin_all.__wrapped__
    source, first_line = inspect.getsourcelines(pin_impl)
    acquire_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._bundle_leases[lease_token] = keys" in line
    )
    triggered = False

    def interrupt_after_acquire(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is pin_impl.__code__
            and frame.f_lineno > acquire_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_acquire

    context = registry.pin_all()
    sys.settrace(interrupt_after_acquire)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            context.__enter__()
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert registry._bundle_leases == {}

    registry.close()
    assert [owner.close_calls for owner in owners] == [1, 1]
    assert registry._retired_bundles == {}


def test_registry_pin_release_interruption_cannot_leave_stale_lease():
    stop = KeyboardInterrupt("pin release interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner
    context = registry.pin("repo")
    assert context.__enter__() is bundle
    registry.close()
    source, first_line = inspect.getsourcelines(RepoRegistry._drop_bundle_lease)
    release_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "keys = self._bundle_leases.pop(lease_token, ())" in line
    )
    triggered = False

    def interrupt_after_release(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry._drop_bundle_lease.__code__
            and frame.f_lineno > release_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_release

    sys.settrace(interrupt_after_release)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            context.__exit__(None, None, None)
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert registry._bundle_leases == {}
    assert owner.close_calls == 0
    assert registry.retired_generation_count == 1

    registry.close()
    assert owner.close_calls == 1
    assert registry._retired_bundles == {}


def test_registry_refresh_publishes_without_escaping_unleased_bundle(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(repo, manifest_path, instance_id="repo")
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())

    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    owner = Owner()
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, owner),
    )
    monkeypatch.setattr(registry, "_prepare_runtime_bundle", lambda _bundle: None)

    assert registry.refresh("repo") is None
    with registry.pin("repo") as pinned:
        assert pinned is candidate
    assert owner.closed is False

    registry.close()
    assert owner.closed is True


def test_registry_refresh_detaches_hostile_lookup_key(tmp_path, monkeypatch):
    touched = []
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, None),
    )
    monkeypatch.setattr(registry, "_prepare_runtime_bundle", lambda _bundle: None)

    registry.refresh(_hostile_registry_id(registry, touched))

    assert touched == []
    assert registry._closed is False
    assert registry.get("repo") is candidate
    registry.close()


def test_registry_refresh_reports_reentrant_cleanup_shutdown(tmp_path, monkeypatch):
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(tmp_path, manifest_path, instance_id="repo")
    old = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())

    class Owner:
        def __init__(self, *, close_registry=False):
            self.close_calls = 0
            self.close_registry = close_registry

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1
            if self.close_registry:
                registry.close()

    old_owner = Owner(close_registry=True)
    candidate_owner = Owner()
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = old_owner
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, candidate_owner),
    )
    monkeypatch.setattr(registry, "_prepare_runtime_bundle", lambda _bundle: None)

    with pytest.raises(RuntimeError, match="repository registry is closed"):
        registry.refresh("repo")

    assert registry._closed is True
    assert registry.get("repo") is None
    assert old_owner.close_calls == 1
    assert candidate_owner.close_calls == 1
    assert registry._source_cleanup_owners == {}
    assert registry._retired_bundles == {}


def test_registry_publication_interrupt_closes_candidate_once(monkeypatch):
    stop = KeyboardInterrupt("publication interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, owner),
    )
    publish_impl = RepoRegistry._publish_owned_under_cleanup_lock
    source, first_line = inspect.getsourcelines(publish_impl)
    assignment_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._bundles[instance_id] = owned.bundle" in line
    )
    triggered = False

    def interrupt_after_publication(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is publish_impl.__code__
            and frame.f_lineno > assignment_line
        ):
            triggered = True
            sys.settrace(None)
            registry.close()
            raise stop
        return interrupt_after_publication

    sys.settrace(interrupt_after_publication)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            registry._replace_entry(
                SimpleNamespace(instance_id="repo"),
                prepare_runtime=False,
            )
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert registry._closed is True
    assert owner.close_calls == 1
    assert registry._bundles == {}
    assert registry._source_cleanup_owners == {}
    assert registry._retired_bundles == {}


def test_registry_previous_retirement_interrupt_rolls_back_old_generation(
    monkeypatch,
):
    stop = KeyboardInterrupt("previous retirement interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    class Vector:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    old_owner = Owner()
    candidate_owner = Owner()
    vector = Vector()
    old = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=vector,
    )
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = old_owner
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, candidate_owner),
    )
    publish_impl = RepoRegistry._publish_owned_under_cleanup_lock
    source, first_line = inspect.getsourcelines(publish_impl)
    retirement_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._retired_bundles[id(previous.bundle)] = previous" in line
    )
    triggered = False

    def interrupt_after_retirement(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is publish_impl.__code__
            and frame.f_lineno > retirement_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_retirement

    sys.settrace(interrupt_after_retirement)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            registry._replace_entry(
                SimpleNamespace(instance_id="repo"),
                prepare_runtime=False,
            )
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert triggered is True
    assert registry.get("repo") is old
    assert old_owner.close_calls == 0
    assert vector.close_calls == 0
    assert candidate_owner.close_calls == 1
    assert registry._retired_bundles == {}

    with registry.pin("repo") as pinned:
        assert pinned is old
    assert old_owner.close_calls == 0
    assert vector.close_calls == 0

    registry.close()
    assert old_owner.close_calls == 1
    assert vector.close_calls == 1


def test_registry_active_alias_cannot_be_cleaned_after_retire_interrupt():
    stop = KeyboardInterrupt("active retirement interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    class Vector:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    vector = Vector()
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        vector_store=vector,
        bm25=object(),
        runner=object(),
    )
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner
    source, first_line = inspect.getsourcelines(RepoRegistry._retire_active_locked)
    retirement_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._retired_bundles[id(bundle)] = owned" in line
    )
    triggered = False

    def interrupt_after_retirement(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry._retire_active_locked.__code__
            and frame.f_lineno > retirement_line
        ):
            triggered = True
            sys.settrace(None)
            raise stop
        return interrupt_after_retirement

    sys.settrace(interrupt_after_retirement)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            with registry._generation_lock:
                registry._retire_active_locked("repo")
    finally:
        sys.settrace(None)

    assert caught.value is stop
    assert registry.get("repo") is bundle
    assert registry.retired_generation_count == 1

    with registry.pin("repo") as pinned:
        assert pinned is bundle

    assert bundle.vector_store is vector
    assert bundle.bm25 is not None
    assert bundle.runner is not None
    assert owner.close_calls == vector.close_calls == 0

    registry.close()
    assert owner.close_calls == vector.close_calls == 1
    assert registry._retired_bundles == {}


def test_registry_cleanup_claim_interruption_preserves_primary(monkeypatch):
    primary = SystemExit("candidate preparation stopped")
    later = KeyboardInterrupt("cleanup claim interrupted")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls > 0

        def close(self):
            self.close_calls += 1

    owner = Owner()
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, owner),
    )
    monkeypatch.setattr(
        registry,
        "_prepare_runtime_bundle",
        lambda _bundle: (_ for _ in ()).throw(primary),
    )
    source, first_line = inspect.getsourcelines(RepoRegistry._retire_unpublished)
    claim_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "self._retired_bundles[key] = owned" in line
    )
    triggered = False

    def interrupt_after_claim(frame, event, _arg):
        nonlocal triggered
        if (
            not triggered
            and event == "line"
            and frame.f_code is RepoRegistry._retire_unpublished.__code__
            and frame.f_lineno > claim_line
        ):
            triggered = True
            sys.settrace(None)
            raise later
        return interrupt_after_claim

    sys.settrace(interrupt_after_claim)
    try:
        with pytest.raises(SystemExit) as caught:
            registry._replace_entry(
                SimpleNamespace(instance_id="repo"),
                prepare_runtime=True,
            )
    finally:
        sys.settrace(None)

    assert caught.value is primary
    assert caught.value.__cause__ is later
    assert triggered is True
    assert owner.close_calls == 0
    assert registry.retired_generation_count == 1

    registry.close()
    assert owner.close_calls == 1
    assert registry._retired_bundles == {}


def test_registry_failed_refresh_keeps_active_generation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(repo, manifest_path, instance_id="repo")
    old = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    candidate = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())

    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    old_owner = Owner()
    candidate_owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = old_owner
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )
    monkeypatch.setattr(
        registry,
        "_build_repo_metadata",
        lambda _entry: _OwnedRepoBundle(candidate, None, candidate_owner),
    )

    def reject(_bundle):
        assert registry.get("repo") is old
        raise ValueError("candidate is incomplete")

    monkeypatch.setattr(registry, "_prepare_runtime_bundle", reject)

    with pytest.raises(ValueError, match="candidate is incomplete"):
        registry.refresh("repo")

    assert registry.get("repo") is old
    assert old_owner.closed is False
    assert candidate_owner.closed is True
    assert registry.retired_generation_count == 0
    registry.close()


def test_registry_failed_refresh_retains_unpublished_cleanup_owner(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    entry = _repo_entry(repo, manifest_path, instance_id="repo")
    old = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())

    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    old_owner = Owner()
    candidate_owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = old
    registry._source_cleanup_owners["repo"] = old_owner
    monkeypatch.setattr(
        "codenib.web.repo_registry.load_registry",
        lambda _path: [entry],
    )

    error = ValueError("candidate authentication failed")
    error.source_cleanup_owner = candidate_owner

    def fail_build(_entry):
        raise error

    monkeypatch.setattr(registry, "_build_repo_metadata", fail_build)

    with pytest.raises(ValueError, match="candidate authentication failed"):
        registry.refresh("repo")

    assert registry.get("repo") is old
    assert old_owner.closed is False
    assert registry._orphan_cleanup_owners == [candidate_owner]
    registry.close()
    assert old_owner.closed is True
    assert candidate_owner.closed is True


def test_registry_close_defers_pinned_generation_cleanup():
    class Owner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner

    with registry.pin("repo") as pinned:
        registry.close()

        assert pinned is bundle
        assert owner.closed is False
        assert registry.get("repo") is None
        assert registry.retired_generation_count == 1

    assert owner.closed is True
    assert registry.retired_generation_count == 0


@pytest.mark.parametrize("pin_all", [False, True])
@pytest.mark.parametrize(
    ("body_type", "cleanup_type", "cleanup_wins"),
    [
        (SystemExit, KeyboardInterrupt, False),
        (RuntimeError, SystemExit, True),
    ],
)
def test_registry_pin_cleanup_preserves_exception_priority(
    pin_all,
    body_type,
    cleanup_type,
    cleanup_wins,
):
    body_failure = body_type("body failure")
    cleanup_failure = cleanup_type("cleanup failure")

    class Owner:
        def __init__(self):
            self.close_calls = 0

        @property
        def closed(self):
            return self.close_calls >= 2

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise cleanup_failure

    bundle = RepoBundle(entry=SimpleNamespace(), manifest=SimpleNamespace())
    owner = Owner()
    registry = RepoRegistry(QAConfig())
    registry._bundles["repo"] = bundle
    registry._source_cleanup_owners["repo"] = owner
    pinned = registry.pin_all() if pin_all else registry.pin("repo")

    caught = None
    try:
        with pinned:
            registry.close()
            raise body_failure
    except BaseException as observed:  # noqa: B036 - assert exact priority
        caught = observed
    else:
        raise AssertionError("pinned body failure did not propagate")

    expected = cleanup_failure if cleanup_wins else body_failure
    secondary = body_failure if cleanup_wins else cleanup_failure
    assert caught is expected
    assert caught.__cause__ is secondary
    assert owner.close_calls == 1
    assert registry.retired_generation_count == 1

    registry.close()
    assert owner.close_calls == 2
    assert registry.retired_generation_count == 0


def test_repo_views_reject_documents_outside_authenticated_selection(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "runtime.py").write_text("runtime\n", encoding="utf-8")
    (repo / "private").mkdir()
    (repo / "private" / "secret.py").write_text("secret\n", encoding="utf-8")
    bm25_dir = tmp_path / "bm25"
    bm25_dir.mkdir()
    (bm25_dir / "documents.json").write_text(
        '[{"page_content":"secret","metadata":{"file":"private/secret.py"}}]',
        encoding="utf-8",
    )
    (bm25_dir / "bm25_metadata.json").write_text(
        '{"max_k":10}',
        encoding="utf-8",
    )
    entry = IndexEntry(
        index_type="bm25",
        path=str(bm25_dir),
        built_at="2026-08-20T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config={
            "artifact_file_fingerprints": bm25_artifact_file_fingerprints(bm25_dir)
        },
    )
    instance_id = "owner__repo-1"
    binding = capture_repository_source(
        repo,
        selection=RepositorySourceSelection(("private",)),
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(instance_id=instance_id),
        manifest=SimpleNamespace(
            indexes={"bm25": entry},
            index_is_current=lambda _name: True,
        ),
        bm25=None,
        vector_store=None,
        source_reader=binding.borrow_reader(),
    )
    registry = RepoRegistry(QAConfig())
    registry._source_bindings[instance_id] = binding

    try:
        with pytest.raises(ValueError, match="outside the authenticated"):
            registry._load_repo_views(bundle)
    finally:
        binding.close()

    assert bundle.bm25 is None


def test_bundle_rejects_graph_paths_outside_authenticated_selection(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "runtime.py").write_text("runtime\n", encoding="utf-8")
    (repo / "private").mkdir()
    (repo / "private" / "secret.py").write_text("secret\n", encoding="utf-8")
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    graph_path = graph_dir / "graph.pkl"
    graph = CodeGraph()
    graph._add_vertex(
        "private/secret.py:secret()",
        {
            "type": "function",
            "file": "private/secret.py",
            "start_line": 0,
            "end_line": 0,
            "unified_name": "private/secret.py:secret()",
        },
    )
    graph.save_graph(graph_path)
    binding = capture_repository_source(
        repo,
        selection=RepositorySourceSelection(("private",)),
    )
    bundle = RepoBundle(
        entry=SimpleNamespace(instance_id="owner__repo-1"),
        manifest=_legacy_view_manifest(
            "symbol_graph",
            str(graph_dir),
            view_commit="abc123",
            manifest_commit="abc123",
            config={
                "graph_artifact": {
                    "relative_path": "graph.pkl",
                    **regular_file_fingerprint(graph_path),
                }
            },
        ),
        source_reader=binding.borrow_reader(),
    )

    try:
        assert bundle.code_graph() is None
    finally:
        binding.close()

    assert "outside the authenticated" in bundle._code_graph_error


def test_manifest_selected_bundle_never_falls_back_to_live_checkout(
    tmp_path, monkeypatch
):
    bundle = RepoBundle(
        entry=SimpleNamespace(repo_dir=str(tmp_path)),
        manifest=SimpleNamespace(
            source_fingerprint="sha256-v2:bound",
            source_selection=RepositorySourceSelection(),
            indexes={},
        ),
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry.read_repository_summary",
        lambda *_args: pytest.fail("must not read the live checkout"),
    )

    assert bundle._description() == ""
    assert bundle.code_graph() is None
    assert bundle.hierarchical_graph() is None

    registry = RepoRegistry(QAConfig())
    with pytest.raises(RuntimeError, match="authenticated source reader"):
        registry._load_repo_views(bundle)


def test_bundle_loads_views_without_constructing_agent_runtime():
    calls = []
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        view_loader=lambda target: calls.append(("views", target)),
        runtime_loader=lambda target: calls.append(("runtime", target)),
    )

    bundle.ensure_views()
    bundle.ensure_views()

    assert calls == [("views", bundle)]
    assert bundle.runner is None

    bundle.ensure_runtime()
    bundle.ensure_runtime()

    assert calls == [("views", bundle), ("runtime", bundle)]


def test_bundle_can_release_and_reload_maintenance_views():
    calls = []

    class Vector:
        def close(self):
            calls.append("closed")

    def load_views(target):
        calls.append("loaded")
        target.vector_store = Vector()
        target.bm25 = object()

    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(),
        view_loader=load_views,
        runtime_loader=lambda _target: None,
    )

    bundle.ensure_views()
    assert bundle.release_views() is True
    assert bundle.vector_store is None
    assert bundle.bm25 is None

    bundle.ensure_views()

    assert calls == ["loaded", "closed", "loaded"]


def test_bundle_reports_indexed_source_files_instead_of_repository_files():
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            file_count=99,
            indexes={
                "bm25": SimpleNamespace(metadata={"source_file_count": 3}),
            },
        ),
    )

    assert bundle._file_count() == 3


def test_bundle_reports_partial_graph_language_coverage():
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(
            indexes={
                "symbol_graph": SimpleNamespace(
                    metadata={
                        "available_languages": ["python", "ts"],
                        "failed_languages": {
                            "cpp": "compile database unavailable",
                            "rust": "manifest unavailable",
                        },
                        "partial": True,
                    }
                )
            }
        ),
    )

    coverage = bundle.graph_coverage()

    assert coverage is not None
    assert coverage.available_languages == ["python", "ts"]
    assert coverage.unavailable_languages == ["cpp", "rust"]
    assert coverage.partial is True


def test_repo_views_reject_bm25_that_no_longer_matches_manifest(tmp_path):
    bm25_dir = tmp_path / "bm25"
    bm25_dir.mkdir()
    documents = bm25_dir / "documents.json"
    documents.write_text('[{"content":"alpha"}]')
    (bm25_dir / "bm25_metadata.json").write_text('{"max_k":10}')
    entry = IndexEntry(
        index_type="bm25",
        path=str(bm25_dir),
        built_at="2026-08-06T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config={
            "artifact_file_fingerprints": bm25_artifact_file_fingerprints(bm25_dir)
        },
    )
    manifest = RepoManifest(
        version="1.1",
        source_selection=None,
        indexes={"bm25": entry},
    )
    documents.write_text(documents.read_text().replace("alpha", "omega"))
    bundle = SimpleNamespace(manifest=manifest, bm25=None, vector_store=None)
    registry = SimpleNamespace(_config=SimpleNamespace(index_types=lambda: ("bm25",)))

    with pytest.raises(ValueError, match="manifest fingerprints"):
        RepoRegistry._load_repo_views(registry, bundle)

    assert bundle.bm25 is None


@pytest.mark.parametrize(
    ("status", "view_commit"),
    [
        ("stale", "old-commit"),
        ("failed", "new-commit"),
        ("fresh", "old-commit"),
    ],
)
def test_bundle_rejects_graphs_outside_the_manifest_snapshot(
    tmp_path,
    status,
    view_commit,
):
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    (graph_dir / "graph.pkl").write_bytes(b"stale graph")
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=_legacy_view_manifest(
            "symbol_graph",
            str(graph_dir),
            status=status,
            view_commit=view_commit,
            manifest_commit="new-commit",
        ),
    )

    assert bundle._graph_path() is None


def test_bundle_accepts_fresh_graph_for_manifest_snapshot(tmp_path):
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    graph_path = graph_dir / "graph.pkl"
    graph_path.write_bytes(b"current graph")
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=_legacy_view_manifest(
            "symbol_graph",
            str(graph_dir),
            view_commit="new-commit",
            manifest_commit="new-commit",
        ),
    )

    assert bundle._graph_path() == str(graph_path)


def test_bundle_rejects_graph_from_a_different_source_selection(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "private").mkdir()
    (repo / "src" / "runtime.py").write_text("def runtime():\n    return 1\n")
    (repo / "private" / "secret.py").write_text("SECRET = True\n")
    selection = RepositorySourceSelection(("private",))
    source = fingerprint_repository(repo, selection=selection)

    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    graph_path = graph_dir / "graph.pkl"
    graph_path.write_bytes(b"graph built for another source selection")
    entry = IndexEntry(
        index_type="symbol_graph",
        path=str(graph_dir),
        built_at="2026-08-24T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        commit="abc123",
        source_fingerprint=source.value,
        source_selection_digest=RepositorySourceSelection().digest,
    )
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="abc123",
        source_fingerprint=source.value,
        source_selection=selection,
        indexes={"symbol_graph": entry},
    )
    bundle = RepoBundle(entry=SimpleNamespace(), manifest=manifest)

    assert manifest.index_is_current("symbol_graph") is False
    assert bundle._graph_path() is None


def test_bundle_rejects_legacy_graph_without_a_symbol_graph_entry(tmp_path):
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    graph_path = vector_dir / "graph.pkl"
    graph_path.write_bytes(b"legacy current graph")
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=_legacy_view_manifest(
            "vector",
            str(vector_dir),
            view_commit="new-commit",
            manifest_commit="new-commit",
        ),
    )

    assert bundle._graph_path() is None


def test_bundle_explains_schema_mismatch_without_advertising_codemap(
    tmp_path, monkeypatch
):
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    with (graph_dir / "graph.pkl").open("wb") as handle:
        pickle.dump({"schema_version": 4}, handle)
    manifest = _legacy_view_manifest(
        "symbol_graph",
        str(graph_dir),
        view_commit="abc123456789",
        manifest_commit="abc123456789",
    )
    manifest.capabilities = {"symbol_navigation": True}
    manifest.languages = ["python"]
    manifest.file_count = 1
    bundle = RepoBundle(
        entry=SimpleNamespace(
            instance_id="org__repo",
            repo="org/repo",
            commit_short="abc12345",
            base_commit="abc123456789",
            language="python",
            problem_statement="",
            repo_dir=str(tmp_path),
        ),
        manifest=manifest,
    )

    def reject_old_graph(_path):
        raise ValueError(
            "graph.pkl at /private/index has schema_version=4, expected 5. Rebuild."
        )

    monkeypatch.setattr(
        "codenib.graph.code_graph.CodeGraph.load_graph", reject_old_graph
    )
    monkeypatch.setattr("codenib.web.repo_registry.find_spec", lambda _name: object())

    assert bundle.info().capabilities["codemap"] is False
    assert bundle.graph_unavailable_note() == (
        "Dependency graph uses schema 4, but this server requires schema 5. "
        "Rebuild symbol_graph for this repository."
    )
    assert bundle.code_graph() is None
    assert bundle.graph_unavailable_note() == (
        "Dependency graph uses schema 4, but this server requires schema 5. "
        "Rebuild symbol_graph for this repository."
    )
    assert bundle.info().capabilities["codemap"] is False


def test_bundle_graph_diagnostic_does_not_import_optional_igraph(monkeypatch):
    bundle = RepoBundle(
        entry=SimpleNamespace(),
        manifest=SimpleNamespace(indexes={}),
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry.find_spec",
        lambda name: None if name == "igraph" else object(),
    )

    assert bundle.graph_unavailable_note() == (
        "Dependency graph support is unavailable. Install CodeNib with the "
        "graph extra to inspect symbol_graph artifacts."
    )


def test_config_index_types_for_mode():
    assert QAConfig(mode="sparse").index_types() == ["bm25"]
    assert QAConfig(mode="hybrid").index_types() == ["bm25", "vector"]


@pytest.mark.parametrize(
    ("mode", "expected_vector_loads"),
    [("sparse", 0), ("hybrid", 1)],
)
def test_repo_views_respect_configured_index_mode(
    monkeypatch,
    native_authorization,
    mode,
    expected_vector_loads,
):
    vector_entry = SimpleNamespace(path="/tmp/vector")
    manifest = SimpleNamespace(
        indexes={"vector": vector_entry},
        index_is_current=lambda index_type: index_type == "vector",
    )
    repo_entry = SimpleNamespace(instance_id="repo")
    bundle = SimpleNamespace(entry=repo_entry, manifest=manifest)
    resolved = []

    def resolve(entry, resolved_manifest, resolved_vector):
        resolved.append((entry, resolved_manifest, resolved_vector))
        return native_authorization

    registry = RepoRegistry(
        QAConfig(mode=mode),
        native_index_authorization_resolver=resolve,
    )
    loaded = []
    monkeypatch.setattr(
        registry,
        "_load_vector_store",
        lambda entry, **_kwargs: loaded.append(entry) or "vector-store",
    )

    registry._load_repo_views(bundle)

    assert loaded == [vector_entry] * expected_vector_loads
    assert bundle.vector_store == ("vector-store" if expected_vector_loads else None)
    assert resolved == (
        [(repo_entry, manifest, vector_entry)] if expected_vector_loads else []
    )


@pytest.mark.parametrize("resolver_kind", ["missing", "declines"])
def test_repo_views_keep_bm25_only_for_explicit_optional_authority_policy(
    monkeypatch,
    resolver_kind,
):
    class FakeBM25:
        def load_index(self, path):
            self.path = path

    monkeypatch.setattr(
        "codenib.index.sparse_idx.bm25_index.BM25CodeIndexer",
        FakeBM25,
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry.require_bm25_manifest_artifact",
        lambda _entry: None,
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail(
            "unusable authority must fail before vector model initialization"
        ),
    )
    if resolver_kind == "missing":
        resolver = None
    else:

        def resolver(_repo_entry, _manifest, _vector_entry):
            return None

    bm25_entry = SimpleNamespace(path="/idx/bm25", config={})
    vector_entry = SimpleNamespace(path="/idx/vector", config={})
    manifest = SimpleNamespace(
        indexes={"bm25": bm25_entry, "vector": vector_entry},
        index_is_current=lambda _index_type: True,
    )
    bundle = SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
    registry = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=resolver,
        allow_missing_native_index_authorization=True,
    )

    registry._load_repo_views(bundle)

    assert bundle.bm25.path == "/idx/bm25"
    assert bundle.vector_store is None


@pytest.mark.parametrize(
    "source_fingerprint",
    ["", f"sha256:{'a' * 64}"],
)
def test_repo_views_keep_bm25_only_for_legacy_source_fingerprint(
    monkeypatch,
    source_fingerprint,
):
    class FakeBM25:
        def load_index(self, path):
            self.path = path

    monkeypatch.setattr(
        "codenib.index.sparse_idx.bm25_index.BM25CodeIndexer",
        FakeBM25,
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry.require_bm25_manifest_artifact",
        lambda _entry: None,
    )
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail(
            "legacy vector must be skipped before model initialization"
        ),
    )

    def unexpected_resolver(_repo_entry, _manifest, _vector_entry):
        pytest.fail("legacy vector must be skipped before authorization")

    bm25_entry = SimpleNamespace(path="/idx/bm25", config={})
    vector_entry = SimpleNamespace(path="/idx/vector", config={})
    manifest = SimpleNamespace(
        source_fingerprint=source_fingerprint,
        indexes={"bm25": bm25_entry, "vector": vector_entry},
        index_is_current=lambda _index_type: True,
    )
    bundle = SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
    registry = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=unexpected_resolver,
        allow_missing_native_index_authorization=True,
    )

    registry._load_repo_views(bundle)

    assert bundle.bm25.path == "/idx/bm25"
    assert bundle.vector_store is None


def test_repo_views_reject_legacy_vector_without_current_bm25(monkeypatch):
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail("legacy vector must not be opened"),
    )
    vector_entry = SimpleNamespace(path="/idx/vector", config={})
    manifest = SimpleNamespace(
        source_fingerprint="",
        indexes={"vector": vector_entry},
        index_is_current=lambda index_type: index_type == "vector",
    )
    bundle = SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
    registry = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=lambda *_args: pytest.fail(
            "legacy vector must be skipped before authorization"
        ),
        allow_missing_native_index_authorization=True,
    )

    with pytest.raises(ValueError, match="no current BM25 fallback is available"):
        registry._load_repo_views(bundle)


@pytest.mark.parametrize("resolver_kind", ["missing", "declines"])
def test_repo_views_fail_closed_when_required_authority_is_missing(
    monkeypatch,
    resolver_kind,
):
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail(
            "missing authority must fail before vector model initialization"
        ),
    )
    if resolver_kind == "missing":
        resolver = None
    else:

        def decline(_repo, _manifest, _entry):
            return None

        resolver = decline

    vector_entry = SimpleNamespace(path="/idx/vector", config={})
    manifest = SimpleNamespace(
        indexes={"vector": vector_entry},
        index_is_current=lambda _index_type: True,
    )
    bundle = SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
    if resolver is None:
        with pytest.raises(
            MissingNativeIndexAuthorizationError,
            match="requires an external",
        ):
            RepoRegistry(QAConfig(mode="hybrid"))
        return

    registry = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=resolver,
    )
    with pytest.raises(MissingNativeIndexAuthorizationError, match="returned no"):
        registry._load_repo_views(bundle)


def test_repo_views_propagate_resolver_rejection_before_model_initialization(
    monkeypatch,
):
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail(
            "resolver rejection must fail before vector model initialization"
        ),
    )

    def reject(_repo, _manifest, _entry):
        raise ValueError("operator denied vector authority")

    vector_entry = SimpleNamespace(path="/idx/vector", config={})
    manifest = SimpleNamespace(
        source_fingerprint=f"sha256-v2:{'a' * 64}",
        indexes={"vector": vector_entry},
        index_is_current=lambda _index_type: True,
    )
    registry = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=reject,
        allow_missing_native_index_authorization=True,
    )

    with pytest.raises(ValueError, match="operator denied"):
        registry._load_repo_views(
            SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
        )


def test_repo_views_propagate_vector_integrity_failures(
    native_authorization, monkeypatch
):
    vector_entry = SimpleNamespace(path="/idx/vector", config={})
    manifest = SimpleNamespace(
        source_fingerprint=f"sha256-v2:{'b' * 64}",
        indexes={"vector": vector_entry},
        index_is_current=lambda _index_type: True,
    )
    registry = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=(
            lambda _repo, _manifest, _entry: native_authorization
        ),
        allow_missing_native_index_authorization=True,
    )
    monkeypatch.setattr(
        registry,
        "_load_vector_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("authenticated vector bytes changed")
        ),
    )

    with pytest.raises(ValueError, match="vector bytes changed"):
        registry._load_repo_views(
            SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
        )


def test_hybrid_requires_a_current_vector_or_current_bm25_fallback(monkeypatch):
    manifest = SimpleNamespace(
        indexes={},
        index_is_current=lambda _index_type: False,
    )
    bundle = SimpleNamespace(entry=SimpleNamespace(), manifest=manifest)
    required = RepoRegistry(
        QAConfig(mode="hybrid"),
        native_index_authorization_resolver=(lambda _repo, _manifest, _entry: object()),
    )

    with pytest.raises(ValueError, match="requires a current vector"):
        required._load_repo_views(bundle)

    optional = RepoRegistry(
        QAConfig(mode="hybrid"),
        allow_missing_native_index_authorization=True,
    )
    with pytest.raises(ValueError, match="no current BM25 fallback is available"):
        optional._load_repo_views(bundle)


def test_vector_authority_is_preflighted_before_model_initialization(monkeypatch):
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail(
            "invalid authority must fail before vector model initialization"
        ),
    )
    registry = RepoRegistry(QAConfig())

    with pytest.raises(ValueError, match="malformed"):
        registry._load_vector_store(
            SimpleNamespace(path="/idx/vector", config={}),
            native_index_authorization=object(),
        )


@pytest.mark.parametrize("mismatch", ["tree", "semantic"])
def test_exact_vector_authority_is_checked_before_model_initialization(
    tmp_path,
    monkeypatch,
    mismatch,
):
    from codenib.index.embedding.artifact_integrity import (
        capture_authenticated_vector_view,
    )

    authorized_root = tmp_path / "authorized"
    loaded_root = tmp_path / "loaded"
    authorized_root.mkdir()
    loaded_root.mkdir()
    (authorized_root / "config.json").write_text('{"tree":"authorized"}')
    (loaded_root / "config.json").write_text('{"tree":"loaded"}')
    manifest_config = {"embedding_model": "vendor/model"}
    token_config = (
        {"embedding_model": "different/model"}
        if mismatch == "semantic"
        else manifest_config
    )
    with capture_authenticated_vector_view(authorized_root) as view:
        authorization = _mint_trusted_local_admin_authorization(
            view.ownership,
            view_type="vector",
            semantic_contract=token_config,
            evidence=("test-local-admin",),
        )
    target = authorized_root if mismatch == "semantic" else loaded_root
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail(
            "wrong-tree or wrong-semantic authority must fail before model init"
        ),
    )

    with pytest.raises(
        InvalidNativeIndexAuthorizationError,
        match="does not match captured bytes",
    ):
        RepoRegistry(QAConfig())._load_vector_store(
            SimpleNamespace(path=str(target), config=manifest_config),
            native_index_authorization=authorization,
        )


def test_config_paths(tmp_path):
    cfg = QAConfig(data_dir=str(tmp_path / "data"))
    assert cfg.registry_path.endswith("/data/qa_registry.json")
    assert cfg.repo_dir("django__django-123").endswith("/data/repos/django__django-123")


def test_load_config_from_yaml(tmp_path):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text(
        "model: my-model\n"
        "wiki_model: wiki-model\n"
        "wiki_api_base: http://wiki.example/v1\n"
        "model_api_base: http://ask.example/v1\n"
        "model_options:\n"
        "  timeout: 20\n"
        "  extra_body:\n"
        "    reasoning:\n"
        "      enabled: true\n"
        "wiki_model_options:\n"
        "  timeout: 45\n"
        "  extra_body:\n"
        "    reasoning:\n"
        "      enabled: false\n"
        "mode: hybrid\n"
        "embedding_provider: openai\n"
        "embedding_base_url: http://embed.example/v1\n"
        "dataset: foo/bar\n"
        "per_language: 2\n"
        "languages: [python, go]\n"
        "instances: [a__a-1, b__b-2]\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg.model == "my-model"
    assert cfg.wiki_generation_model == "wiki-model"
    assert cfg.wiki_generation_api_base == "http://wiki.example/v1"
    assert cfg.model_api_base == "http://ask.example/v1"
    assert cfg.model_options["timeout"] == 20
    assert cfg.wiki_generation_options == {
        "timeout": 45,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert cfg.mode == "hybrid"
    assert cfg.embedding_provider == "openai"
    assert cfg.embedding_base_url == "http://embed.example/v1"
    assert cfg.dataset == "foo/bar"
    assert cfg.per_language == 2
    assert cfg.languages == ["python", "go"]
    assert cfg.instances == ["a__a-1", "b__b-2"]


def test_backend_environment_overrides_yaml(tmp_path, monkeypatch):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text(
        "model: yaml-ask\n"
        "wiki_model: yaml-wiki\n"
        "embedding_provider: huggingface\n"
    )
    monkeypatch.setenv("CODENIB_DEMO_MODEL", "env-ask")
    monkeypatch.setenv("CODENIB_DEMO_WIKI_MODEL", "env-wiki")
    monkeypatch.setenv("CODENIB_DEMO_WIKI_API_BASE", "http://wiki.local/v1")
    monkeypatch.setenv("CODENIB_DEMO_WIKI_API_KEY", "wiki-secret")
    monkeypatch.setenv("CODENIB_DEMO_API_BASE", "http://ask.local/v1")
    monkeypatch.setenv(
        "CODENIB_DEMO_MODEL_OPTIONS",
        '{"timeout":30,"extra_body":{"reasoning":{"enabled":true}}}',
    )
    monkeypatch.setenv(
        "CODENIB_DEMO_WIKI_MODEL_OPTIONS",
        '{"extra_body":{"reasoning":{"enabled":false}}}',
    )
    monkeypatch.setenv("CODENIB_EMBEDDING_PROVIDER", "OPENAI")
    monkeypatch.setenv("CODENIB_EMBEDDING_BASE_URL", "http://embed.local/v1")

    cfg = load_config(str(cfg_file))

    assert cfg.model == "env-ask"
    assert cfg.wiki_generation_model == "env-wiki"
    assert cfg.wiki_generation_api_base == "http://wiki.local/v1"
    assert cfg.wiki_generation_api_key == "wiki-secret"
    assert cfg.model_api_base == "http://ask.local/v1"
    assert cfg.model_options == {
        "timeout": 30,
        "extra_body": {"reasoning": {"enabled": True}},
    }
    assert cfg.wiki_generation_options == {
        "timeout": 30,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert cfg.embedding_provider == "openai"
    assert cfg.embedding_base_url == "http://embed.local/v1"


def test_wiki_backend_falls_back_to_ask_backend():
    cfg = QAConfig(
        model_api_base="http://ask.local/v1",
        model_api_key="ask-secret",
    )

    assert cfg.wiki_generation_api_base == "http://ask.local/v1"
    assert cfg.wiki_generation_api_key == "ask-secret"


def test_explicit_wiki_model_reuses_matching_ask_provider_backend():
    cfg = QAConfig(
        model="openai/ask-model",
        wiki_model="openai/wiki-model",
        model_api_base="http://shared.local/v1",
        model_api_key="shared-secret",
    )

    assert cfg.wiki_generation_api_base == "http://shared.local/v1"
    assert cfg.wiki_generation_api_key == "shared-secret"


def test_explicit_wiki_model_does_not_inherit_different_provider_backend():
    cfg = QAConfig(
        model="openai/ask-model",
        wiki_model="vertex_ai/wiki-model",
        model_api_base="http://ask.local/v1",
        model_api_key="ask-secret",
    )

    assert cfg.wiki_generation_api_base is None
    assert cfg.wiki_generation_api_key is None


def test_rejects_unknown_embedding_provider(tmp_path):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text("embedding_provider: custom\n")

    with pytest.raises(ValueError, match="embedding_provider"):
        load_config(str(cfg_file))


def test_vector_store_uses_provider_config_and_reuses_client(
    monkeypatch,
    native_authorization,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = kwargs.get("embedding") or object()
            self.loaded = None
            created.append(self)

        def load(self, path, **kwargs):
            self.loaded = path
            assert kwargs["native_index_authorization"] is native_authorization

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    cfg = QAConfig(
        embedding_provider="openai",
        embedding_model="embed-model",
        embedding_dimension=768,
        embedding_base_url="http://embed.local/v1",
        embedding_api_key="secret",
    )
    registry = RepoRegistry(cfg)
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "embed-model",
            "embedding_provider": "openai",
            "embedding_dimension": 768,
            "embedding_endpoint": "http://embed.local/v1",
        },
    )

    first = registry._load_vector_store(
        entry,
        native_index_authorization=native_authorization,
    )
    second = registry._load_vector_store(
        entry,
        native_index_authorization=native_authorization,
    )

    assert first.loaded == "/tmp/vector"
    assert first.kwargs["embedding_provider"] == "openai"
    assert first.kwargs["base_url"] == "http://embed.local/v1"
    assert first.kwargs["api_key"] == "secret"
    assert first.kwargs["dimension"] == 768
    assert second.kwargs["embedding"] is first.embedding


def test_concurrent_vector_loads_initialize_shared_embedding_once(
    monkeypatch,
    native_authorization,
):
    first_load_started = Event()
    allow_first_load = Event()
    duplicate_construction = Event()
    construction_lock = Lock()
    cold_embeddings = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            embedding = kwargs.get("embedding")
            if embedding is None:
                embedding = object()
                with construction_lock:
                    cold_embeddings.append(embedding)
                    if len(cold_embeddings) > 1:
                        duplicate_construction.set()
            self.embedding = embedding

        def load(self, _path, **_kwargs):
            if not first_load_started.is_set():
                first_load_started.set()
                assert allow_first_load.wait(timeout=2)

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "vendor/model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
        },
    )

    def load():
        return registry._load_vector_store(
            entry,
            native_index_authorization=native_authorization,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(load)
        assert first_load_started.wait(timeout=2)
        second_future = executor.submit(load)
        try:
            assert not duplicate_construction.wait(timeout=0.2)
        finally:
            allow_first_load.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert cold_embeddings == [first.embedding]
    assert second.embedding is first.embedding


def test_vector_load_failure_does_not_publish_embedding_and_retains_cleanup_owner(
    monkeypatch,
    native_authorization,
):
    primary = RuntimeError("vector load failed")
    cleanup = KeyboardInterrupt("vector cleanup interrupted")
    created = []

    class FakeVectorStore:
        def __init__(self, **_kwargs):
            self.embedding = object()
            self.close_calls = 0
            created.append(self)

        def load(self, _path, **_kwargs):
            assert registry._embeddings == {}
            raise primary

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise cleanup

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "vendor/model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
        },
    )

    observed = None
    try:
        registry._load_vector_store(
            entry,
            native_index_authorization=native_authorization,
        )
    except BaseException as exc:  # noqa: B036 - assert primary/cleanup priority
        observed = exc

    assert observed is cleanup
    assert observed.__cause__ is primary
    assert registry._embeddings == {}
    assert observed.vector_cleanup_owner is created[0]

    observed.vector_cleanup_owner.close()
    assert created[0].close_calls == 2


def test_vector_store_restores_manifest_embedding_identity(
    monkeypatch,
    native_authorization,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = kwargs.get("embedding") or object()
            created.append(self)

        def load(self, _path, **_kwargs):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "nomic-ai/CodeRankEmbed",
            "embedding_provider": "huggingface",
            "dimension": 768,
            "embedding_kwargs": {
                "model_kwargs": {"trust_remote_code": True},
                "revision": "immutable-model-revision",
            },
            "index_metric": "l2",
        },
    )

    registry._load_vector_store(
        entry,
        native_index_authorization=native_authorization,
    )

    assert created[0].kwargs["embedding_model"] == "nomic-ai/CodeRankEmbed"
    assert created[0].kwargs["dimension"] == 768
    assert created[0].kwargs["index_metric"] == "l2"
    assert created[0].kwargs["model_kwargs"] == {"trust_remote_code": True}
    assert created[0].kwargs["revision"] == "immutable-model-revision"


def test_vector_store_supports_legacy_prebuilt_route_fallback(
    monkeypatch,
    native_authorization,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = object()
            created.append(self)

        def load(self, _path, **_kwargs):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(
        QAConfig(
            embedding_provider="huggingface",
            embedding_dimension=768,
        )
    )
    entry = SimpleNamespace(
        path="/tmp/legacy-prebuilt",
        config={
            "embedding_model": "nomic-ai/CodeRankEmbed",
        },
    )

    registry._load_vector_store(
        entry,
        native_index_authorization=native_authorization,
    )

    assert created[0].kwargs["embedding_provider"] == "huggingface"
    assert created[0].kwargs["embedding_model"] == "nomic-ai/CodeRankEmbed"
    assert created[0].kwargs["dimension"] == 768
    assert created[0].kwargs["artifact_metadata"] == entry.config


def test_vector_store_fills_legacy_dimension_with_persisted_provider(
    monkeypatch,
    native_authorization,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = object()
            created.append(self)

        def load(self, _path, **_kwargs):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(
        QAConfig(
            embedding_provider="huggingface",
            embedding_dimension=768,
        )
    )
    entry = SimpleNamespace(
        path="/tmp/legacy-prebuilt",
        config={
            "embedding_model": "nomic-ai/CodeRankEmbed",
            "embedding_provider": "huggingface",
        },
    )

    registry._load_vector_store(
        entry,
        native_index_authorization=native_authorization,
    )

    assert created[0].kwargs["dimension"] == 768


def test_vector_store_rejects_invalid_persisted_legacy_dimension(
    monkeypatch,
    native_authorization,
):
    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: pytest.fail("invalid route must fail before loading the store"),
    )
    registry = RepoRegistry(
        QAConfig(
            embedding_provider="huggingface",
            embedding_dimension=768,
        )
    )
    entry = SimpleNamespace(
        path="/tmp/legacy-prebuilt",
        config={
            "embedding_model": "nomic-ai/CodeRankEmbed",
            "embedding_dimension": 0,
        },
    )

    with pytest.raises(ValueError, match="dimension must be a positive integer"):
        registry._load_vector_store(
            entry,
            native_index_authorization=native_authorization,
        )


def test_vector_store_cache_separates_model_revisions(
    monkeypatch,
    native_authorization,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = kwargs.get("embedding") or object()
            created.append(self)

        def load(self, _path, **_kwargs):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(QAConfig(embedding_provider="huggingface"))

    def entry(revision):
        return SimpleNamespace(
            path=f"/tmp/vector-{revision}",
            config={
                "embedding_model": "vendor/model",
                "embedding_provider": "huggingface",
                "embedding_dimension": 384,
                "embedding_kwargs": {"revision": revision},
            },
        )

    first = registry._load_vector_store(
        entry("revision-a"),
        native_index_authorization=native_authorization,
    )
    second = registry._load_vector_store(
        entry("revision-b"),
        native_index_authorization=native_authorization,
    )

    assert first.embedding is not second.embedding
    assert second.kwargs["embedding"] is None


def test_remote_embedding_override_cannot_replace_artifact_route(
    monkeypatch,
    native_authorization,
):
    created = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.embedding = object()
            created.append(self)

        def load(self, _path, **_kwargs):
            pass

    monkeypatch.setattr(
        "codenib.web.repo_registry._vector_store_type",
        lambda: FakeVectorStore,
    )
    registry = RepoRegistry(
        QAConfig(
            embedding_provider="openai",
            embedding_base_url="http://embed.local/v1",
            embedding_api_key="secret",
        )
    )
    entry = SimpleNamespace(
        path="/tmp/vector",
        config={
            "embedding_model": "vendor/model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "embedding_kwargs": {
                "model_kwargs": {"trust_remote_code": True},
                "revision": "local-revision",
            },
        },
    )

    with pytest.raises(ValueError, match="endpoint does not match"):
        registry._load_vector_store(
            entry,
            native_index_authorization=native_authorization,
        )

    assert created == []


def test_ask_model_receives_its_own_endpoint(monkeypatch):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("codenib.web.repo_registry._ask_llm_type", lambda: fake_chat)
    registry = RepoRegistry(
        QAConfig(
            model="openai/ask-model",
            wiki_model="vertex_ai/wiki-model",
            model_api_base="http://ask.local/v1",
            model_api_key="ask-secret",
            max_tokens=2048,
            model_options={"api_version": "2025-01-01"},
        )
    )

    registry._create_ask_llm()

    assert captured == {
        "model": "openai/ask-model",
        "temperature": 0.0,
        "max_tokens": 2048,
        "api_base": "http://ask.local/v1",
        "api_key": "ask-secret",
        "extra_kwargs": {"api_version": "2025-01-01"},
    }


def test_ask_runtime_exposes_only_query_facing_repository_search(monkeypatch):
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("codenib.agent.runner.AgentRunner", FakeRunner)
    registry = RepoRegistry(QAConfig())
    monkeypatch.setattr(registry, "_create_ask_llm", lambda: object())
    bundle = RepoBundle(
        entry=SimpleNamespace(language="python"),
        manifest=SimpleNamespace(
            repo_path="/tmp/repository",
            file_count=12,
            languages=["python"],
        ),
        bm25=object(),
    )

    registry._load_repo_runtime(bundle)

    assert captured["allow_skills"] == {"repository_search"}
    assert captured["include_default_tools"] is False
    assert captured["force_final_answer"] is True
    assert captured["review_final_answer"] is True
    assert captured["registry"].has("repository_search")
    assert bundle.runner is not None


@pytest.mark.parametrize(
    ("model", "api_base", "options"),
    [
        ("anthropic/claude-sonnet-4-5", None, {"timeout": 60}),
        (
            "vertex_ai/gemini-2.5-flash",
            None,
            {"vertex_project": "project", "vertex_location": "us-central1"},
        ),
        ("ollama/qwen3", "http://localhost:11434", {}),
        (
            "openrouter/qwen/qwen3-coder",
            None,
            {"extra_headers": {"HTTP-Referer": "https://codenib.ai"}},
        ),
    ],
)
def test_ask_model_preserves_litellm_provider_configuration(
    monkeypatch,
    model,
    api_base,
    options,
):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("codenib.web.repo_registry._ask_llm_type", lambda: fake_chat)
    RepoRegistry(
        QAConfig(
            model=model,
            model_api_base=api_base,
            model_options=options,
        )
    )._create_ask_llm()

    assert captured["model"] == model
    assert captured["api_base"] == api_base
    assert captured["extra_kwargs"] == options


def test_registry_round_trip(tmp_path):
    path = str(tmp_path / "qa_registry.json")
    entries = [
        RepoEntry(
            instance_id="django__django-11099",
            repo="django/django",
            base_commit="abcdef1234567890",
            language="python",
            repo_dir="/data/repos/django__django-11099/django_django",
            manifest_path="/data/.../repo_manifest.json",
            problem_statement="Something is broken.",
        )
    ]
    save_registry(path, entries)
    loaded = load_registry(path)
    assert len(loaded) == 1
    assert loaded[0].instance_id == "django__django-11099"
    assert loaded[0].repo == "django/django"
    assert loaded[0].commit_short == "abcdef12"


def test_load_registry_missing_returns_empty(tmp_path):
    assert load_registry(str(tmp_path / "nope.json")) == []
