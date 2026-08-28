# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare one FULL BM25 job directly from retained repository source.

The adapter deliberately composes the existing compiler-cache preparation
boundary.  It builds one caller-owned private cache generation, writes the
exact single-view manifest expected by that boundary, and delegates strict
recapture, context publication, and CAS ingestion.  It receives no catalog or
ref mutation authority; the durable worker remains the only publisher.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from .._atomic_directory import (
    PublicationDirectoryReader,
    TreeFileRecord,
    _open_publication_authority,
    _PublicationAuthority,
    _PublicationAuthorityOwner,
    directory_ownership_file_records,
    directory_ownership_inventory,
    lexical_directory_path,
    publication_parent_identity,
)
from .._captured_directory import (
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)
from .._local_workspace_provider import LocalWorkspaceProvider
from .._workspace_provider import (
    StrictWorkspaceProvider,
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_strict_workspace,
)
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
)
from ..storage.job_worker import (
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobStopReason,
    IndexJobStopToken,
    IndexJobViewExecutionResult,
)
from ..storage.models import (
    DEFAULT_NAMESPACE_NAME,
    IndexJobEffectiveMode,
    IndexJobViewOutcome,
    StorageIntegrityError,
    StorageValidationError,
    ViewProfile,
    canonical_json,
)
from ..storage.protocols import RetainedImportObjectStore
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
)
from .cache_import import (
    _compiler_cache_job_stop_check,
    _path_relation,
    _preflight_cache_job_preparation_operation,
    _preflight_workspace_provider,
    _require_missing,
    _resolved_path,
    prepare_compiler_cache_job_view_from_generation,
)
from .cache_lock import COMPILER_CACHE_LOCK_FILENAME
from .index_builders import BM25IndexBuilder, PreparedBm25Build
from .manifest import MANIFEST_FILENAME, MANIFEST_VERSION, IndexEntry, RepoManifest
from .manifest_import import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    _snapshot_environment,
    _snapshot_forbidden_paths,
)
from .manifest_storage import (
    DEFAULT_MAX_MANIFEST_BYTES,
    REPO_MANIFEST_PROFILE_NAME,
    _profile_config,
)
from .resources import IndexState, IndexStatus

_DISPLAY_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_BM25_VIEW = "bm25"
_BM25_SCOPE = "current_repo"
_CACHE_JSON_CHARS = 1024 * 1024
_PRIVATE_CACHE_PLAN_DOMAIN = b"codenib-private-bm25-cache-generation-v1"


def _json_byte_chunks(
    value: object,
    check_cancelled: Callable[[], None] | None,
) -> Iterator[bytes]:
    """Encode replayable indented JSON with bounded cancellation intervals."""

    encoder = json.JSONEncoder(indent=2, allow_nan=False)
    if check_cancelled is not None:
        check_cancelled()
    for fragment in encoder.iterencode(value):
        for offset in range(0, len(fragment), _CACHE_JSON_CHARS):
            if check_cancelled is not None:
                check_cancelled()
            yield fragment[offset : offset + _CACHE_JSON_CHARS].encode("utf-8")
        if check_cancelled is not None:
            check_cancelled()


def _payload_record(
    relative: str,
    chunks: Iterable[bytes],
    check_cancelled: Callable[[], None] | None,
) -> TreeFileRecord:
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        if check_cancelled is not None:
            check_cancelled()
        if type(chunk) is not bytes:
            raise TypeError("private BM25 cache payload chunks must be exact bytes")
        digest.update(chunk)
        size += len(chunk)
    if check_cancelled is not None:
        check_cancelled()
    return TreeFileRecord(
        path=relative,
        mode=0o600,
        size=size,
        sha256=digest.hexdigest(),
    )


def _private_cache_subject(records: tuple[TreeFileRecord, ...]) -> str:
    digest = hashlib.sha256(_PRIVATE_CACHE_PLAN_DOMAIN)
    for record in records:
        encoded = record.path.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(record.mode.to_bytes(4, "big"))
        digest.update(record.size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(record.sha256))
    return digest.hexdigest()


def _validate_private_cache_generation(
    publication: PublicationDirectoryReader,
    *,
    expected_records: tuple[TreeFileRecord, ...],
    expected_manifest_bytes: bytes,
    check_cancelled: Callable[[], None] | None,
) -> None:
    ownership = publication.capture_ownership(check_cancelled=check_cancelled)
    expected_inventory = {
        (COMPILER_CACHE_LOCK_FILENAME, "file"),
        (_BM25_VIEW, "directory"),
        (f"{_BM25_VIEW}/bm25_metadata.json", "file"),
        (f"{_BM25_VIEW}/documents.json", "file"),
        (MANIFEST_FILENAME, "file"),
    }
    if (
        set(directory_ownership_inventory(ownership)) != expected_inventory
        or tuple(directory_ownership_file_records(ownership)) != expected_records
    ):
        raise StorageIntegrityError(
            "private BM25 cache generation differs from its exact plan"
        )
    observed_manifest = publication.read_bytes(
        MANIFEST_FILENAME,
        max_bytes=len(expected_manifest_bytes),
    )
    if observed_manifest != expected_manifest_bytes:
        raise StorageIntegrityError(
            "private BM25 cache manifest differs from its exact plan"
        )
    if publication.capture_ownership(check_cancelled=check_cancelled) != ownership:
        raise StorageIntegrityError(
            "private BM25 cache generation changed during validation"
        )


@dataclass(frozen=True, slots=True)
class _RetainedCacheWorkspaceProvider:
    """Bind one provider call to the exact parent opened before cache checks."""

    delegate: LocalWorkspaceProvider
    parent_authority: _PublicationAuthority = field(repr=False, compare=False)
    parent_identity: tuple[int, ...]
    topology_verifier: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def _verify_parent(self) -> None:
        if self.topology_verifier is not None:
            self.topology_verifier()
        self.parent_authority.verify_path_binding()
        observed = publication_parent_identity(self.parent_authority.resource)
        if observed != self.parent_identity:
            raise RuntimeError("BM25 source cache parent authority changed")
        if self.topology_verifier is not None:
            self.topology_verifier()

    def require_support(self) -> None:
        self._verify_parent()
        self.delegate.require_support()
        self._verify_parent()

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], object],
        check_cancelled: Callable[[], None] | None = None,
    ) -> object:
        self._verify_parent()
        arguments = {
            "receipt_owner": receipt_owner,
            "operation": operation,
            "_expected_parent_identity": self.parent_identity,
        }
        if check_cancelled is None:
            result = self.delegate.run_workspace(  # type: ignore[call-arg]
                request,
                **arguments,
            )
        else:
            result = self.delegate.run_workspace(  # type: ignore[call-arg]
                request,
                **arguments,
                check_cancelled=check_cancelled,
            )
        self._verify_parent()
        return result


@dataclass(frozen=True, slots=True)
class _BoundSourceJobStopToken:
    """Carry one exact source-job stop exception into cache preparation."""

    token: IndexJobStopToken
    check_cancelled: Callable[[], None]

    @property
    def reason(self) -> IndexJobStopReason | None:
        return self.token.reason

    def is_set(self) -> bool:
        self.check_cancelled()
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return self.token.wait(timeout)


@dataclass(frozen=True, slots=True)
class _BM25BuilderConfiguration:
    languages: tuple[str, ...]
    max_k: int
    max_lines_per_chunk: int
    additional_ignore_dirs: tuple[str, ...]
    source_selection: RepositorySourceSelection

    def builder(self) -> BM25IndexBuilder:
        return BM25IndexBuilder(
            languages=list(self.languages),
            max_k=self.max_k,
            max_lines_per_chunk=self.max_lines_per_chunk,
            additional_ignore_dirs=list(self.additional_ignore_dirs),
            source_selection=RepositorySourceSelection(
                self.source_selection.exclude_subtrees
            ),
        )

    def profile(self) -> ViewProfile:
        identity = self.builder().artifact_identity()
        # Reuse the manifest planner's profile classifier so this source
        # adapter cannot drift from imported compiler-cache identities.
        entry = IndexEntry(
            index_type=_BM25_VIEW,
            path=f"views/{_BM25_VIEW}",
            built_at="",
            built_at_epoch=0.0,
            status="fresh",
            config=copy.deepcopy(identity),
            metadata=copy.deepcopy(identity),
            source_selection_digest=self.source_selection.digest,
        )
        config = _profile_config(
            entry,
            view_type=_BM25_VIEW,
            manifest_version=MANIFEST_VERSION,
        )
        return ViewProfile.create(
            _BM25_VIEW,
            config,
            name=REPO_MANIFEST_PROFILE_NAME,
        )


def _snapshot_builder(builder: BM25IndexBuilder) -> _BM25BuilderConfiguration:
    if type(builder) is not BM25IndexBuilder:
        raise TypeError("BM25 source job builder must use the exact builder type")
    if type(builder.languages) is not list or any(
        type(language) is not str for language in builder.languages
    ):
        raise TypeError("BM25 source job languages must be an exact text list")
    if type(builder.additional_ignore_dirs) is not list or any(
        type(path) is not str for path in builder.additional_ignore_dirs
    ):
        raise TypeError("BM25 source job ignore directories must be an exact text list")
    if type(builder.max_k) is not int or type(builder.max_lines_per_chunk) is not int:
        raise TypeError("BM25 source job numeric options must be exact integers")
    if type(builder.source_selection) is not RepositorySourceSelection:
        raise TypeError("BM25 source job selection must use the exact selection type")
    snapshot = _BM25BuilderConfiguration(
        languages=tuple(builder.languages),
        max_k=builder.max_k,
        max_lines_per_chunk=builder.max_lines_per_chunk,
        additional_ignore_dirs=tuple(builder.additional_ignore_dirs),
        source_selection=RepositorySourceSelection(
            builder.source_selection.exclude_subtrees
        ),
    )
    # Fail closed on incomplete or non-canonical compatibility axes now, not
    # after an attempt generation has been created.
    snapshot.profile()
    return snapshot


def bm25_source_job_profile(builder: BM25IndexBuilder) -> ViewProfile:
    """Return the exact portable profile requested by this source adapter."""

    return _snapshot_builder(builder).profile()


def _exact_display_commit(value: object) -> str:
    if type(value) is not str or _DISPLAY_COMMIT_RE.fullmatch(value) is None:
        raise StorageValidationError(
            "BM25 source job display commit must be a full lowercase Git SHA"
        )
    return value


def _exact_attempt_generation(value: object) -> Path:
    if type(value) is not type(Path()):
        raise TypeError("BM25 source job attempt generation must be an exact Path")
    generation = lexical_directory_path(value)
    if generation == generation.parent:
        raise ValueError(
            "BM25 source job attempt generation cannot be a filesystem root"
        )
    return generation


def _preflight_source_job_topology(
    generation: Path,
    source: RepositorySourceIdentitySnapshot,
    *,
    view_destination: Path,
    context_destination: Path,
    forbidden_paths: tuple[Path, ...],
) -> None:
    repository = lexical_directory_path(source.root)
    source_view = lexical_directory_path(generation / _BM25_VIEW)
    outputs = (
        lexical_directory_path(view_destination),
        lexical_directory_path(context_destination),
    )
    forbidden = tuple(lexical_directory_path(boundary) for boundary in forbidden_paths)
    physical_repository = _resolved_path(
        repository,
        strict=True,
        label="BM25 source job repository",
    )
    physical_generation = _resolved_path(
        generation,
        strict=False,
        label="BM25 source job attempt generation",
    )
    physical_source_view = _resolved_path(
        source_view,
        strict=False,
        label="BM25 source job cache view",
    )
    physical_outputs = tuple(
        _resolved_path(
            output,
            strict=False,
            label="BM25 source job destination",
        )
        for output in outputs
    )
    physical_forbidden = tuple(
        _resolved_path(
            boundary,
            strict=False,
            label="BM25 source job forbidden boundary",
        )
        for boundary in forbidden
    )
    if (
        _path_relation(generation, repository) != "disjoint"
        or _path_relation(physical_generation, physical_repository) != "disjoint"
    ):
        raise ValueError("BM25 source job attempt generation overlaps repository")
    for lexical, physical in zip(
        (*outputs, *forbidden),
        (*physical_outputs, *physical_forbidden),
        strict=True,
    ):
        if (
            _path_relation(generation, lexical) != "disjoint"
            or _path_relation(physical_generation, physical) != "disjoint"
        ):
            raise ValueError(
                "BM25 source job attempt generation overlaps output or "
                "forbidden boundary"
            )

    lexical_inputs = (repository, generation, source_view, *forbidden)
    physical_inputs = (
        physical_repository,
        physical_generation,
        physical_source_view,
        *physical_forbidden,
    )
    for output, physical_output in zip(outputs, physical_outputs, strict=True):
        if any(
            _path_relation(output, boundary) != "disjoint"
            for boundary in lexical_inputs
        ) or any(
            _path_relation(physical_output, boundary) != "disjoint"
            for boundary in physical_inputs
        ):
            raise ValueError("BM25 source job destination overlaps an input authority")
        _require_missing(output, label="BM25 source job destination")
    if (
        _path_relation(outputs[0], outputs[1]) != "disjoint"
        or _path_relation(physical_outputs[0], physical_outputs[1]) != "disjoint"
    ):
        raise ValueError("BM25 source job output destinations overlap")


def _require_attempt_provider_destination(
    generation: Path,
    provider: LocalWorkspaceProvider,
) -> None:
    root = lexical_directory_path(provider.allowed_root)
    physical_root = _resolved_path(
        root,
        strict=True,
        label="BM25 source job attempt provider root",
    )
    physical_generation = _resolved_path(
        generation,
        strict=False,
        label="BM25 source job attempt generation",
    )
    if (
        root not in generation.parents
        or physical_root not in physical_generation.parents
    ):
        raise ValueError(
            "BM25 source job attempt must be strictly below its local provider root"
        )


def _built_at(status: IndexStatus) -> tuple[str, float]:
    value = status.last_built
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise StorageIntegrityError("BM25 source builder returned an invalid timestamp")
    epoch = float(value)
    try:
        timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise StorageIntegrityError(
            "BM25 source builder returned an invalid timestamp"
        ) from exc
    return timestamp, epoch


def _source_manifest(
    status: IndexStatus,
    *,
    output: Path,
    display_commit: str,
    repository_source: RepositorySourceBinding,
    expected_source: RepositorySourceIdentitySnapshot,
    builder: _BM25BuilderConfiguration,
    check_cancelled: Callable[[], None],
) -> RepoManifest:
    if type(status) is not IndexStatus:
        raise StorageIntegrityError("BM25 source builder returned an invalid status")
    if (
        status.index_type != _BM25_VIEW
        or status.state is not IndexState.FRESH
        or status.scope != _BM25_SCOPE
        or status.path != str(output)
        or type(status.metadata) is not dict
    ):
        raise StorageIntegrityError("BM25 source builder returned inconsistent status")
    expected_identity = builder.builder().artifact_identity()
    if any(
        status.metadata.get(key) != value for key, value in expected_identity.items()
    ):
        raise StorageIntegrityError(
            "BM25 source builder changed its compatibility identity"
        )
    try:
        # Detach through the canonical JSON boundary before it becomes a
        # persisted manifest input.
        metadata = json.loads(canonical_json(status.metadata))
    except (RecursionError, TypeError, ValueError) as exc:
        raise StorageIntegrityError(
            "BM25 source builder returned non-canonical metadata"
        ) from exc
    built_at, built_at_epoch = _built_at(status)
    source = repository_source.authenticated_identity_snapshot(
        check_cancelled=check_cancelled,
    )
    if source != expected_source:
        raise StorageIntegrityError("BM25 retained source changed after job preflight")
    if source.source_selection != builder.source_selection:
        raise StorageIntegrityError(
            "BM25 source builder selection changed after job preflight"
        )
    selection_digest = builder.source_selection.digest
    entry = IndexEntry(
        index_type=_BM25_VIEW,
        path=_BM25_VIEW,
        built_at=built_at,
        built_at_epoch=built_at_epoch,
        status="fresh",
        config=copy.deepcopy(metadata),
        metadata=copy.deepcopy(metadata),
        commit=display_commit,
        source_fingerprint=source.fingerprint,
        source_selection_digest=selection_digest,
    )
    manifest = RepoManifest(
        repo_path=str(source.root),
        commit=display_commit,
        last_indexed_commit=display_commit,
        source_fingerprint=source.fingerprint,
        last_indexed_source_fingerprint=source.fingerprint,
        source_selection=RepositorySourceSelection(
            builder.source_selection.exclude_subtrees
        ),
        last_indexed_source_selection_digest=selection_digest,
        languages=list(builder.languages),
        file_count=source.file_count,
        indexes={_BM25_VIEW: entry},
        compiled_at=built_at,
        compiled_at_epoch=built_at_epoch,
    )
    manifest.derive_capabilities()
    # Exercise the exact persisted validation path before writing bytes.
    return RepoManifest.from_dict(manifest.to_dict())


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _publish_private_bm25_cache_generation(
    generation: Path,
    *,
    display_commit: str,
    builder: _BM25BuilderConfiguration,
    repository_source: RepositorySourceBinding,
    expected_source: RepositorySourceIdentitySnapshot,
    output_owner: PublishedWorkspaceReceiptOwner,
    workspace_provider: _RetainedCacheWorkspaceProvider,
    parent_authority: _PublicationAuthority,
    check_cancelled: Callable[[], None],
) -> RepoManifest:
    """Prepare in memory and publish under one retained parent authority."""

    _preflight_workspace_provider(workspace_provider)
    if type(output_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("BM25 source job attempt owner must be exact")
    if output_owner.state != "empty":
        raise RuntimeError("BM25 source job attempt owner must be empty")
    check_cancelled()
    parent_authority.verify_path_binding()
    resolved_generation = generation.resolve(strict=False)
    resolved_repository = expected_source.root.resolve(strict=True)
    if _paths_overlap(generation, expected_source.root) or _paths_overlap(
        resolved_generation,
        resolved_repository,
    ):
        raise ValueError("BM25 source job attempt overlaps the repository")
    if (
        parent_authority.child_metadata(
            generation.name,
            path=generation,
            label="BM25 source job attempt generation",
        )
        is not None
    ):
        raise FileExistsError("BM25 source job attempt generation must be missing")
    parent_authority.verify_path_binding()

    source_builder = builder.builder()
    expected_identity = source_builder.artifact_identity()
    prepared = source_builder.prepare_from_repository_source(
        _BM25_SCOPE,
        repository_source=repository_source,
        source_selection=builder.source_selection,
        check_cancelled=check_cancelled,
    )
    if type(prepared) is not PreparedBm25Build:
        raise StorageIntegrityError("BM25 source builder returned an invalid build")
    if (
        prepared.scope != _BM25_SCOPE
        or prepared.repository_root != str(expected_source.root)
        or prepared.artifact_identity != expected_identity
    ):
        raise StorageIntegrityError(
            "BM25 source builder returned a different generation"
        )

    indexer = prepared.indexer
    documents: list[dict[str, object]] = []
    index_documents = getattr(indexer, "documents", None)
    if type(index_documents) is not list:
        raise StorageIntegrityError("BM25 source builder documents are invalid")
    for document in index_documents:
        check_cancelled()
        page_content = getattr(document, "page_content", None)
        document_metadata = getattr(document, "metadata", None)
        if type(page_content) is not str or type(document_metadata) is not dict:
            raise StorageIntegrityError("BM25 source builder document is invalid")
        try:
            detached_metadata = copy.deepcopy(document_metadata)
        except Exception as exc:
            raise StorageIntegrityError(
                "BM25 source builder document metadata cannot be detached"
            ) from exc
        documents.append(
            {
                "page_content": page_content,
                "metadata": detached_metadata,
            }
        )
    metadata_payload = {
        "project_root": (
            str(indexer.project_root) if indexer.project_root is not None else None
        ),
        "max_k": indexer.max_k,
        "language": indexer.language,
    }
    documents_record = _payload_record(
        f"{_BM25_VIEW}/documents.json",
        _json_byte_chunks(documents, check_cancelled),
        check_cancelled,
    )
    metadata_record = _payload_record(
        f"{_BM25_VIEW}/bm25_metadata.json",
        _json_byte_chunks(metadata_payload, check_cancelled),
        check_cancelled,
    )
    artifact_fingerprints = {
        record.path.removeprefix(f"{_BM25_VIEW}/"): {
            "size": record.size,
            "sha256": record.sha256,
        }
        for record in (documents_record, metadata_record)
    }
    status = IndexStatus(
        index_type=_BM25_VIEW,
        state=IndexState.FRESH,
        last_built=datetime.now(timezone.utc).timestamp(),
        age_seconds=0.0,
        scope=_BM25_SCOPE,
        path=str(generation / _BM25_VIEW),
        metadata={
            **copy.deepcopy(expected_identity),
            "artifact_file_fingerprints": artifact_fingerprints,
            "chunk_count": prepared.chunk_count,
            "source_file_count": prepared.source_file_count,
            "file_count": prepared.chunk_count,
        },
    )
    manifest = _source_manifest(
        status,
        output=generation / _BM25_VIEW,
        display_commit=display_commit,
        repository_source=repository_source,
        expected_source=expected_source,
        builder=builder,
        check_cancelled=check_cancelled,
    )
    if (
        bm25_source_job_profile(source_builder).profile_id
        != builder.profile().profile_id
    ):
        raise StorageIntegrityError(
            "BM25 source builder profile changed during execution"
        )
    manifest_bytes = json.dumps(
        manifest.to_dict(),
        indent=2,
        allow_nan=False,
    ).encode("utf-8")
    manifest_record = _payload_record(
        MANIFEST_FILENAME,
        (manifest_bytes,),
        check_cancelled,
    )
    lock_record = _payload_record(
        COMPILER_CACHE_LOCK_FILENAME,
        (),
        check_cancelled,
    )
    expected_records = tuple(
        sorted(
            (lock_record, documents_record, metadata_record, manifest_record),
            key=lambda record: record.path,
        )
    )
    payloads = {
        COMPILER_CACHE_LOCK_FILENAME: lambda: iter(()),
        f"{_BM25_VIEW}/documents.json": lambda: _json_byte_chunks(
            documents,
            check_cancelled,
        ),
        f"{_BM25_VIEW}/bm25_metadata.json": lambda: _json_byte_chunks(
            metadata_payload,
            check_cancelled,
        ),
        MANIFEST_FILENAME: lambda: iter((manifest_bytes,)),
    }
    plan = WorkspacePlan(
        subject_digest=_private_cache_subject(expected_records),
        directories=(WorkspaceDirectory(PurePosixPath(_BM25_VIEW), mode=0o700),),
        files=tuple(
            WorkspaceFile(
                PurePosixPath(record.path),
                mode=record.mode,
                max_bytes=record.size,
            )
            for record in expected_records
        ),
        root_mode=0o700,
        check_cancelled=check_cancelled,
    )
    request = StrictWorkspaceRequest(
        purpose="private-bm25-compiler-cache",
        destination=generation,
        plan=plan,
        destination_binding=None,
    )

    def operation(session: StrictWorkspaceSession) -> None:
        if session.request != request:
            raise RuntimeError("BM25 cache provider changed its request")
        written = tuple(
            sorted(
                (
                    session.write_file(record.path, payloads[record.path]())
                    for record in expected_records
                ),
                key=lambda record: record.path,
            )
        )
        if written != expected_records:
            raise StorageIntegrityError(
                "private BM25 cache write differs from its exact plan"
            )
        session.publish_validated(
            lambda publication: _validate_private_cache_generation(
                publication,
                expected_records=expected_records,
                expected_manifest_bytes=manifest_bytes,
                check_cancelled=check_cancelled,
            ),
            validate_published_destination=lambda publication: (
                _validate_private_cache_generation(
                    publication,
                    expected_records=expected_records,
                    expected_manifest_bytes=manifest_bytes,
                    check_cancelled=None,
                )
            ),
        )

    run_strict_workspace(
        workspace_provider,
        request,
        receipt_owner=output_owner,
        operation=operation,
        check_cancelled=check_cancelled,
    )
    if (
        not output_owner.active
        or output_owner.receipt.path != generation
        or output_owner.receipt.plan != plan
    ):
        raise StorageIntegrityError(
            "private BM25 cache provider returned a different generation"
        )
    return manifest


@dataclass(frozen=True, slots=True)
class BM25SourceJobExecutor:
    """Prepare exactly one required FULL BM25 job from retained source bytes.

    ``attempt_generation`` is a unique missing cache root whose exact published
    generation remains retained by ``attempt_output_owner``. The executor never
    removes it, including after cancellation or failure; retries must supply
    another missing root and owner. ``display_commit`` is provenance for the
    strict portable context only. It is never derived from a lexical repository
    read and does not alter the fingerprint-bound durable source ID.
    """

    attempt_generation: Path
    display_commit: str
    builder: InitVar[BM25IndexBuilder]
    attempt_output_owner: PublishedWorkspaceReceiptOwner
    attempt_workspace_provider: LocalWorkspaceProvider
    repository_source: RepositorySourceBinding
    view_output_owner: PublishedWorkspaceReceiptOwner
    context_output_owner: PublishedWorkspaceReceiptOwner
    view_destination: Path
    context_destination: Path
    workspace_provider: StrictWorkspaceProvider
    repository_key: str
    object_store: RetainedImportObjectStore
    namespace_name: str = DEFAULT_NAMESPACE_NAME
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES
    forbidden_paths: Iterable[Path] = ()
    environ: Mapping[str, str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    attempt_parent_identity: tuple[int, ...] | None = None
    attempt_topology_verifier: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _builder: _BM25BuilderConfiguration = field(init=False, repr=False)
    _profile: ViewProfile = field(init=False, repr=False)

    def __post_init__(self, builder: BM25IndexBuilder) -> None:
        configuration = _snapshot_builder(builder)
        object.__setattr__(
            self,
            "attempt_generation",
            _exact_attempt_generation(self.attempt_generation),
        )
        object.__setattr__(
            self,
            "display_commit",
            _exact_display_commit(self.display_commit),
        )
        object.__setattr__(
            self,
            "forbidden_paths",
            _snapshot_forbidden_paths(self.forbidden_paths),
        )
        object.__setattr__(self, "environ", _snapshot_environment(self.environ))
        if (
            type(self.attempt_output_owner) is not PublishedWorkspaceReceiptOwner
            or type(self.view_output_owner) is not PublishedWorkspaceReceiptOwner
            or type(self.context_output_owner) is not PublishedWorkspaceReceiptOwner
        ):
            raise TypeError("BM25 source job requires exact receipt owners")
        if (
            len(
                {
                    id(self.attempt_output_owner),
                    id(self.view_output_owner),
                    id(self.context_output_owner),
                }
            )
            != 3
        ):
            raise ValueError("BM25 source job receipt owners must be distinct")
        if self.attempt_output_owner.state != "empty":
            raise RuntimeError("BM25 source job attempt owner must be empty")
        if type(self.attempt_workspace_provider) is not LocalWorkspaceProvider:
            raise TypeError(
                "BM25 source job attempt requires an exact local workspace provider"
            )
        _preflight_workspace_provider(self.attempt_workspace_provider)
        _require_attempt_provider_destination(
            self.attempt_generation,
            self.attempt_workspace_provider,
        )
        parent_identity = self.attempt_parent_identity
        if parent_identity is not None and (
            type(parent_identity) is not tuple
            or len(parent_identity) < 2
            or any(type(value) is not int for value in parent_identity)
        ):
            raise TypeError("BM25 source job attempt parent identity is invalid")
        topology_verifier = self.attempt_topology_verifier
        if topology_verifier is not None and not callable(topology_verifier):
            raise TypeError("BM25 source job topology verifier must be callable")
        object.__setattr__(
            self,
            "attempt_parent_identity",
            None if parent_identity is None else tuple(parent_identity),
        )
        object.__setattr__(self, "_builder", configuration)
        object.__setattr__(self, "_profile", configuration.profile())

    def execute(
        self,
        context: IndexJobExecutionContext,
    ) -> IndexJobExecutionResult:
        """Build and prepare the claimed view without publication authority."""

        if type(context) is not IndexJobExecutionContext:
            raise TypeError("BM25 source executor requires an exact job context")
        check_cancelled = _compiler_cache_job_stop_check(context.control.stop_token)
        if check_cancelled is None:  # pragma: no cover - execution context invariant
            raise AssertionError("BM25 source job context has no stop check")
        check_cancelled()
        if self.attempt_output_owner.state != "empty":
            raise RuntimeError("BM25 source job attempt owner must be empty")
        _require_attempt_provider_destination(
            self.attempt_generation,
            self.attempt_workspace_provider,
        )
        _require_missing(
            self.attempt_generation,
            label="BM25 source job attempt generation",
        )
        operation, binding = _preflight_cache_job_preparation_operation(
            self.attempt_generation,
            view_type=_BM25_VIEW,
            job=context.job,
            views=context.views,
            repository_source=self.repository_source,
            view_output_owner=self.view_output_owner,
            context_output_owner=self.context_output_owner,
            view_destination=self.view_destination,
            context_destination=self.context_destination,
            workspace_provider=self.workspace_provider,
            repository_key=self.repository_key,
            object_store=self.object_store,
            namespace_name=self.namespace_name,
            max_manifest_bytes=self.max_manifest_bytes,
            max_context_files=self.max_context_files,
            max_context_bytes=self.max_context_bytes,
            max_bundle_files=self.max_bundle_files,
            max_bundle_bytes=self.max_bundle_bytes,
            max_bundle_metadata_bytes=self.max_bundle_metadata_bytes,
            forbidden_paths=self.forbidden_paths,
            environ=self.environ,
            check_cancelled=check_cancelled,
        )
        if binding.view.profile_id != self._profile.profile_id:
            raise StorageValidationError(
                "BM25 source job profile does not match the builder"
            )
        if binding.source_snapshot.source_selection != self._builder.source_selection:
            raise StorageValidationError(
                "BM25 source job selection does not match the retained source"
            )
        _preflight_source_job_topology(
            self.attempt_generation,
            binding.source_snapshot,
            view_destination=operation.view_outputs[_BM25_VIEW],
            context_destination=operation.context_output,
            forbidden_paths=operation.inputs.forbidden_paths,
        )
        check_cancelled()
        if self.attempt_topology_verifier is not None:
            self.attempt_topology_verifier()
        with _PublicationAuthorityOwner() as parent_owner:
            parent_authority = _open_publication_authority(
                self.attempt_generation.parent,
                parent_resource=None,
                expected_parent_identity=None,
                authority_owner=parent_owner,
            )
            parent_identity = publication_parent_identity(parent_authority.resource)
            if (
                self.attempt_parent_identity is not None
                and parent_identity != self.attempt_parent_identity
            ):
                raise RuntimeError(
                    "BM25 source attempt parent differs from retained topology"
                )
            retained_provider = _RetainedCacheWorkspaceProvider(
                delegate=self.attempt_workspace_provider,
                parent_authority=parent_authority,
                parent_identity=parent_identity,
                topology_verifier=self.attempt_topology_verifier,
            )
            parent_authority.verify_path_binding()
            _preflight_source_job_topology(
                self.attempt_generation,
                binding.source_snapshot,
                view_destination=operation.view_outputs[_BM25_VIEW],
                context_destination=operation.context_output,
                forbidden_paths=operation.inputs.forbidden_paths,
            )
            parent_authority.verify_path_binding()
            check_cancelled()
            manifest = _publish_private_bm25_cache_generation(
                self.attempt_generation,
                display_commit=self.display_commit,
                builder=self._builder,
                repository_source=self.repository_source,
                expected_source=binding.source_snapshot,
                output_owner=self.attempt_output_owner,
                workspace_provider=retained_provider,
                parent_authority=parent_authority,
                check_cancelled=check_cancelled,
            )
        check_cancelled()

        prepared = prepare_compiler_cache_job_view_from_generation(
            self.attempt_output_owner,
            expected_manifest=manifest,
            job=context.job,
            views=context.views,
            repository_source=self.repository_source,
            view_output_owner=self.view_output_owner,
            context_output_owner=self.context_output_owner,
            view_destination=self.view_destination,
            context_destination=self.context_destination,
            workspace_provider=self.workspace_provider,
            repository_key=self.repository_key,
            object_store=self.object_store,
            namespace_name=self.namespace_name,
            stop_token=_BoundSourceJobStopToken(
                context.control.stop_token,
                check_cancelled,
            ),
            max_manifest_bytes=self.max_manifest_bytes,
            max_context_files=self.max_context_files,
            max_context_bytes=self.max_context_bytes,
            max_bundle_files=self.max_bundle_files,
            max_bundle_bytes=self.max_bundle_bytes,
            max_bundle_metadata_bytes=self.max_bundle_metadata_bytes,
            forbidden_paths=self.forbidden_paths,
            environ=self.environ,
        )
        if (
            prepared.view != binding.view
            or prepared.artifact.profile_id != self._profile.profile_id
        ):
            raise StorageIntegrityError(
                "BM25 source preparation returned a different requested view"
            )
        return IndexJobExecutionResult(
            views=(
                IndexJobViewExecutionResult.create(
                    prepared.view,
                    effective_mode=IndexJobEffectiveMode.FULL,
                    outcome=IndexJobViewOutcome.SUCCEEDED,
                    artifact=prepared.artifact,
                    payload={"adapter": "bm25_source", "prepared": True},
                ),
            ),
            retryable=False,
            supporting_artifacts=prepared.supporting_artifacts,
        )


__all__ = ["BM25SourceJobExecutor", "bm25_source_job_profile"]
