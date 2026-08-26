# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import pytest

import codenib.artifacts as artifacts_module
import codenib.artifacts.portable_views as portable_views_module
import codenib.artifacts.strict_context as strict_context_module
from codenib import LocalWorkspaceProvider
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
    run_strict_workspace,
)
from codenib.artifacts import (
    PlannedContextArtifact,
    plan_context_artifact_strict,
    publish_planned_context_artifact_strict,
    stage_context_artifact_strict,
    verify_context_artifact,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.artifact_integrity import VECTOR_PERSISTENCE_SCHEMA
from codenib.repository_filters import REPOSITORY_FILTER_POLICY_VERSION
from codenib.repository_source_selection import DEFAULT_REPOSITORY_SOURCE_SELECTION
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
    capture_repository_source,
)

_Result = TypeVar("_Result")
_COMMIT = "a" * 40
_SOURCE_SELECTION_CONFIG = {
    "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
    "source_selection_digest": DEFAULT_REPOSITORY_SOURCE_SELECTION.digest,
}
_VECTOR_CONFIG = {
    **_SOURCE_SELECTION_CONFIG,
    "builder_schema": 2,
    "embedding_model": "test/model",
    "embedding_provider": "huggingface",
    "embedding_dimension": 4,
    "dimension": 4,
    "embedding_kwargs": {},
    "index_metric": "ip",
}


class _ReadOnceMapping(Mapping[str, Any]):
    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = dict(values)
        self.iterations = 0

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("strict context input mapping was read twice")
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


def test_strict_context_symbols_are_public_package_exports() -> None:
    expected = {
        "PlannedContextArtifact",
        "PlannedContextView",
        "plan_context_artifact_strict",
        "publish_planned_context_artifact_strict",
        "stage_context_artifact_strict",
        "validate_content_bound_portable_query_view_reader",
    }

    assert expected <= set(artifacts_module.__all__)
    assert all(hasattr(artifacts_module, name) for name in artifacts_module.__all__)
    assert (
        artifacts_module.validate_content_bound_portable_query_view_reader
        is portable_views_module.validate_content_bound_portable_query_view_reader
    )

    assert list(inspect.signature(stage_context_artifact_strict).parameters) == [
        "destination",
        "manifest",
        "repository",
        "repository_source",
        "view_generations",
        "workspace_provider",
        "output_receipt_owner",
        "environ",
    ]
    assert list(
        inspect.signature(publish_planned_context_artifact_strict).parameters
    ) == [
        "destination",
        "planned",
        "manifest",
        "repository",
        "repository_source",
        "view_generations",
        "workspace_provider",
        "output_receipt_owner",
        "environ",
    ]


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _fingerprint(name: str, payload: bytes) -> dict[str, object]:
    return {
        "file": name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class _TestWorkspaceProvider:
    """Quiescent provisioner used only to exercise the real authority layer."""

    def __init__(
        self,
        *,
        workspace_plan: WorkspacePlan | None = None,
        support_hook: Callable[[], None] | None = None,
    ) -> None:
        self.workspace_plan = workspace_plan
        self.support_hook = support_hook
        self.support_count = 0
        self.run_count = 0
        self.requests: list[StrictWorkspaceRequest] = []

    def require_support(self) -> None:
        self.support_count += 1
        if self.support_hook is not None:
            self.support_hook()

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _Result],
        check_cancelled: Callable[[], None] | None = None,
    ) -> _Result:
        self.run_count += 1
        self.requests.append(request)
        if check_cancelled is not None:
            check_cancelled()
        plan = request.plan if self.workspace_plan is None else self.workspace_plan
        parent = request.destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / (
            f".{request.destination.name}.strict-{id(self):x}-{self.run_count}"
        )
        stage.mkdir(mode=plan.root_mode)
        for index, directory in enumerate(plan.directories):
            (stage / directory.path.as_posix()).mkdir(mode=directory.mode)
            if check_cancelled is not None and index + 1 < len(plan.directories):
                check_cancelled()

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        directory_descriptors = {
            directory.path.as_posix(): os.open(stage / directory.path.as_posix(), flags)
            for directory in plan.directories
        }
        workspace = OwnedWorkspaceAuthority()
        try:
            workspace.adopt(
                destination=request.destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors=directory_descriptors,
                plan=plan,
                destination_binding=None,
            )
            try:
                return run_adopted_workspace_operation(
                    request,
                    workspace=workspace,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:
                if receipt_owner.state == "empty":
                    workspace.close()
                raise
        finally:
            for descriptor in directory_descriptors.values():
                os.close(descriptor)
            os.close(root_descriptor)
            os.close(parent_descriptor)


def _publish_generation(
    destination: Path,
    files: Mapping[str, bytes],
) -> PublishedWorkspaceReceiptOwner:
    directories = {
        parent.as_posix()
        for relative in files
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    identity = [
        (relative, len(payload), hashlib.sha256(payload).hexdigest())
        for relative, payload in sorted(files.items())
    ]
    plan = WorkspacePlan(
        subject_digest=hashlib.sha256(_json_bytes(identity)).hexdigest(),
        directories=tuple(
            WorkspaceDirectory(PurePosixPath(path)) for path in sorted(directories)
        ),
        files=tuple(
            WorkspaceFile(PurePosixPath(relative), max_bytes=len(payload))
            for relative, payload in sorted(files.items())
        ),
    )
    request = StrictWorkspaceRequest(
        purpose="strict-context-test-source",
        destination=destination,
        plan=plan,
    )
    owner = PublishedWorkspaceReceiptOwner()

    def publish(session: StrictWorkspaceSession) -> None:
        for relative, payload in sorted(files.items()):
            session.write_file(relative, (payload,))
        session.publish_validated(lambda _reader: None)

    run_strict_workspace(
        _TestWorkspaceProvider(),
        request,
        receipt_owner=owner,
        operation=publish,
    )
    return owner


def _bm25_files() -> dict[str, bytes]:
    return {
        "bm25_metadata.json": _json_bytes(
            {"project_root": "source", "max_k": 17, "language": "english"}
        ),
        "documents.json": _json_bytes(
            [
                {
                    "page_content": "VALUE = 1",
                    "metadata": {"file": "sample.py", "node_id": "sample.VALUE"},
                }
            ]
        ),
    }


def _vector_files() -> dict[str, bytes]:
    documents_name = "documents_test__model.json"
    index_name = "index_test__model.faiss"
    documents = _json_bytes(
        [
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": "sample.py", "node_id": "sample.VALUE"},
            }
        ]
    )
    # Publication treats an authenticated native index as inert bytes.
    index = b"not-loaded-as-native-faiss\x00\x01"
    config = _json_bytes(
        {
            "embedding_model": "test/model",
            "embedding_provider": "huggingface",
            "dimension": 4,
            "index_type": "flat",
            "index_metric": "ip",
            "l0_documents": 0,
            "l2_documents": 1,
            "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
            "level_artifacts": {
                "l2": {
                    "documents": _fingerprint(documents_name, documents),
                    "index": _fingerprint(index_name, index),
                }
            },
        }
    )
    return {
        "config_test__model.json": config,
        f"l2/{documents_name}": documents,
        f"l2/{index_name}": index,
    }


def _entry(
    view: str,
    *,
    source_fingerprint: str,
) -> IndexEntry:
    config = dict(_SOURCE_SELECTION_CONFIG) if view == "bm25" else dict(_VECTOR_CONFIG)
    return IndexEntry(
        index_type=view,
        path=f"state/{view}",
        built_at="2026-01-01T00:00:00Z",
        built_at_epoch=1.0,
        status="fresh",
        config=config,
        metadata={},
        commit=_COMMIT,
        source_fingerprint=source_fingerprint,
    )


@dataclass
class _Fixture:
    root: Path
    repository: Path
    repository_source: Any
    manifest: RepoManifest
    owners: dict[str, PublishedWorkspaceReceiptOwner]

    def close(self) -> None:
        for owner in self.owners.values():
            owner.close()
        self.repository_source.close()


def _fixture(tmp_path: Path, views: tuple[str, ...]) -> _Fixture:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    repository_source = capture_repository_source(repository)
    owners: dict[str, PublishedWorkspaceReceiptOwner] = {}
    try:
        for view in views:
            files = _bm25_files() if view == "bm25" else _vector_files()
            owners[view] = _publish_generation(tmp_path / "state" / view, files)
        indexes = {
            view: _entry(view, source_fingerprint=repository_source.fingerprint)
            for view in views
        }
        manifest = RepoManifest(
            repo_path=str(repository),
            commit=_COMMIT,
            last_indexed_commit=_COMMIT,
            source_fingerprint=repository_source.fingerprint,
            last_indexed_source_fingerprint=repository_source.fingerprint,
            languages=["python"],
            file_count=repository_source.file_count,
            indexes=indexes,
            compiled_at="2026-01-01T00:00:00Z",
            compiled_at_epoch=1.0,
        )
        manifest.derive_capabilities()
        return _Fixture(
            root=tmp_path,
            repository=repository,
            repository_source=repository_source,
            manifest=manifest,
            owners=owners,
        )
    except BaseException:
        for owner in owners.values():
            owner.close()
        repository_source.close()
        raise


def _native_local_provider(root: Path) -> LocalWorkspaceProvider:
    root.mkdir(mode=0o700)
    provider = LocalWorkspaceProvider(root)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")
    return provider


@pytest.mark.parametrize(
    "views",
    [("bm25",), ("vector",), ("bm25", "vector")],
)
def test_strict_context_publishes_portable_view_combinations(
    tmp_path: Path,
    views: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, views)
    output_owner = PublishedWorkspaceReceiptOwner()
    destination = tmp_path / "published" / "context"
    provider = _TestWorkspaceProvider()

    def reject_native(*_args: object, **_kwargs: object) -> object:
        pytest.fail("strict context publication invoked native vector parsing")

    monkeypatch.setattr(portable_views_module, "_faiss_contract", reject_native)
    try:
        result = stage_context_artifact_strict(
            destination,
            manifest=fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=fixture.owners,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            environ={},
        )

        assert result.output_dir == destination
        assert result.views == tuple(sorted(views))
        assert output_owner.active
        assert all(owner.active for owner in fixture.owners.values())
        assert provider.support_count == 1
        assert provider.run_count == 1
        assert provider.requests[0].destination_expectation == "missing"
        verified = verify_context_artifact(
            destination,
            expected_repository="owner/repo",
            expected_commit=_COMMIT,
        )
        assert verified.views == tuple(sorted(views))
        assert verified.file_count == result.file_count
        assert verified.byte_count == result.byte_count
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_publishes_with_native_local_provider(tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    fixture = _fixture(input_root, ("bm25", "vector"))
    provider = _native_local_provider(tmp_path / "authority")
    output_owner = PublishedWorkspaceReceiptOwner()
    destination = provider.allowed_root / "context"
    try:
        result = stage_context_artifact_strict(
            destination,
            manifest=fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=fixture.owners,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            environ={},
        )

        assert result.output_dir == destination
        assert result.views == ("bm25", "vector")
        assert output_owner.active
        verified = verify_context_artifact(
            destination,
            expected_repository="owner/repo",
            expected_commit=_COMMIT,
        )
        assert verified.views == result.views
        assert verified.file_count == result.file_count
        assert verified.byte_count == result.byte_count
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_freezes_inputs_before_single_provider_support_call(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    fixture.manifest.indexes["bm25"].config["tokenizer"] = "frozen"
    generations = _ReadOnceMapping(fixture.owners)
    environment = _ReadOnceMapping({"SAFE_VALUE": "frozen"})
    output_owner = PublishedWorkspaceReceiptOwner()
    destination = tmp_path / "published" / "context"

    def mutate_caller_inputs() -> None:
        fixture.manifest.indexes["bm25"].config["tokenizer"] = "mutated"
        generations.values.clear()
        environment.values["SAFE_VALUE"] = "mutated"

    provider = _TestWorkspaceProvider(support_hook=mutate_caller_inputs)
    try:
        stage_context_artifact_strict(
            destination,
            manifest=fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=generations,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            environ=environment,
        )

        assert generations.iterations == 1
        assert environment.iterations == 1
        assert provider.support_count == 1
        assert provider.run_count == 1
        published = json.loads(
            (destination / "repo_manifest.json").read_text(encoding="utf-8")
        )
        assert published["indexes"]["bm25"]["config"]["tokenizer"] == "frozen"
        assert output_owner.active
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_uses_one_detached_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    binding = fixture.repository_source
    original_root = binding.root
    original_fingerprint = binding.fingerprint
    original_file_count = binding.file_count
    original_file_records = binding.file_records
    forged_root = tmp_path / "forged-public-root"
    snapshot_calls = 0
    observed_identities: list[RepositorySourceIdentitySnapshot] = []
    real_snapshot = RepositorySourceBinding.authenticated_identity_snapshot
    real_planned_view = strict_context_module._planned_view

    def counted_snapshot(self):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return real_snapshot(self)

    def mutate_public_projection(view, owner, *, manifest, inputs):
        assert type(inputs.repository_identity) is RepositorySourceIdentitySnapshot
        observed_identities.append(inputs.repository_identity)
        binding.root = forged_root
        binding.fingerprint = f"sha256-v2:{'0' * 64}"
        binding.file_count = 0
        binding.file_records = ()
        try:
            return real_planned_view(
                view,
                owner,
                manifest=manifest,
                inputs=inputs,
            )
        finally:
            binding.root = original_root
            binding.fingerprint = original_fingerprint
            binding.file_count = original_file_count
            binding.file_records = original_file_records

    monkeypatch.setattr(
        RepositorySourceBinding,
        "authenticated_identity_snapshot",
        counted_snapshot,
    )
    monkeypatch.setattr(
        strict_context_module,
        "_planned_view",
        mutate_public_projection,
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    destination = tmp_path / "published" / "context"
    try:
        stage_context_artifact_strict(
            destination,
            manifest=fixture.manifest,
            repository="owner/repo",
            repository_source=binding,
            view_generations=fixture.owners,
            workspace_provider=_TestWorkspaceProvider(),
            output_receipt_owner=output_owner,
            environ={},
        )
        assert snapshot_calls == 1
        assert len(observed_identities) == 1
        assert observed_identities[0].root == original_root
        assert observed_identities[0].fingerprint == original_fingerprint
        assert output_owner.active
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_plan_is_deterministic_and_binds_manifest_config(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, ("bm25", "vector"))
    try:
        first = plan_context_artifact_strict(
            fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=fixture.owners,
            environ={},
        )
        repeated = plan_context_artifact_strict(
            fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=dict(reversed(tuple(fixture.owners.items()))),
            environ={},
        )
        assert first == repeated
        assert isinstance(first, PlannedContextArtifact)
        assert first.views == ("bm25", "vector")

        fixture.manifest.indexes["bm25"].config["tokenizer"] = "changed"
        changed = plan_context_artifact_strict(
            fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=fixture.owners,
            environ={},
        )
        assert changed.plan.subject_digest != first.plan.subject_digest
        assert changed.manifest_digest != first.manifest_digest
    finally:
        fixture.close()


def test_strict_context_plan_stops_before_source_record_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    source_records = tuple(
        strict_context_module.directory_ownership_file_records(
            fixture.owners["bm25"].receipt.ownership
        )
    )
    assert len(source_records) == 2
    first_path, poisoned_path = (record.path for record in source_records)
    interruption = RuntimeError("injected strict context planning stop")
    poison_consumed = False
    armed = False
    real_record_payload = strict_context_module._record_payload

    def guarded_record_payload(record):
        nonlocal armed, poison_consumed
        if record.path == poisoned_path and armed:
            poison_consumed = True
            raise AssertionError("strict context planning consumed its poisoned tail")
        payload = real_record_payload(record)
        if record.path == first_path:
            armed = True
        return payload

    def check_cancelled() -> None:
        if armed:
            raise interruption

    monkeypatch.setattr(
        strict_context_module,
        "_record_payload",
        guarded_record_payload,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            strict_context_module._plan_context_artifact_strict_interruptibly(
                fixture.manifest,
                repository="owner/repo",
                repository_source=fixture.repository_source,
                view_generations=fixture.owners,
                environ={},
                check_cancelled=check_cancelled,
            )

        assert caught.value is interruption
        assert not poison_consumed
        assert fixture.repository_source.usable
        assert all(owner.active for owner in fixture.owners.values())
    finally:
        fixture.close()


def test_strict_context_expected_plan_mismatch_precedes_terminal_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    planned = plan_context_artifact_strict(
        fixture.manifest,
        repository="owner/repo",
        repository_source=fixture.repository_source,
        view_generations=fixture.owners,
        environ={},
    )
    forged = replace(planned, repository="other/repo")
    inputs = strict_context_module._freeze_inputs(
        fixture.manifest,
        repository="owner/repo",
        repository_source=fixture.repository_source,
        view_generations=fixture.owners,
        environ={},
    )
    real_artifact_type = strict_context_module.PlannedContextArtifact
    interruption = RuntimeError("terminal strict context planning stop")
    terminal_polls = 0
    armed = False

    def construct_and_arm(*args: object, **kwargs: object) -> PlannedContextArtifact:
        nonlocal armed
        result = real_artifact_type(*args, **kwargs)
        armed = True
        return result

    def check_cancelled() -> None:
        nonlocal terminal_polls
        if armed:
            terminal_polls += 1
            raise interruption

    monkeypatch.setattr(
        strict_context_module,
        "PlannedContextArtifact",
        construct_and_arm,
    )
    try:
        with pytest.raises(ValueError, match="exact inputs"):
            strict_context_module._require_expected_plan(
                forged,
                inputs,
                check_cancelled=check_cancelled,
            )

        assert terminal_polls == 0
        assert fixture.repository_source.usable
        assert all(owner.active for owner in fixture.owners.values())
    finally:
        fixture.close()


def test_strict_context_vector_plan_resolves_artifact_route_without_ambient_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("vector",))
    real_resolve = strict_context_module.resolve_embedding_artifact_route
    calls: list[Mapping[str, str] | None] = []

    def resolve_without_ambient_env(
        config: Mapping[str, Any],
        *,
        credential_env: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Any:
        calls.append(environ)
        assert environ == {}
        return real_resolve(
            config,
            credential_env=credential_env,
            environ=environ,
        )

    monkeypatch.setattr(
        strict_context_module,
        "resolve_embedding_artifact_route",
        resolve_without_ambient_env,
    )
    try:
        plan_context_artifact_strict(
            fixture.manifest,
            repository="owner/repo",
            repository_source=fixture.repository_source,
            view_generations=fixture.owners,
            environ={},
        )

        assert calls == [{}]
    finally:
        fixture.close()


def test_strict_context_planned_publish_rejects_forgery_and_input_drift(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    planned = plan_context_artifact_strict(
        fixture.manifest,
        repository="owner/repo",
        repository_source=fixture.repository_source,
        view_generations=fixture.owners,
        environ={},
    )
    destination = tmp_path / "published" / "context"
    try:
        forged = replace(planned, repository="other/repo")
        output_owner = PublishedWorkspaceReceiptOwner()
        provider = _TestWorkspaceProvider()
        try:
            with pytest.raises(ValueError, match="exact inputs"):
                publish_planned_context_artifact_strict(
                    destination,
                    planned=forged,
                    manifest=fixture.manifest,
                    repository="owner/repo",
                    repository_source=fixture.repository_source,
                    view_generations=fixture.owners,
                    workspace_provider=provider,
                    output_receipt_owner=output_owner,
                    environ={},
                )
            assert provider.run_count == 0
            assert output_owner.state == "empty"
            assert not destination.exists()
        finally:
            output_owner.close()

        fixture.manifest.indexes["bm25"].config["tokenizer"] = "changed"
        output_owner = PublishedWorkspaceReceiptOwner()
        provider = _TestWorkspaceProvider()
        try:
            with pytest.raises(ValueError, match="exact inputs"):
                publish_planned_context_artifact_strict(
                    destination,
                    planned=planned,
                    manifest=fixture.manifest,
                    repository="owner/repo",
                    repository_source=fixture.repository_source,
                    view_generations=fixture.owners,
                    workspace_provider=provider,
                    output_receipt_owner=output_owner,
                    environ={},
                )
            assert provider.run_count == 0
            assert output_owner.state == "empty"
            assert not destination.exists()
        finally:
            output_owner.close()
    finally:
        fixture.close()


def test_strict_context_rejects_invalid_destination_before_source_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    try:
        with pytest.raises(ValueError, match="destination overlaps"):
            stage_context_artifact_strict(
                fixture.repository / "context",
                manifest=fixture.manifest,
                repository="owner/repo",
                repository_source=fixture.repository_source,
                view_generations=fixture.owners,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                environ={},
            )
        assert consume_calls == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert not (fixture.repository / "context").exists()
    finally:
        output_owner.close()
        fixture.close()


@pytest.mark.parametrize("kind", ["directory", "file", "dangling-symlink"])
def test_strict_context_rejects_existing_destination_before_source_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    destination = tmp_path / "published" / "context"
    destination.parent.mkdir()
    if kind == "directory":
        destination.mkdir()
    elif kind == "file":
        destination.write_bytes(b"occupied")
    else:
        destination.symlink_to(tmp_path / "absent-target")
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    try:
        with pytest.raises(ValueError, match="destination must be missing"):
            stage_context_artifact_strict(
                destination,
                manifest=fixture.manifest,
                repository="owner/repo",
                repository_source=fixture.repository_source,
                view_generations=fixture.owners,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                environ={},
            )
        assert consume_calls == 0
        assert provider.run_count == 0
        assert provider.support_count == 0
        assert output_owner.state == "empty"
        assert os.path.lexists(destination)
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_planned_publish_rejects_existing_destination_before_replan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    planned = plan_context_artifact_strict(
        fixture.manifest,
        repository="owner/repo",
        repository_source=fixture.repository_source,
        view_generations=fixture.owners,
        environ={},
    )
    destination = tmp_path / "published" / "context"
    destination.parent.mkdir()
    destination.write_bytes(b"occupied")
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    try:
        with pytest.raises(ValueError, match="destination must be missing"):
            publish_planned_context_artifact_strict(
                destination,
                planned=planned,
                manifest=fixture.manifest,
                repository="owner/repo",
                repository_source=fixture.repository_source,
                view_generations=fixture.owners,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                environ={},
            )
        assert consume_calls == 0
        assert provider.run_count == 0
        assert provider.support_count == 0
        assert output_owner.state == "empty"
        assert destination.read_bytes() == b"occupied"
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_published_source_drift_rolls_back_missing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    output_owner = PublishedWorkspaceReceiptOwner()
    destination = tmp_path / "published" / "context"
    provider = _TestWorkspaceProvider()
    real_validate = strict_context_module._validate_candidate
    validations = 0

    def validate_then_drift(*args: object, **kwargs: object) -> None:
        nonlocal validations
        real_validate(*args, **kwargs)
        validations += 1
        if validations == 2:
            (fixture.repository / "sample.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )

    monkeypatch.setattr(
        strict_context_module, "_validate_candidate", validate_then_drift
    )
    try:
        with pytest.raises(
            Exception,
            match="changed|failed validation|quarantined",
        ):
            stage_context_artifact_strict(
                destination,
                manifest=fixture.manifest,
                repository="owner/repo",
                repository_source=fixture.repository_source,
                view_generations=fixture.owners,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                environ={},
            )
        assert validations == 2
        assert not destination.exists()
        # The atomic layer retains the quarantined generation as explicit,
        # caller-cleanable authority rather than degrading it to an empty slot.
        assert output_owner.state == "cleanup"
        assert fixture.owners["bm25"].active
        output_owner.close()
        assert output_owner.closed
    finally:
        output_owner.close()
        fixture.close()


def test_strict_context_provider_plan_substitution_has_no_publication(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, ("bm25",))
    planned = plan_context_artifact_strict(
        fixture.manifest,
        repository="owner/repo",
        repository_source=fixture.repository_source,
        view_generations=fixture.owners,
        environ={},
    )
    wrong_plan = WorkspacePlan(subject_digest=hashlib.sha256(b"wrong").hexdigest())
    provider = _TestWorkspaceProvider(workspace_plan=wrong_plan)
    output_owner = PublishedWorkspaceReceiptOwner()
    destination = tmp_path / "published" / "context"
    try:
        with pytest.raises(ValueError, match="plan differs"):
            publish_planned_context_artifact_strict(
                destination,
                planned=planned,
                manifest=fixture.manifest,
                repository="owner/repo",
                repository_source=fixture.repository_source,
                view_generations=fixture.owners,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                environ={},
            )
        assert not destination.exists()
        assert output_owner.state == "empty"
        assert fixture.owners["bm25"].active
    finally:
        output_owner.close()
        fixture.close()
