# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest

import codenib.artifacts.strict_bm25 as strict_bm25_module
from codenib import LocalWorkspaceProvider
from codenib._atomic_directory import TreeFileRecord
from codenib._bounded_json import canonical_json_value_chunks
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
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
    PlannedBm25View,
    normalize_owned_query_view_strict,
    plan_bm25_view_strict,
    publish_planned_bm25_view_strict,
    recapture_bm25_view_strict,
    validate_portable_query_view,
)
from codenib.artifacts.strict_bm25 import _record_chunks
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
    capture_repository_source,
)

_Result = TypeVar("_Result")
_Value = TypeVar("_Value")


def _canonical_bytes(value: Any) -> bytes:
    return b"".join(canonical_json_value_chunks(value)) + b"\n"


class _ReadOnceMapping(Mapping[str, _Value]):
    def __init__(self, values: dict[str, _Value]) -> None:
        self.values = values
        self.iterations = 0

    def __getitem__(self, key: str) -> _Value:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("caller mapping was read more than once")
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


class _TestWorkspaceProvider:
    """Quiescent test provisioner; not a production security boundary."""

    def __init__(
        self,
        *,
        expected_destination: object | None,
        workspace_plan: WorkspacePlan | None = None,
        support_hook: Callable[[], None] | None = None,
    ) -> None:
        self.expected_destination = expected_destination
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
    ) -> _Result:
        self.run_count += 1
        self.requests.append(request)
        plan = request.plan if self.workspace_plan is None else self.workspace_plan
        parent = request.destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / (
            f".{request.destination.name}.strict-{id(self):x}-{self.run_count}"
        )
        stage.mkdir(mode=plan.root_mode)
        for directory in plan.directories:
            (stage / directory.path.as_posix()).mkdir(mode=directory.mode)

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        directory_descriptors = {
            directory.path.as_posix(): os.open(
                stage / directory.path.as_posix(),
                flags,
            )
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
                expected_destination=self.expected_destination,
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


def _repository(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    return repository


def _publish_source(
    root: Path,
    repository: Path,
    *,
    documents: bytes | None = None,
    metadata: bytes | None = None,
) -> tuple[Path, PublishedWorkspaceReceiptOwner]:
    destination = root / "state" / "bm25"
    documents = documents or _canonical_bytes(
        [
            {
                "page_content": "alpha VALUE omega",
                "metadata": {
                    "file": str(repository / "sample.py"),
                    "node_id": "sample.VALUE",
                },
            },
            {
                "page_content": "beta helper",
                "metadata": {"file": "sample.py", "node_id": "sample.helper"},
            },
        ]
    )
    metadata = metadata or _canonical_bytes(
        {
            "project_root": str(repository),
            "max_k": 17,
            "language": "english",
        }
    )
    plan = WorkspacePlan(
        subject_digest=hashlib.sha256(b"strict-bm25-source").hexdigest(),
        files=(
            WorkspaceFile("documents.json", max_bytes=len(documents)),
            WorkspaceFile("bm25_metadata.json", max_bytes=len(metadata)),
        ),
    )
    request = StrictWorkspaceRequest(
        purpose="portable-bm25-test-source",
        destination=destination,
        plan=plan,
    )
    owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)

    def publish(session: StrictWorkspaceSession) -> None:
        written = session.write_file("documents.json", (documents,))
        assert written == TreeFileRecord(
            path="documents.json",
            mode=0o600,
            size=len(documents),
            sha256=hashlib.sha256(documents).hexdigest(),
        )
        session.write_file("bm25_metadata.json", (metadata,))
        session.publish_validated(lambda _reader: None)

    run_strict_workspace(
        provider,
        request,
        receipt_owner=owner,
        operation=publish,
    )
    return destination, owner


def _write_raw_bm25_source(
    source: Path,
    repository: Path,
) -> tuple[bytes, bytes]:
    documents = _canonical_bytes(
        [
            {
                "page_content": "alpha VALUE omega",
                "metadata": {
                    "file": str(repository / "sample.py"),
                    "node_id": "sample.VALUE",
                },
            },
            {
                "page_content": "beta helper",
                "metadata": {"file": "sample.py", "node_id": "sample.helper"},
            },
        ]
    )
    metadata = _canonical_bytes(
        {
            "project_root": str(repository),
            "max_k": 17,
            "language": "english",
        }
    )
    source.mkdir(parents=True)
    (source / "documents.json").write_bytes(documents)
    (source / "bm25_metadata.json").write_bytes(metadata)
    return documents, metadata


@pytest.fixture
def strict_generation(tmp_path: Path):
    repository = _repository(tmp_path)
    destination, source_owner = _publish_source(tmp_path, repository)
    repository_source = capture_repository_source(repository)
    try:
        yield SimpleNamespace(
            repository=repository,
            destination=destination,
            source_owner=source_owner,
            repository_source=repository_source,
        )
    finally:
        source_owner.close()
        repository_source.close()


def test_strict_bm25_plans_replays_and_serves_same_query_semantics(
    strict_generation,
) -> None:
    fixture = strict_generation
    view_config = {"builder_schema": 2, "tokenizer": "code-aware-v1"}
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config=view_config,
        environ={},
    )
    assert isinstance(planned, PlannedBm25View)
    assert tuple(record.path for record in planned.output_records) == (
        "bm25_metadata.json",
        "documents.json",
    )
    assert tuple(file.path.as_posix() for file in planned.plan.files) == (
        "bm25_metadata.json",
        "documents.json",
    )

    source_documents = json.loads(
        (fixture.destination / "documents.json").read_text(encoding="utf-8")
    )
    source_metadata = json.loads(
        (fixture.destination / "bm25_metadata.json").read_text(encoding="utf-8")
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    try:
        adjustments = publish_planned_bm25_view_strict(
            fixture.destination,
            planned=planned,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config=view_config,
            environ={},
        )
        assert provider.run_count == 1
        assert provider.support_count == 1
        assert provider.requests[0].destination_expectation == "provider-bound-exact"
        assert output_owner.active
        assert fixture.source_owner.active
        assert output_owner.receipt.plan == planned.plan

        output_documents = json.loads(
            (fixture.destination / "documents.json").read_text(encoding="utf-8")
        )
        output_metadata = json.loads(
            (fixture.destination / "bm25_metadata.json").read_text(encoding="utf-8")
        )
        assert [item["page_content"] for item in output_documents] == [
            item["page_content"] for item in source_documents
        ]
        assert output_documents[0]["metadata"]["file"] == "sample.py"
        assert output_metadata == {
            "project_root": "source",
            "max_k": source_metadata["max_k"],
            "language": source_metadata["language"],
        }
        validate_portable_query_view(
            fixture.destination,
            repo_path=fixture.repository,
            view_type="bm25",
            view_config={**view_config, **adjustments},
            environ={},
        )

        before = BM25CodeIndexer()
        before.load_index_values(source_documents, source_metadata)
        after = BM25CodeIndexer()
        after.load_index_values(output_documents, output_metadata)
        assert [doc.page_content for doc in before.retriever.invoke("VALUE")] == [
            doc.page_content for doc in after.retriever.invoke("VALUE")
        ]
    finally:
        output_owner.close()


@pytest.mark.parametrize(
    ("view_config", "message"),
    [
        ({"builder_schema": 8}, "requires a source selection digest"),
        (
            {
                "builder_schema": 8,
                "source_selection_digest": RepositorySourceSelection(
                    ("generated",)
                ).digest,
            },
            "source selection differs",
        ),
    ],
)
def test_current_bm25_selection_is_bound_before_source_consumption(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
    view_config: dict[str, Any],
    message: str,
) -> None:
    fixture = strict_generation
    monkeypatch.setattr(
        strict_bm25_module,
        "_planned_view_from_publication",
        lambda *_args, **_kwargs: pytest.fail("source generation must not be consumed"),
    )

    with pytest.raises(ValueError, match=message):
        plan_bm25_view_strict(
            fixture.source_owner,
            repository_source=fixture.repository_source,
            view_config=view_config,
            environ={},
        )

    assert fixture.source_owner.active
    assert fixture.repository_source.usable


def test_strict_bm25_recaptures_raw_directory_with_missing_test_provider(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    destination = tmp_path / "strict-output" / "bm25"
    source_documents, source_metadata = _write_raw_bm25_source(source, repository)
    repository_source = capture_repository_source(repository)
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)
    view_config = {"builder_schema": 2, "tokenizer": "code-aware-v1"}
    try:
        planned = strict_bm25_module._plan_recaptured_bm25_view(
            source,
            destination,
            repository_source=repository_source,
            view_config=view_config,
            environ={},
        )
        assert isinstance(planned, PlannedBm25View)
        assert not destination.exists()
        assert planned.output_records == (
            TreeFileRecord(
                path="bm25_metadata.json",
                mode=0o600,
                size=71,
                sha256=(
                    "579b6c52fa161af428ca5fc99969fbe4eb7f68c28ff00aadfda3c1522be3bcad"
                ),
            ),
            TreeFileRecord(
                path="documents.json",
                mode=0o600,
                size=264,
                sha256=(
                    "31da28af246dec6b8069d7128da2f2f108c514dcba9718c1faa3bb165eb81a41"
                ),
            ),
        )

        adjustments = strict_bm25_module._publish_recaptured_bm25_view(
            source,
            destination,
            planned=planned,
            repository_source=repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config=view_config,
            environ={},
        )

        assert provider.support_count == 1
        assert provider.run_count == 1
        assert provider.requests[0].destination_expectation == "missing"
        assert provider.requests[0].plan == planned.plan
        assert output_owner.active
        assert output_owner.receipt.path == destination
        assert output_owner.receipt.plan == planned.plan
        assert adjustments == planned.adjustments
        assert (source / "documents.json").read_bytes() == source_documents
        assert (source / "bm25_metadata.json").read_bytes() == source_metadata
        assert json.loads(
            (destination / "bm25_metadata.json").read_text(encoding="utf-8")
        ) == {
            "project_root": "source",
            "max_k": 17,
            "language": "english",
        }
        output_documents = json.loads(
            (destination / "documents.json").read_text(encoding="utf-8")
        )
        assert [item["metadata"]["file"] for item in output_documents] == [
            "sample.py",
            "sample.py",
        ]
        validate_portable_query_view(
            destination,
            repo_path=repository,
            view_type="bm25",
            view_config={**view_config, **adjustments},
            environ={},
        )
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_has_public_package_export() -> None:
    assert recapture_bm25_view_strict is strict_bm25_module.recapture_bm25_view_strict


def test_strict_bm25_public_recapture_uses_local_missing_provider(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    _write_raw_bm25_source(source, repository)
    workspace_root = tmp_path / "private-workspaces"
    workspace_root.mkdir(mode=0o700)
    os.chmod(workspace_root, 0o700)
    destination = workspace_root / "published-bm25"
    provider = LocalWorkspaceProvider(workspace_root)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")
    repository_source = capture_repository_source(repository)
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        adjustments = strict_bm25_module.recapture_bm25_view_strict(
            source,
            destination,
            repository_source=repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config={"builder_schema": 2},
            environ={},
        )
        assert output_owner.active
        assert output_owner.receipt.path == destination
        assert set(adjustments["artifact_file_fingerprints"]) == {
            "bm25_metadata.json",
            "documents.json",
        }
        assert destination.is_dir()
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_accepts_excluded_source_inside_repository(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = repository / "compiler-cache" / "bm25"
    _write_raw_bm25_source(source, repository)
    repository_source = capture_repository_source(
        repository,
        exclude_roots=(source,),
    )
    destination = tmp_path / "strict-output" / "bm25"
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)
    try:
        identity = repository_source.authenticated_identity_snapshot()
        assert not any(
            record.path.startswith("compiler-cache/bm25/")
            for record in identity.file_records
        )
        strict_bm25_module.recapture_bm25_view_strict(
            source,
            destination,
            repository_source=repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config={},
            environ={},
        )
        assert output_owner.active
        assert provider.run_count == 1
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_rejects_included_repository_source_before_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    source = repository / "compiler-cache" / "bm25"
    _write_raw_bm25_source(source, repository)
    repository_source = capture_repository_source(repository)
    destination = tmp_path / "strict-output" / "bm25"

    def forbidden_capture(*_args, **_kwargs):
        raise AssertionError("source capture ran before repository exclusion proof")

    monkeypatch.setattr(
        strict_bm25_module,
        "capture_directory_ownership",
        forbidden_capture,
    )
    try:
        with pytest.raises(ValueError, match="must be excluded"):
            strict_bm25_module._plan_recaptured_bm25_view(
                source,
                destination,
                repository_source=repository_source,
                view_config={},
                environ={},
            )
        assert not destination.exists()
    finally:
        repository_source.close()


@pytest.mark.parametrize("destination_kind", ["existing", "repository"])
def test_strict_bm25_recapture_rejects_destination_preflight_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_kind: str,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    source_documents, source_metadata = _write_raw_bm25_source(source, repository)
    repository_source = capture_repository_source(repository)
    if destination_kind == "existing":
        destination = tmp_path / "occupied"
        destination.mkdir()
        marker = destination / "marker"
        marker.write_text("retained\n", encoding="utf-8")
    else:
        destination = repository / "strict-output"
        marker = None
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)

    def forbidden_capture(*_args, **_kwargs):
        raise AssertionError("source capture ran after invalid destination preflight")

    monkeypatch.setattr(
        strict_bm25_module,
        "capture_directory_ownership",
        forbidden_capture,
    )
    try:
        with pytest.raises((FileExistsError, ValueError), match="destination"):
            strict_bm25_module.recapture_bm25_view_strict(
                source,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert provider.run_count == 0
        assert provider.support_count == 0
        assert output_owner.state == "empty"
        assert (source / "documents.json").read_bytes() == source_documents
        assert (source / "bm25_metadata.json").read_bytes() == source_metadata
        if marker is not None:
            assert marker.read_text(encoding="utf-8") == "retained\n"
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_rejects_source_drift_before_provider(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    _write_raw_bm25_source(source, repository)
    destination = tmp_path / "strict-output" / "bm25"
    repository_source = capture_repository_source(repository)
    planned = strict_bm25_module._plan_recaptured_bm25_view(
        source,
        destination,
        repository_source=repository_source,
        view_config={},
        environ={},
    )
    (source / "documents.json").write_bytes(_canonical_bytes([]))
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)
    try:
        with pytest.raises(ValueError, match="another source generation"):
            strict_bm25_module._publish_recaptured_bm25_view(
                source,
                destination,
                planned=planned,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert provider.run_count == 0
        assert provider.support_count == 0
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_rejects_plan_tamper_before_source_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    _write_raw_bm25_source(source, repository)
    destination = tmp_path / "strict-output" / "bm25"
    repository_source = capture_repository_source(repository)
    planned = strict_bm25_module._plan_recaptured_bm25_view(
        source,
        destination,
        repository_source=repository_source,
        view_config={},
        environ={},
    )
    tampered = replace(
        planned,
        plan=WorkspacePlan(
            subject_digest=hashlib.sha256(b"tampered-recapture-plan").hexdigest(),
            files=planned.plan.files,
        ),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)

    def forbidden_capture(*_args, **_kwargs):
        raise AssertionError("source was recaptured before plan validation")

    monkeypatch.setattr(
        strict_bm25_module,
        "capture_directory_ownership",
        forbidden_capture,
    )
    try:
        with pytest.raises(ValueError, match="not canonical"):
            strict_bm25_module._publish_recaptured_bm25_view(
                source,
                destination,
                planned=tampered,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_detects_tamper_at_reopen_and_rolls_back(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    _write_raw_bm25_source(source, repository)
    destination = tmp_path / "strict-output" / "bm25"
    repository_source = capture_repository_source(repository)
    planned = strict_bm25_module._plan_recaptured_bm25_view(
        source,
        destination,
        repository_source=repository_source,
        view_config={},
        environ={},
    )

    def tamper_before_workspace() -> None:
        (source / "documents.json").write_bytes(_canonical_bytes([]))

    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=None,
        support_hook=tamper_before_workspace,
    )
    try:
        with pytest.raises(RuntimeError, match="differs|changed"):
            strict_bm25_module._publish_recaptured_bm25_view(
                source,
                destination,
                planned=planned,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert provider.support_count == 1
        assert provider.run_count == 1
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_recapture_rejects_forbidden_policy_postflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    source = tmp_path / "legacy-bm25"
    _write_raw_bm25_source(source, repository)
    destination = tmp_path / "strict-output" / "bm25"
    forbidden = tmp_path / "private-build-root"
    repository_source = capture_repository_source(repository)
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(expected_destination=None)
    candidate_calls = 0
    real_candidate_records = strict_bm25_module._candidate_records

    def reject_published_candidate(*args, **kwargs):
        nonlocal candidate_calls
        assert kwargs["forbidden_paths"] == (repository, forbidden)
        records = real_candidate_records(*args, **kwargs)
        candidate_calls += 1
        if candidate_calls == 2:
            raise ValueError("strict BM25 forbidden path postflight failed")
        return records

    monkeypatch.setattr(
        strict_bm25_module,
        "_candidate_records",
        reject_published_candidate,
    )
    try:
        with pytest.raises(
            (RuntimeError, ValueError),
            match="forbidden path postflight|identity failed validation",
        ):
            strict_bm25_module.recapture_bm25_view_strict(
                source,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                forbidden_paths=(forbidden,),
                environ={},
            )
        assert candidate_calls == 2
        assert provider.run_count == 1
        assert not output_owner.active
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_bm25_rejects_missing_only_local_provider_before_mutation(
    strict_generation,
    tmp_path: Path,
) -> None:
    fixture = strict_generation
    view_config = {"builder_schema": 2, "tokenizer": "code-aware-v1"}
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config=view_config,
        environ={},
    )
    os.chmod(tmp_path, 0o700)
    provider = LocalWorkspaceProvider(tmp_path)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")
    output_owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(
        UnsupportedWorkspaceCreation,
        match="requires a missing destination",
    ):
        publish_planned_bm25_view_strict(
            fixture.destination,
            planned=planned,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config=view_config,
            environ={},
        )

    assert output_owner.state == "empty"
    assert fixture.source_owner.active
    assert not tuple(tmp_path.glob(".codenib-workspace-stage-*"))
    assert not tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_strict_bm25_high_level_freezes_caller_mappings_once(
    strict_generation,
) -> None:
    fixture = strict_generation
    config = _ReadOnceMapping({"builder_schema": 2})
    environment = _ReadOnceMapping({"SAFE_VALUE": "public"})
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    try:
        normalize_owned_query_view_strict(
            fixture.destination,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_type="bm25",
            view_config=config,
            environ=environment,
        )
        assert config.iterations == 1
        assert environment.iterations == 1
        assert provider.support_count == 1
        assert output_owner.active
    finally:
        output_owner.close()


def test_strict_bm25_high_level_uses_one_detached_source_identity(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = strict_generation
    binding = fixture.repository_source
    original_root = binding.root
    original_fingerprint = binding.fingerprint
    original_file_count = binding.file_count
    original_file_records = binding.file_records
    forged_root = tmp_path / "forged-public-root"
    snapshot_calls = 0
    policy_identities: list[RepositorySourceIdentitySnapshot] = []
    real_snapshot = RepositorySourceBinding.authenticated_identity_snapshot
    real_policy = strict_bm25_module._policy

    def counted_snapshot(self):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return real_snapshot(self)

    def mutate_public_projection(repository_identity, *args, **kwargs):
        assert type(repository_identity) is RepositorySourceIdentitySnapshot
        policy_identities.append(repository_identity)
        binding.root = forged_root
        binding.fingerprint = f"sha256-v2:{'0' * 64}"
        binding.file_count = 0
        binding.file_records = ()
        try:
            return real_policy(repository_identity, *args, **kwargs)
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
    monkeypatch.setattr(strict_bm25_module, "_policy", mutate_public_projection)
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    try:
        normalize_owned_query_view_strict(
            fixture.destination,
            source_generation=fixture.source_owner,
            repository_source=binding,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_type="bm25",
            view_config={},
            environ={},
        )
        assert snapshot_calls == 1
        assert len(policy_identities) == 2
        assert policy_identities[0] is policy_identities[1]
        assert policy_identities[0].root == original_root
        assert policy_identities[0].fingerprint == original_fingerprint
        assert output_owner.active
    finally:
        output_owner.close()


def test_strict_bm25_plan_binds_view_config_into_subject(
    strict_generation,
) -> None:
    fixture = strict_generation
    first = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={"tokenizer": "first"},
        environ={},
    )
    repeated = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={"tokenizer": "first"},
        environ={},
    )
    changed = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={"tokenizer": "second"},
        environ={},
    )
    assert first == repeated
    assert first.output_records == changed.output_records
    assert first.view_config_digest != changed.view_config_digest
    assert first.plan.subject_digest != changed.plan.subject_digest


def test_strict_bm25_invokes_provider_support_once_after_freezing_inputs(
    strict_generation,
) -> None:
    fixture = strict_generation
    view_config = {"tokenizer": "initial"}
    environ = {"CODENIB_STRICT_TEST": "initial"}
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config=view_config,
        environ=environ,
    )
    output_owner = PublishedWorkspaceReceiptOwner()

    def mutate_caller_inputs() -> None:
        view_config["tokenizer"] = "mutated"
        environ["CODENIB_STRICT_TEST"] = "mutated"

    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
        support_hook=mutate_caller_inputs,
    )
    try:
        publish_planned_bm25_view_strict(
            fixture.destination,
            planned=planned,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config=view_config,
            environ=environ,
        )
        assert provider.support_count == 1
        assert provider.run_count == 1
        assert view_config == {"tokenizer": "mutated"}
        assert environ == {"CODENIB_STRICT_TEST": "mutated"}
        assert output_owner.active
    finally:
        output_owner.close()


def test_strict_bm25_rejects_invalid_authority_before_source_consume(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = strict_generation
    output_owner = PublishedWorkspaceReceiptOwner()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)

    class InvalidProvider:
        @property
        def require_support(self):
            raise RuntimeError("invalid provider getter reached")

    class ForgedSourceOwner(PublishedWorkspaceReceiptOwner):
        pass

    forged_source = ForgedSourceOwner()

    with pytest.raises(TypeError, match="output receipt owner"):
        normalize_owned_query_view_strict(
            fixture.destination,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=object(),
            output_receipt_owner=None,  # type: ignore[arg-type]
            view_type="bm25",
            view_config={},
        )
    with pytest.raises(TypeError, match="provider has an invalid contract"):
        normalize_owned_query_view_strict(
            fixture.destination,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=InvalidProvider(),  # type: ignore[arg-type]
            output_receipt_owner=output_owner,
            view_type="bm25",
            view_config={},
        )
    with pytest.raises(ValueError, match="source and output"):
        normalize_owned_query_view_strict(
            fixture.destination,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=object(),
            output_receipt_owner=fixture.source_owner,
            view_type="bm25",
            view_config={},
        )
    with pytest.raises(TypeError, match="source must"):
        normalize_owned_query_view_strict(
            fixture.destination,
            source_generation=forged_source,
            repository_source=fixture.repository_source,
            workspace_provider=object(),
            output_receipt_owner=output_owner,
            view_type="bm25",
            view_config={},
        )
    assert consume_calls == 0
    assert output_owner.state == "empty"
    forged_source.close()
    output_owner.close()


def test_strict_bm25_shared_support_gate_precedes_provider_getters(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = strict_generation
    output_owner = PublishedWorkspaceReceiptOwner()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    class ExplosiveProvider:
        def __getattribute__(self, _name):
            raise AssertionError("provider getter ran before the shared support gate")

    def unsupported() -> None:
        raise RuntimeError("shared publication support is unavailable")

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    monkeypatch.setattr(
        strict_bm25_module,
        "require_owned_workspace_publication_support",
        unsupported,
    )
    try:
        with pytest.raises(RuntimeError, match="support is unavailable"):
            normalize_owned_query_view_strict(
                fixture.destination,
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=ExplosiveProvider(),  # type: ignore[arg-type]
                output_receipt_owner=output_owner,
                view_type="bm25",
                view_config={},
                environ={},
            )
        assert consume_calls == 0
        assert output_owner.state == "empty"
    finally:
        output_owner.close()


def test_strict_bm25_rejects_physical_repository_alias_before_source_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    alias = tmp_path / "view-alias"
    destination, source_owner = _publish_source(alias, repository)
    retained = tmp_path / "retained-view"
    alias.rename(retained)
    alias.symlink_to(repository, target_is_directory=True)
    repository_source = capture_repository_source(repository)
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=source_owner.receipt.ownership,
    )
    retained_destination = retained / "state" / "bm25"
    original_documents = (retained_destination / "documents.json").read_bytes()
    original_metadata = (retained_destination / "bm25_metadata.json").read_bytes()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    try:
        with pytest.raises(ValueError, match="must not overlap"):
            normalize_owned_query_view_strict(
                destination,
                source_generation=source_owner,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_type="bm25",
                view_config={},
                environ={},
            )
        assert consume_calls == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert (
            retained_destination / "documents.json"
        ).read_bytes() == original_documents
        assert (
            retained_destination / "bm25_metadata.json"
        ).read_bytes() == original_metadata
    finally:
        output_owner.close()
        repository_source.close()
        source_owner.close()


def test_strict_bm25_rejects_repository_overlap_before_source_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    destination, source_owner = _publish_source(repository, repository)
    repository_source = capture_repository_source(repository)
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=source_owner.receipt.ownership,
    )
    original_documents = (destination / "documents.json").read_bytes()
    original_metadata = (destination / "bm25_metadata.json").read_bytes()
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    try:
        with pytest.raises(ValueError, match="must not overlap"):
            plan_bm25_view_strict(
                source_owner,
                repository_source=repository_source,
                view_config={},
                environ={},
            )
        with pytest.raises(ValueError, match="must not overlap"):
            normalize_owned_query_view_strict(
                destination,
                source_generation=source_owner,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_type="bm25",
                view_config={},
                environ={},
            )
        assert consume_calls == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert (destination / "documents.json").read_bytes() == original_documents
        assert (destination / "bm25_metadata.json").read_bytes() == original_metadata
        for overlapping in (repository, repository / "nested", repository.parent):
            with pytest.raises(ValueError, match="must not overlap"):
                strict_bm25_module._require_disjoint_view_path(
                    overlapping,
                    repository,
                )
    finally:
        output_owner.close()
        repository_source.close()
        source_owner.close()


def test_strict_bm25_rejects_wrong_destination_before_source_consume(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = strict_generation
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    consume_calls = 0
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def counted_consume(self, callback):
        nonlocal consume_calls
        consume_calls += 1
        return real_consume(self, callback)

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", counted_consume)
    try:
        with pytest.raises(ValueError, match="exact source generation"):
            normalize_owned_query_view_strict(
                fixture.destination.parent / "wrong",
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_type="bm25",
                view_config={},
                environ={},
            )
        assert consume_calls == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
    finally:
        output_owner.close()


def test_strict_bm25_rejects_deep_config_before_source_consume(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = strict_generation
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(140):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child

    def forbidden_consume(*_args, **_kwargs):
        raise AssertionError("source consume ran before config complexity gate")

    monkeypatch.setattr(
        PublishedWorkspaceReceiptOwner,
        "consume",
        forbidden_consume,
    )
    with pytest.raises(ValueError, match="depth"):
        plan_bm25_view_strict(
            fixture.source_owner,
            repository_source=fixture.repository_source,
            view_config=deep,
            environ={},
        )


def test_strict_bm25_metadata_is_bounded_before_dom(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    metadata = b'{"extra":' + (b"[" * 140) + b"0" + (b"]" * 140) + b"}\n"
    destination, source_owner = _publish_source(
        tmp_path,
        repository,
        metadata=metadata,
    )
    repository_source = capture_repository_source(repository)
    try:
        with pytest.raises(ValueError, match="depth"):
            plan_bm25_view_strict(
                source_owner,
                repository_source=repository_source,
                view_config={},
                environ={},
            )
        assert destination.is_dir()
    finally:
        source_owner.close()
        repository_source.close()


def test_strict_bm25_documents_are_bounded_before_element_dom(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    documents = (
        b'[{"page_content":"ok","metadata":{"nested":'
        + (b"[" * 140)
        + b"0"
        + (b"]" * 140)
        + b"}}]\n"
    )
    _destination, source_owner = _publish_source(
        tmp_path,
        repository,
        documents=documents,
    )
    repository_source = capture_repository_source(repository)
    try:
        with pytest.raises(ValueError, match="depth"):
            plan_bm25_view_strict(
                source_owner,
                repository_source=repository_source,
                view_config={},
                environ={},
            )
    finally:
        source_owner.close()
        repository_source.close()


@pytest.mark.parametrize(
    "metadata,match",
    [
        (b'\xef\xbb\xbf{"max_k": 10}\n', "trailing data|UTF-8 JSON"),
        (b'{"max_k": 10, "max_k": 20}\n', "invalid UTF-8 JSON"),
        (b'{"max_k": NaN}\n', "invalid UTF-8 JSON"),
    ],
)
def test_strict_bm25_metadata_rejects_nonportable_json(
    tmp_path: Path,
    metadata: bytes,
    match: str,
) -> None:
    repository = _repository(tmp_path)
    _destination, source_owner = _publish_source(
        tmp_path,
        repository,
        metadata=metadata,
    )
    repository_source = capture_repository_source(repository)
    try:
        with pytest.raises(ValueError, match=match):
            plan_bm25_view_strict(
                source_owner,
                repository_source=repository_source,
                view_config={},
                environ={},
            )
    finally:
        source_owner.close()
        repository_source.close()


def test_strict_bm25_rejects_tampered_plan_before_provider(
    strict_generation,
) -> None:
    fixture = strict_generation
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={},
        environ={},
    )
    tampered = replace(
        planned,
        plan=WorkspacePlan(
            subject_digest=hashlib.sha256(b"wrong-plan").hexdigest(),
            files=planned.plan.files,
        ),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )

    class ForgedPlan(PlannedBm25View):
        pass

    class ForgedWorkspacePlan(WorkspacePlan):
        pass

    class ForgedRecord(TreeFileRecord):
        pass

    with pytest.raises(TypeError, match="exact WorkspacePlan"):
        replace(
            planned,
            plan=ForgedWorkspacePlan(
                planned.plan.subject_digest,
                directories=planned.plan.directories,
                files=planned.plan.files,
                root_mode=planned.plan.root_mode,
                digest=planned.plan.digest,
            ),
        )
    first_record, *remaining_records = planned.output_records
    with pytest.raises(TypeError, match="output records"):
        replace(
            planned,
            output_records=(
                ForgedRecord(
                    first_record.path,
                    first_record.mode,
                    first_record.size,
                    first_record.sha256,
                ),
                *remaining_records,
            ),
        )

    forged = ForgedPlan(
        plan=planned.plan,
        source_ownership=planned.source_ownership,
        source_digest=planned.source_digest,
        source_records=planned.source_records,
        output_records=planned.output_records,
        view_config_digest=planned.view_config_digest,
        repository_fingerprint=planned.repository_fingerprint,
    )
    try:
        with pytest.raises(ValueError, match="not canonical"):
            publish_planned_bm25_view_strict(
                fixture.destination,
                planned=tampered,
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        with pytest.raises(TypeError, match="PlannedBm25View"):
            publish_planned_bm25_view_strict(
                fixture.destination,
                planned=forged,
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert provider.run_count == 0
    finally:
        output_owner.close()


def test_strict_bm25_detects_source_drift_between_plan_and_replay(
    strict_generation,
) -> None:
    fixture = strict_generation
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={},
        environ={},
    )
    (fixture.destination / "documents.json").write_bytes(_canonical_bytes([]))
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    try:
        with pytest.raises(RuntimeError, match="changed|differs"):
            publish_planned_bm25_view_strict(
                fixture.destination,
                planned=planned,
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert output_owner.state == "empty"
    finally:
        output_owner.close()


def test_strict_bm25_rolls_back_when_repository_changes_during_publication(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = strict_generation
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={},
        environ={},
    )
    original_documents = (fixture.destination / "documents.json").read_bytes()
    original_metadata = (fixture.destination / "bm25_metadata.json").read_bytes()
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    candidate_calls = 0
    real_candidate_records = strict_bm25_module._candidate_records

    def mutate_after_validation(*args, **kwargs):
        nonlocal candidate_calls
        records = real_candidate_records(*args, **kwargs)
        candidate_calls += 1
        if candidate_calls == 2:
            (fixture.repository / "sample.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
        return records

    monkeypatch.setattr(
        strict_bm25_module,
        "_candidate_records",
        mutate_after_validation,
    )
    try:
        with pytest.raises(
            (RuntimeError, ValueError),
            match="changed|identity failed validation",
        ):
            publish_planned_bm25_view_strict(
                fixture.destination,
                planned=planned,
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={},
                environ={},
            )
        assert candidate_calls == 2
        assert output_owner.state == "cleanup"
        assert fixture.source_owner.active
        assert (
            fixture.destination / "documents.json"
        ).read_bytes() == original_documents
        assert (
            fixture.destination / "bm25_metadata.json"
        ).read_bytes() == original_metadata
    finally:
        output_owner.close()


def test_strict_bm25_rejects_provider_plan_and_expectation_mismatch(
    strict_generation,
) -> None:
    fixture = strict_generation
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={},
        environ={},
    )
    wrong_plan = WorkspacePlan(
        subject_digest=hashlib.sha256(b"wrong-workspace-plan").hexdigest(),
        files=planned.plan.files,
    )
    providers = (
        _TestWorkspaceProvider(
            expected_destination=fixture.source_owner.receipt.ownership,
            workspace_plan=wrong_plan,
        ),
        _TestWorkspaceProvider(expected_destination=None),
    )
    for provider in providers:
        output_owner = PublishedWorkspaceReceiptOwner()
        try:
            with pytest.raises(
                (RuntimeError, ValueError),
                match="plan|expectation|expected missing",
            ):
                publish_planned_bm25_view_strict(
                    fixture.destination,
                    planned=planned,
                    source_generation=fixture.source_owner,
                    repository_source=fixture.repository_source,
                    workspace_provider=provider,
                    output_receipt_owner=output_owner,
                    view_config={},
                    environ={},
                )
            assert provider.run_count == 1
            assert output_owner.state == "empty"
        finally:
            output_owner.close()


def test_strict_bm25_validates_staged_and_published_generations(
    strict_generation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = strict_generation
    planned = plan_bm25_view_strict(
        fixture.source_owner,
        repository_source=fixture.repository_source,
        view_config={},
        environ={},
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    calls = 0
    real_candidate_records = strict_bm25_module._candidate_records

    def counted_candidate_records(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_candidate_records(*args, **kwargs)

    monkeypatch.setattr(
        strict_bm25_module,
        "_candidate_records",
        counted_candidate_records,
    )
    try:
        publish_planned_bm25_view_strict(
            fixture.destination,
            planned=planned,
            source_generation=fixture.source_owner,
            repository_source=fixture.repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config={},
            environ={},
        )
        assert calls == 2
        assert output_owner.active
    finally:
        output_owner.close()


def test_strict_bm25_rejects_unbound_document_source(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    documents = _canonical_bytes(
        [
            {
                "page_content": "outside",
                "metadata": {"file": "missing.py"},
            }
        ]
    )
    _destination, source_owner = _publish_source(
        tmp_path,
        repository,
        documents=documents,
    )
    repository_source = capture_repository_source(repository)
    try:
        with pytest.raises(ValueError, match="authenticated repository source"):
            plan_bm25_view_strict(
                source_owner,
                repository_source=repository_source,
                view_config={},
                environ={},
            )
    finally:
        source_owner.close()
        repository_source.close()


def test_strict_bm25_never_falls_back_for_vector(
    strict_generation,
) -> None:
    fixture = strict_generation
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestWorkspaceProvider(
        expected_destination=fixture.source_owner.receipt.ownership,
    )
    try:
        with pytest.raises(ValueError, match="only bm25"):
            normalize_owned_query_view_strict(
                fixture.destination,
                source_generation=fixture.source_owner,
                repository_source=fixture.repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_type="vector",
                view_config={},
            )
        assert provider.support_count == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
    finally:
        output_owner.close()


class _FaultyChunks:
    def __init__(
        self,
        *,
        work_error: BaseException | None,
        close_error: BaseException,
    ) -> None:
        self.work_error = work_error
        self.close_error = close_error
        self.step = 0

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self.step == 0 and self.work_error is not None:
            self.step += 1
            return b"first"
        if self.work_error is not None:
            raise self.work_error
        raise StopIteration

    def close(self) -> None:
        raise self.close_error


def test_strict_bm25_recording_keeps_work_failure_primary() -> None:
    primary = KeyboardInterrupt("generation interrupted")
    secondary = SystemExit("iterator cleanup failed")
    with pytest.raises(KeyboardInterrupt) as caught:
        _record_chunks(
            "documents.json",
            _FaultyChunks(work_error=primary, close_error=secondary),
            max_bytes=128,
        )
    assert caught.value is primary
    notes = (
        *getattr(primary, "__notes__", ()),
        *getattr(primary, "_codenib_cleanup_notes", ()),
    )
    assert any("iterator close" in note for note in notes)


def test_strict_bm25_recording_propagates_lone_close_failure() -> None:
    primary = SystemExit("iterator cleanup failed")
    with pytest.raises(SystemExit) as caught:
        _record_chunks(
            "documents.json",
            _FaultyChunks(work_error=None, close_error=primary),
            max_bytes=128,
        )
    assert caught.value is primary
