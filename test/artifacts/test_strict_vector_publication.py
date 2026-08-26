# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import numpy as np
import pytest

import codenib.artifacts.portable_views as portable_views_module
import codenib.artifacts.strict_vector as strict_vector_module
import codenib.index.embedding.vector_store as vector_store_module
from codenib._atomic_directory import PublicationDirectoryReader, TreeFileRecord
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
)
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
)
from codenib.artifacts import (
    PlannedVectorView,
    recapture_vector_view_strict,
    validate_content_bound_portable_query_view_reader,
)
from codenib.compiler.index_builders import VectorIndexBuilder
from codenib.index.embedding.artifact_integrity import (
    VECTOR_ROW_MAPPING_CONTRACT,
    capture_authenticated_vector_view,
    vector_config_artifact_record,
)
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import capture_repository_source

_Result = TypeVar("_Result")
_DIMENSION = 4
_MODEL = "test/model"
_MODEL_SUFFIX = "test__model"


class _DeterministicEmbedding:
    def _vector(self, text: str) -> list[float]:
        seed = int.from_bytes(
            hashlib.sha256(text.encode("utf-8")).digest()[:8],
            "little",
        )
        generator = np.random.default_rng(seed)
        vector = generator.standard_normal(_DIMENSION).astype(np.float32)
        vector /= np.linalg.norm(vector) + 1e-12
        return vector.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class _TestWorkspaceProvider:
    """Quiescent unit-test provisioner, not a production trust boundary."""

    def __init__(self) -> None:
        self.support_count = 0
        self.run_count = 0
        self.requests: list[StrictWorkspaceRequest] = []

    def require_support(self) -> None:
        self.support_count += 1

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
        parent = request.destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / f".{request.destination.name}.vector-test-{self.run_count}"
        stage.mkdir(mode=request.plan.root_mode)
        for index, directory in enumerate(request.plan.directories):
            (stage / directory.path.as_posix()).mkdir(mode=directory.mode)
            if check_cancelled is not None and index + 1 < len(
                request.plan.directories
            ):
                check_cancelled()

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        directory_descriptors = {
            directory.path.as_posix(): os.open(
                stage / directory.path.as_posix(),
                flags,
            )
            for directory in request.plan.directories
        }
        workspace = OwnedWorkspaceAuthority()
        try:
            workspace.adopt(
                destination=request.destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors=directory_descriptors,
                plan=request.plan,
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


def _write_current_mutable_state(source: Path) -> None:
    payloads = {
        "chunk_store.json": b"{}\n",
        "chunk_store.pkl": b"opaque legacy chunk state",
        "embeddings_cache.json": b"[]\n",
        "embeddings_cache.npz": b"opaque numeric cache",
        "embeddings_cache.pkl": b"opaque legacy embedding state",
        "incremental_state.json": b"{}\n",
    }
    for name, payload in payloads.items():
        (source / name).write_bytes(payload)


def _generation(tmp_path: Path) -> SimpleNamespace:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    contents = {
        "alpha.py": "def alpha(value):\n    return value + 1\n",
        "beta.py": "def beta(value):\n    return value * 2\n",
        "gamma.py": "def gamma(value):\n    return value - 3\n",
    }
    for relative, content in contents.items():
        (repository / relative).write_text(content, encoding="utf-8")

    identity = VectorIndexBuilder(
        languages=["python"],
        embedding_model=_MODEL,
        embedding_dimension=_DIMENSION,
        build_levels=["l2"],
    ).artifact_identity()
    source = tmp_path / "raw-vector"
    store = CodeVectorStore(
        embedding_model=_MODEL,
        embedding_provider="huggingface",
        dimension=_DIMENSION,
        embedding=_DeterministicEmbedding(),
        artifact_metadata=identity,
    )
    store.add_code_chunks(
        [
            {
                "content": content,
                "chunk_type": "function",
                "name": Path(relative).stem,
                "file": relative,
                "start_line": 0,
                "end_line": 1,
                "node_id": f"{Path(relative).stem}.function",
            }
            for relative, content in contents.items()
        ],
        level="l2",
    )
    store.save(str(source))
    _write_current_mutable_state(source)
    view_config = {
        **identity,
        "persistence_config_fingerprint": vector_config_artifact_record(
            source,
            _MODEL_SUFFIX,
        ),
        "document_count": {"l2": len(contents)},
    }
    return SimpleNamespace(
        repository=repository,
        source=source,
        view_config=view_config,
        query=contents["beta.py"],
    )


def _load_for_native_parity(path: Path, view_config: dict) -> CodeVectorStore:
    store = CodeVectorStore(
        embedding_model=_MODEL,
        embedding_provider="huggingface",
        dimension=_DIMENSION,
        store_path=str(path),
        embedding=_DeterministicEmbedding(),
        artifact_metadata=view_config,
    )
    with capture_authenticated_vector_view(path) as captured:
        authorization = _mint_trusted_local_admin_authorization(
            captured.ownership,
            view_type="vector",
            semantic_contract=store.artifact_metadata,
            evidence=(
                "strict-vector-test-local-admin",
                "captured-vector-tree-subject",
            ),
        )
    store.load(native_index_authorization=authorization)
    return store


def test_schema_8_recapture_is_inert_and_preserves_native_top_k(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _generation(tmp_path)
    destination = tmp_path / "workspace" / "vector"
    repository_source = capture_repository_source(fixture.repository)
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    candidate_capture_callers: list[str] = []
    faiss_stream_opens: list[str] = []
    original_capture = PublicationDirectoryReader.capture_ownership
    original_open = PublicationDirectoryReader.open_authenticated_file

    def track_capture(self, *args, **kwargs):
        caller = sys._getframe(1).f_code.co_name
        if caller in {"_candidate_records", "verify_root"}:
            candidate_capture_callers.append(caller)
        return original_capture(self, *args, **kwargs)

    def track_open(self, relative, *, max_bytes):
        if str(relative).endswith(".faiss"):
            faiss_stream_opens.append(str(relative))
        return original_open(self, relative, max_bytes=max_bytes)

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                PublicationDirectoryReader,
                "capture_ownership",
                track_capture,
            )
            patcher.setattr(
                PublicationDirectoryReader,
                "open_authenticated_file",
                track_open,
            )
            patcher.setattr(
                vector_store_module.faiss,
                "read_index",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("strict recapture parsed FAISS")
                ),
            )
            patcher.setattr(
                vector_store_module.compat_pickle,
                "load",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("strict recapture parsed pickle")
                ),
            )
            adjustments = recapture_vector_view_strict(
                fixture.source,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config=fixture.view_config,
                environ={},
            )

        assert provider.support_count == 1
        assert provider.run_count == 1
        assert provider.requests[0].destination_expectation == "missing"
        assert candidate_capture_callers == []
        assert faiss_stream_opens == [f"l2/index_{_MODEL_SUFFIX}.faiss"]
        assert output_owner.active
        assert output_owner.receipt.path == destination
        assert output_owner.receipt.plan == provider.requests[0].plan
        assert adjustments == {
            "artifact_scope": "query-serving",
            "portable_document_format": "codenib.vector-documents.v1",
            "persistence_config_fingerprint": vector_config_artifact_record(
                destination,
                _MODEL_SUFFIX,
            ),
        }
        assert sorted(
            path.relative_to(destination).as_posix() for path in destination.rglob("*")
        ) == [
            f"config_{_MODEL_SUFFIX}.json",
            "l2",
            f"l2/config_{_MODEL_SUFFIX}.json",
            f"l2/documents_{_MODEL_SUFFIX}.json",
            f"l2/index_{_MODEL_SUFFIX}.faiss",
        ]
        assert not list(destination.rglob("*.pkl"))
        assert (destination / f"l2/index_{_MODEL_SUFFIX}.faiss").read_bytes() == (
            fixture.source / f"l2/index_{_MODEL_SUFFIX}.faiss"
        ).read_bytes()

        raw = _load_for_native_parity(fixture.source, fixture.view_config)
        recaptured = _load_for_native_parity(
            destination,
            {**fixture.view_config, **adjustments},
        )
        raw_results = raw.search(fixture.query, top_k=3, level="l2")
        recaptured_results = recaptured.search(
            fixture.query,
            top_k=3,
            level="l2",
        )
        assert [result.node_id for result in recaptured_results] == [
            result.node_id for result in raw_results
        ]
        assert [result.score for result in recaptured_results] == pytest.approx(
            [result.score for result in raw_results],
            abs=1e-7,
        )
    finally:
        output_owner.close()
        repository_source.close()


def test_real_chunker_builder_generation_recaptures_with_relative_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    source_file = repository / "pkg" / "sample.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        "def sample(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    raw_generation = tmp_path / "raw-vector"
    monkeypatch.setattr(
        CodeVectorStore,
        "_initialize_embedding_model",
        lambda _self, **_kwargs: _DeterministicEmbedding(),
    )
    builder = VectorIndexBuilder(
        languages=["python"],
        embedding_model=_MODEL,
        embedding_dimension=_DIMENSION,
        build_levels=["l2"],
    )

    status = builder.build(
        scope="current_repo",
        repo_path=str(repository),
        output_dir=str(raw_generation),
    )

    raw_documents = json.loads(
        (raw_generation / f"l2/documents_{_MODEL_SUFFIX}.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw_documents
    assert {document["metadata"]["file"] for document in raw_documents} == {
        "pkg/sample.py"
    }
    destination = tmp_path / "workspace" / "vector"
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        with capture_repository_source(repository) as repository_source:
            recapture_vector_view_strict(
                raw_generation,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config=status.metadata,
                environ={},
            )
        portable_documents = json.loads(
            (destination / f"l2/documents_{_MODEL_SUFFIX}.json").read_text(
                encoding="utf-8"
            )
        )
        assert portable_documents == raw_documents
    finally:
        output_owner.close()


def test_schema_7_is_rejected_before_provider_or_destination(
    tmp_path: Path,
) -> None:
    fixture = _generation(tmp_path)
    destination = tmp_path / "workspace" / "vector"
    repository_source = capture_repository_source(fixture.repository)
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        with pytest.raises(ValueError, match="schema 8.*rebuild older"):
            recapture_vector_view_strict(
                fixture.source,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config={**fixture.view_config, "builder_schema": 7},
                environ={},
            )
        assert provider.support_count == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_source_selection_mismatch_is_rejected_before_cache_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _generation(tmp_path)
    destination = tmp_path / "workspace" / "vector"
    repository_source = capture_repository_source(
        fixture.repository,
        selection=RepositorySourceSelection(("generated",)),
    )
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    monkeypatch.setattr(
        strict_vector_module,
        "capture_directory_ownership",
        lambda *_args, **_kwargs: pytest.fail("raw cache must not be captured"),
    )
    try:
        with pytest.raises(ValueError, match="source selection differs"):
            recapture_vector_view_strict(
                fixture.source,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config=fixture.view_config,
                environ={},
            )
        assert provider.support_count == 0
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_content_validator_rejects_oversized_inert_faiss_without_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _generation(tmp_path)
    destination = tmp_path / "workspace" / "vector"
    repository_source = capture_repository_source(fixture.repository)
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    faiss_opens: list[str] = []
    try:
        adjustments = recapture_vector_view_strict(
            fixture.source,
            destination,
            repository_source=repository_source,
            workspace_provider=provider,
            output_receipt_owner=output_owner,
            view_config=fixture.view_config,
            environ={},
        )
        index_path = destination / f"l2/index_{_MODEL_SUFFIX}.faiss"
        original_open = PublicationDirectoryReader.open_authenticated_file

        def track_open(self, relative, *, max_bytes):
            if str(relative).endswith(".faiss"):
                faiss_opens.append(str(relative))
            return original_open(self, relative, max_bytes=max_bytes)

        monkeypatch.setattr(
            portable_views_module,
            "MAX_PORTABLE_FAISS_INDEX_BYTES",
            index_path.stat().st_size - 1,
        )
        monkeypatch.setattr(
            PublicationDirectoryReader,
            "open_authenticated_file",
            track_open,
        )

        def validate(_receipt, publication):
            with pytest.raises(ValueError, match="byte limit"):
                validate_content_bound_portable_query_view_reader(
                    publication,
                    repository_source=repository_source,
                    view_type="vector",
                    view_config={**fixture.view_config, **adjustments},
                    environ={},
                )

        output_owner.consume(validate)
        assert faiss_opens == []
        assert output_owner.active
    finally:
        output_owner.close()
        repository_source.close()


@pytest.mark.parametrize("tamper", ["row-mapping", "documents"])
def test_tampered_schema_8_receipts_fail_before_provider(
    tmp_path: Path,
    tamper: str,
) -> None:
    fixture = _generation(tmp_path)
    if tamper == "row-mapping":
        config_path = fixture.source / f"config_{_MODEL_SUFFIX}.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["row_mapping"] == VECTOR_ROW_MAPPING_CONTRACT
        config["row_mapping"] = "attacker-controlled-row-order"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    else:
        documents = fixture.source / f"l2/documents_{_MODEL_SUFFIX}.json"
        documents.write_bytes(documents.read_bytes() + b" ")

    destination = tmp_path / "workspace" / "vector"
    repository_source = capture_repository_source(fixture.repository)
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        with pytest.raises(ValueError, match="row mapping|fingerprint"):
            recapture_vector_view_strict(
                fixture.source,
                destination,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config=fixture.view_config,
                environ={},
            )
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_source_change_after_plan_is_rejected_before_provider(
    tmp_path: Path,
) -> None:
    fixture = _generation(tmp_path)
    destination = tmp_path / "workspace" / "vector"
    repository_source = capture_repository_source(fixture.repository)
    provider = _TestWorkspaceProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        planned = strict_vector_module._plan_recaptured_vector_view(
            fixture.source,
            destination,
            repository_source=repository_source,
            view_config=fixture.view_config,
            environ={},
        )
        assert isinstance(planned, PlannedVectorView)
        documents = fixture.source / f"l2/documents_{_MODEL_SUFFIX}.json"
        documents.write_bytes(documents.read_bytes() + b" ")

        with pytest.raises(ValueError, match="another source generation"):
            strict_vector_module._publish_recaptured_vector_view(
                fixture.source,
                destination,
                planned=planned,
                repository_source=repository_source,
                workspace_provider=provider,
                output_receipt_owner=output_owner,
                view_config=fixture.view_config,
                environ={},
            )
        assert provider.run_count == 0
        assert output_owner.state == "empty"
        assert not destination.exists()
    finally:
        output_owner.close()
        repository_source.close()


def test_strict_vector_policy_stops_before_repository_record_poison(
    tmp_path: Path,
) -> None:
    fixture = _generation(tmp_path)
    repository_source = capture_repository_source(fixture.repository)
    repository_identity = repository_source.authenticated_identity_snapshot()
    records = repository_identity.file_records
    assert len(records) >= 2
    stop = ValueError("injected strict vector repository policy stop")
    poison_consumed = False

    class PoisonedRecord:
        @property
        def path(self) -> str:
            nonlocal poison_consumed
            poison_consumed = True
            raise AssertionError(
                "strict vector policy consumed its poisoned repository tail"
            )

    object.__setattr__(
        repository_identity,
        "file_records",
        (records[0], PoisonedRecord(), *records[2:]),
    )

    def check_cancelled() -> None:
        raise stop

    try:
        with pytest.raises(ValueError) as caught:
            strict_vector_module._plan_recaptured_vector_view_with_identity(
                fixture.source,
                repository_source=repository_source,
                repository_identity=repository_identity,
                view_config=fixture.view_config,
                forbidden_paths=(),
                environ={},
                check_cancelled=check_cancelled,
            )

        assert caught.value is stop
        assert not poison_consumed
        assert repository_source.usable
    finally:
        repository_source.close()


def test_strict_vector_repository_mismatch_precedes_terminal_policy_stop(
    tmp_path: Path,
) -> None:
    fixture = _generation(tmp_path)
    repository_source = capture_repository_source(fixture.repository)
    repository_identity = repository_source.authenticated_identity_snapshot()
    first, second, *_tail = repository_identity.file_records
    object.__setattr__(
        repository_identity,
        "file_records",
        (first, replace(second, path=first.path)),
    )
    stop = RuntimeError("terminal strict vector repository policy stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls > 1:
            raise stop

    try:
        with pytest.raises(RuntimeError, match="repeats a file") as caught:
            strict_vector_module._repository_files(
                repository_identity,
                check_cancelled=check_cancelled,
            )

        assert caught.value is not stop
        assert polls == 1
        assert repository_source.usable
    finally:
        repository_source.close()


def test_strict_vector_snapshot_stops_before_poisoned_future_item() -> None:
    stop = BaseException("injected strict vector snapshot stop")
    armed = False
    poison_consumed = False

    class PoisonedConfig(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            return 1

        def __iter__(self) -> Iterator[str]:
            nonlocal armed, poison_consumed
            armed = True
            yield "first"
            poison_consumed = True
            raise AssertionError("strict vector config consumed its poisoned tail")

        def __len__(self) -> int:
            return 2

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(BaseException) as caught:
        strict_vector_module._json_object_snapshot(
            PoisonedConfig(),
            label="strict vector test config",
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poison_consumed


def test_strict_vector_snapshot_current_error_precedes_armed_stop() -> None:
    stop = RuntimeError("injected strict vector snapshot stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(TypeError, match="text pairs") as environment_error:
        strict_vector_module._environment_snapshot(
            {"bad": object()},  # type: ignore[dict-item]
            check_cancelled=check_cancelled,
        )
    with pytest.raises(TypeError, match="exact Path") as forbidden_error:
        strict_vector_module._forbidden_paths_snapshot(
            (object(),),  # type: ignore[arg-type]
            check_cancelled=check_cancelled,
        )

    assert environment_error.value is not stop
    assert forbidden_error.value is not stop
    assert polls == 0


def test_strict_vector_snapshot_none_uses_legacy_single_pass_shapes() -> None:
    class SinglePassMapping(Mapping[str, object]):
        def __init__(self, values: dict[str, object]) -> None:
            self.values = values
            self.iterations = 0

        def __getitem__(self, key: str) -> object:
            return self.values[key]

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("strict vector snapshot reread caller mapping")
            return iter(self.values)

        def __len__(self) -> int:
            return len(self.values)

    config = SinglePassMapping({"value": 1})
    environment = SinglePassMapping({"NAME": "value"})

    assert strict_vector_module._json_object_snapshot(
        config,
        label="strict vector test config",
    ) == {"value": 1}
    assert strict_vector_module._environment_snapshot(environment) == {"NAME": "value"}
    assert config.iterations == 1
    assert environment.iterations == 1


@pytest.mark.parametrize("operation", ("plan", "publish"))
def test_strict_vector_identity_error_precedes_initial_stop(
    tmp_path: Path,
    operation: str,
) -> None:
    fixture = _generation(tmp_path)
    repository_source = capture_repository_source(fixture.repository)
    try:
        identity = repository_source.authenticated_identity_snapshot()
        forged = replace(identity, file_count=-1)
        stop = KeyboardInterrupt("injected strict vector initial stop")
        polls = 0

        def check_cancelled() -> None:
            nonlocal polls
            polls += 1
            raise stop

        if operation == "plan":

            def invoke() -> object:
                return strict_vector_module._plan_recaptured_vector_view_with_identity(
                    fixture.source,
                    repository_source=repository_source,
                    repository_identity=forged,
                    view_config=fixture.view_config,
                    forbidden_paths=(),
                    environ={},
                    check_cancelled=check_cancelled,
                )

        else:

            def invoke() -> object:
                return (
                    strict_vector_module._publish_recaptured_vector_view_with_identity(
                        fixture.source,
                        tmp_path / "destination",
                        planned=None,  # type: ignore[arg-type]
                        repository_source=repository_source,
                        repository_identity=forged,
                        workspace_provider=None,  # type: ignore[arg-type]
                        output_receipt_owner=None,  # type: ignore[arg-type]
                        view_config=fixture.view_config,
                        forbidden_paths=(),
                        environ={},
                        check_cancelled=check_cancelled,
                    )
                )

        with pytest.raises(ValueError, match="identity is invalid") as caught:
            invoke()

        assert caught.value is not stop
        assert polls == 0
    finally:
        repository_source.close()


def test_publication_vector_reader_polling_attests_current_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TreeFileRecord(path="a", mode=0o600, size=1, sha256="0" * 64)
    duplicate = replace(first, size=2)
    ownership = object()
    publication = SimpleNamespace(capture_ownership=lambda: ownership)
    stop = RuntimeError("injected strict vector record stop")
    polls = 0

    monkeypatch.setattr(
        strict_vector_module,
        "directory_ownership_file_records",
        lambda _ownership: (first, duplicate),
    )

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls > 1:
            raise stop

    with pytest.raises(RuntimeError, match="repeats a file record") as caught:
        strict_vector_module._PublicationVectorReader(
            publication,  # type: ignore[arg-type]
            ownership,
            check_cancelled=check_cancelled,
        )

    assert caught.value is not stop
    assert polls == 1


def test_publication_vector_reader_stops_before_poisoned_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TreeFileRecord(path="a", mode=0o600, size=1, sha256="0" * 64)
    ownership = object()
    publication = SimpleNamespace(capture_ownership=lambda: ownership)
    stop = ValueError("injected strict vector record stop")
    poison_consumed = False

    class PoisonedRecord:
        @property
        def path(self) -> str:
            nonlocal poison_consumed
            poison_consumed = True
            raise AssertionError("strict vector reader consumed its poisoned record")

    monkeypatch.setattr(
        strict_vector_module,
        "directory_ownership_file_records",
        lambda _ownership: (first, PoisonedRecord()),
    )

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(ValueError) as caught:
        strict_vector_module._PublicationVectorReader(
            publication,  # type: ignore[arg-type]
            ownership,
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poison_consumed


def test_publication_vector_reader_none_keeps_legacy_overwrite_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TreeFileRecord(path="a", mode=0o600, size=1, sha256="0" * 64)
    replacement = replace(first, size=2)
    monkeypatch.setattr(
        strict_vector_module,
        "directory_ownership_file_records",
        lambda _ownership: (first, replacement),
    )

    reader = strict_vector_module._PublicationVectorReader(
        SimpleNamespace(),  # type: ignore[arg-type]
        object(),
    )

    assert reader.record("a") is replacement


def test_publication_vector_reader_mutation_precedes_exact_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TreeFileRecord(path="a", mode=0o600, size=1, sha256="0" * 64)
    ownership = object()
    stop = BaseException("injected strict vector record stop")
    monkeypatch.setattr(
        strict_vector_module,
        "directory_ownership_file_records",
        lambda _ownership: (first, replace(first, path="b")),
    )

    class MutatedPublication:
        def capture_ownership(self) -> object:
            return object()

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(RuntimeError, match="changed during record") as caught:
        strict_vector_module._PublicationVectorReader(
            MutatedPublication(),  # type: ignore[arg-type]
            ownership,
            check_cancelled=check_cancelled,
        )

    assert caught.value is not stop
