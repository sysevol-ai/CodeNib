# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from pathlib import Path

import pytest

import codenib.compiler as compiler_module
import codenib.compiler.job_resources as job_resources_module
from codenib import LocalWorkspaceProvider
from codenib._atomic_directory import publication_parent_identity
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.job_resources import (
    BM25AttemptPoolReclamation,
    LocalBM25AttemptPoolCoordinator,
    LocalBM25SourceJobTarget,
)
from codenib.storage import StorageIntegrityError, StorageValidationError

_ATTEMPT = "a" * 32
_STAGE = "b" * 24
_DISCARD = "c" * 32


def _cleanup_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *tuple(getattr(error, "__notes__", ())),
        *tuple(getattr(error, "_codenib_cleanup_notes", ())),
    )


def _attempt_pool_target(
    tmp_path: Path,
    *,
    topology_verifier=None,
    workspace_parent_identity: tuple[int, ...] | None = None,
) -> tuple[LocalBM25SourceJobTarget, Path, list[bool]]:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700, parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = LocalWorkspaceProvider(workspace)
    if workspace_parent_identity is None:
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            workspace_parent_identity = publication_parent_identity(descriptor)
        finally:
            os.close(descriptor)
    topology_checks: list[bool] = []
    if topology_verifier is None:

        def topology_verifier() -> None:
            topology_checks.append(True)

    target = LocalBM25SourceJobTarget(
        repository_root=repository,
        workspace_provider=provider,
        repository_key="owner/repository",
        display_commit="d" * 40,
        builder=BM25IndexBuilder(),
        workspace_parent_identity=workspace_parent_identity,
        topology_verifier=topology_verifier,
    )
    return target, workspace, topology_checks


@pytest.mark.parametrize(
    ("name", "lineage", "discarded"),
    (
        (f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}", "current", False),
        (
            f"..codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}"
            f".discarded-{_DISCARD}",
            "current",
            True,
        ),
        (f".codenib-source-job-{_ATTEMPT}-attempt", "legacy", False),
        (
            f"..codenib-source-job-{_ATTEMPT}-bm25.normalize-{_STAGE}",
            "legacy",
            False,
        ),
        (
            f"..codenib-source-job-{_ATTEMPT}-context.discarded-{_DISCARD}",
            "legacy",
            True,
        ),
        (
            f"...codenib-source-job-{_ATTEMPT}-attempt.normalize-{_STAGE}"
            f".discarded-{_DISCARD}",
            "legacy",
            True,
        ),
    ),
)
def test_bm25_attempt_pool_name_policy_accepts_exact_lineage(
    name: str,
    lineage: str,
    discarded: bool,
) -> None:
    child = job_resources_module._classify_bm25_attempt_pool_child_name(name)

    assert child is not None
    assert child.lineage == lineage
    assert child.discarded is discarded


@pytest.mark.parametrize(
    "name",
    (
        f"codenib-source-job-{_ATTEMPT}",
        f".CODENIB-SOURCE-JOB-{_ATTEMPT}.NORMALIZE-{_STAGE}",
        f".codenib-source-job-{_ATTEMPT}-vector",
        f".codenib-source-job-{'a' * 31}.normalize-{_STAGE}",
        f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}.extra",
        f"....codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}",
        ".codenib-discarded-0123456789abcdef-" + _DISCARD,
    ),
)
def test_bm25_attempt_pool_name_policy_rejects_ambiguous_reserved_names(
    name: str,
) -> None:
    with pytest.raises(StorageValidationError, match="unrecognized reserved child"):
        job_resources_module._classify_bm25_attempt_pool_child_name(name)


@pytest.mark.parametrize(
    "name",
    (
        "unrelated",
        ".other.normalize-" + _STAGE,
        ".codenib-source-worker-topology-" + _ATTEMPT,
        "unrelated-µ",
    ),
)
def test_bm25_attempt_pool_name_policy_retains_unrelated_names(name: str) -> None:
    assert job_resources_module._classify_bm25_attempt_pool_child_name(name) is None


def test_bm25_attempt_pool_coordinator_reclaims_exact_mixed_lineage(
    tmp_path: Path,
) -> None:
    target, workspace, topology_checks = _attempt_pool_target(tmp_path)
    current = f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}"
    current_discarded = (
        f"..codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}" f".discarded-{_DISCARD}"
    )
    legacy = f".codenib-source-job-{_ATTEMPT}-attempt"
    legacy_stage = f"..codenib-source-job-{_ATTEMPT}-bm25.normalize-{_STAGE}"
    legacy_discarded = f"..codenib-source-job-{_ATTEMPT}-context.discarded-{_DISCARD}"
    legacy_discarded_stage = (
        f"...codenib-source-job-{_ATTEMPT}-attempt.normalize-{_STAGE}"
        f".discarded-{_DISCARD}"
    )
    attempts = (
        current,
        current_discarded,
        legacy,
        legacy_stage,
        legacy_discarded,
        legacy_discarded_stage,
    )
    for name in attempts:
        child = workspace / name
        child.mkdir(mode=0o700)
        (child / "payload.txt").write_text(name, encoding="utf-8")
    unrelated = workspace / "retain-me"
    unrelated.mkdir(mode=0o700)

    result = LocalBM25AttemptPoolCoordinator(target).reclaim(
        caller_asserts_quiescence=True,
    )

    assert result == BM25AttemptPoolReclamation(
        scanned_children=7,
        reclaimed_children=6,
        current_children=2,
        legacy_children=4,
        discarded_children=3,
        retained_unrelated_children=1,
    )
    assert tuple(workspace.iterdir()) == (unrelated,)
    assert len(topology_checks) >= 18


def test_bm25_attempt_pool_coordinator_preflights_every_name_before_mutation(
    tmp_path: Path,
) -> None:
    target, workspace, _topology_checks = _attempt_pool_target(tmp_path)
    recognized = workspace / (f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}")
    recognized.mkdir(mode=0o700)
    malformed = workspace / (f".codenib-source-job-{_ATTEMPT}.normalize-{'B' * 24}")
    malformed.mkdir(mode=0o700)

    with pytest.raises(StorageValidationError, match="unrecognized reserved child"):
        LocalBM25AttemptPoolCoordinator(target).reclaim(
            caller_asserts_quiescence=True,
        )

    assert recognized.is_dir()
    assert malformed.is_dir()


def test_bm25_attempt_pool_coordinator_requires_exact_quiescence_and_topology(
    tmp_path: Path,
) -> None:
    target, workspace, _topology_checks = _attempt_pool_target(tmp_path)
    coordinator = LocalBM25AttemptPoolCoordinator(target)

    with pytest.raises(StorageValidationError, match="caller-asserted quiescence"):
        coordinator.reclaim()
    with pytest.raises(TypeError, match="exact bool"):
        coordinator.reclaim(caller_asserts_quiescence=1)  # type: ignore[arg-type]

    no_topology_target = LocalBM25SourceJobTarget(
        repository_root=target.repository_root,
        workspace_provider=target.workspace_provider,
        repository_key=target.repository_key,
        display_commit=target.display_commit,
        builder=BM25IndexBuilder(),
        workspace_parent_identity=target.attempt_pool_identity,
    )
    with pytest.raises(StorageValidationError, match="requires retained topology"):
        LocalBM25AttemptPoolCoordinator(no_topology_target).reclaim(
            caller_asserts_quiescence=True,
        )

    short_identity_target, _workspace, _checks = _attempt_pool_target(
        tmp_path / "short-identity",
        workspace_parent_identity=(1, 2),
    )
    with pytest.raises(StorageValidationError, match="exact parent identity"):
        LocalBM25AttemptPoolCoordinator(short_identity_target).reclaim(
            caller_asserts_quiescence=True,
        )
    assert not tuple(workspace.iterdir())


def test_bm25_attempt_pool_coordinator_rejects_quiescence_contradiction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, _workspace, _topology_checks = _attempt_pool_target(tmp_path)
    child_name = f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}"

    class Reclaimer:
        def __init__(self, parent: Path, *, expected_parent_identity) -> None:
            assert parent == target.attempt_pool_root
            assert expected_parent_identity == target.attempt_pool_identity

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def snapshot_child_names(self) -> tuple[str, ...]:
            return (child_name,)

        def reclaim_child(self, name: str) -> bool:
            assert name == child_name
            return False

    monkeypatch.setattr(job_resources_module, "QuiescentDirectoryReclaimer", Reclaimer)

    with pytest.raises(StorageIntegrityError, match="despite quiescence"):
        LocalBM25AttemptPoolCoordinator(target).reclaim(
            caller_asserts_quiescence=True,
        )


def test_bm25_attempt_pool_coordinator_routes_only_discarded_lineage_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, _workspace, _topology_checks = _attempt_pool_target(tmp_path)
    live = f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}"
    discarded = f"..codenib-source-job-{_ATTEMPT}-context.discarded-{_DISCARD}"
    snapshots = iter(((discarded, live, "retain-me"), ("retain-me",)))
    calls: list[tuple[str, str]] = []

    class Reclaimer:
        def __init__(self, _parent: Path, *, expected_parent_identity) -> None:
            assert expected_parent_identity == target.attempt_pool_identity

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def snapshot_child_names(self) -> tuple[str, ...]:
            return next(snapshots)

        def reclaim_child(self, name: str) -> bool:
            calls.append(("live", name))
            return True

        def reclaim_quarantined_child(self, name: str) -> bool:
            calls.append(("discarded", name))
            return True

    monkeypatch.setattr(job_resources_module, "QuiescentDirectoryReclaimer", Reclaimer)

    result = LocalBM25AttemptPoolCoordinator(target).reclaim(
        caller_asserts_quiescence=True,
    )

    assert calls == [("discarded", discarded), ("live", live)]
    assert result.reclaimed_children == 2
    assert result.discarded_children == 1


def test_bm25_attempt_pool_coordinator_rejects_final_snapshot_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, _workspace, _topology_checks = _attempt_pool_target(tmp_path)
    snapshots = iter((("retain-me",), ("retain-me", "raced-child")))

    class Reclaimer:
        def __init__(self, _parent: Path, *, expected_parent_identity) -> None:
            assert expected_parent_identity == target.attempt_pool_identity

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def snapshot_child_names(self) -> tuple[str, ...]:
            return next(snapshots)

    monkeypatch.setattr(job_resources_module, "QuiescentDirectoryReclaimer", Reclaimer)

    with pytest.raises(StorageIntegrityError, match="changed during"):
        LocalBM25AttemptPoolCoordinator(target).reclaim(
            caller_asserts_quiescence=True,
        )


def test_bm25_attempt_pool_coordinator_rechecks_topology_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification_count = 0
    topology_lost = StorageIntegrityError("topology changed")

    def verify_topology() -> None:
        nonlocal verification_count
        verification_count += 1
        if verification_count == 4:
            raise topology_lost

    target, _workspace, _topology_checks = _attempt_pool_target(
        tmp_path,
        topology_verifier=verify_topology,
    )
    live = f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}"

    class Reclaimer:
        def __init__(self, _parent: Path, *, expected_parent_identity) -> None:
            assert expected_parent_identity == target.attempt_pool_identity

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def snapshot_child_names(self) -> tuple[str, ...]:
            return (live,)

        def reclaim_child(self, _name: str) -> bool:
            pytest.fail("topology loss must precede attempt mutation")

    monkeypatch.setattr(job_resources_module, "QuiescentDirectoryReclaimer", Reclaimer)

    with pytest.raises(StorageIntegrityError) as caught:
        LocalBM25AttemptPoolCoordinator(target).reclaim(
            caller_asserts_quiescence=True,
        )

    assert caught.value is topology_lost
    assert verification_count == 5


def test_bm25_attempt_pool_coordinator_rechecks_topology_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification_count = 0
    topology_lost = StorageIntegrityError("topology changed after failure")
    reclaim_failure = OSError("reclaim failed")

    def verify_topology() -> None:
        nonlocal verification_count
        verification_count += 1
        if verification_count == 5:
            raise topology_lost

    target, _workspace, _topology_checks = _attempt_pool_target(
        tmp_path,
        topology_verifier=verify_topology,
    )
    live = f".codenib-source-job-{_ATTEMPT}.normalize-{_STAGE}"

    class Reclaimer:
        def __init__(self, _parent: Path, *, expected_parent_identity) -> None:
            assert expected_parent_identity == target.attempt_pool_identity

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def snapshot_child_names(self) -> tuple[str, ...]:
            return (live,)

        def reclaim_child(self, _name: str) -> bool:
            raise reclaim_failure

    monkeypatch.setattr(job_resources_module, "QuiescentDirectoryReclaimer", Reclaimer)

    with pytest.raises(OSError) as caught:
        LocalBM25AttemptPoolCoordinator(target).reclaim(
            caller_asserts_quiescence=True,
        )

    assert caught.value is reclaim_failure
    assert verification_count >= 6
    assert any(
        "BM25 attempt-pool child reclamation topology validation also failed" in note
        and "topology changed after failure" in note
        for note in _cleanup_notes(reclaim_failure)
    )


def test_bm25_attempt_pool_types_are_lazy_compiler_exports() -> None:
    assert compiler_module.BM25AttemptPoolReclamation is BM25AttemptPoolReclamation
    assert (
        compiler_module.LocalBM25AttemptPoolCoordinator
        is LocalBM25AttemptPoolCoordinator
    )
