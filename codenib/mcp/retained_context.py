# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cold-start one MCP context from an exact retained catalog generation.

The retained materializer deliberately returns data while leaving the published
workspace authority in a caller-created :class:`PublishedWorkspaceReceiptOwner`.
This module joins those two outputs without reopening the published path.  The
receipt callback verifies the context artifact, loads its query-only or
explicitly source-bound runtime, and installs both the live context and a
detached result in a PID-bound owner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TypeVar

from .._atomic_directory import (
    PublicationDirectoryReader,
    _annotate_secondary_error,
    _attach_publication_cleanup_owner,
    _OrderedAction,
    _run_context_with_cleanup_actions,
    directory_ownership_file_records,
    lexical_directory_path,
)
from .._captured_directory import (
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
)
from .._owned_file_publication import _CancellationSafeRLock
from .._workspace_provider import StrictWorkspaceProvider
from ..artifacts.context import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    ContextArtifactResult,
)
from ..artifacts.runtime import (
    ContextArtifactBinding,
    SourceBindingCleanupOwner,
    bind_context_artifact_reader,
    query_context_artifact_reader,
)
from ..compiler.manifest import MANIFEST_FILENAME
from ..compiler.manifest_import import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    DEFAULT_MAX_PROJECTION_BYTES,
    DEFAULT_NAMESPACE_NAME,
    DEFAULT_REF_NAME,
)
from ..compiler.manifest_materialization import (
    RepoManifestMaterializationResult,
    materialize_retained_repo_manifest_ref,
    materialize_retained_repo_manifest_snapshot,
)
from ..compiler.manifest_storage import DEFAULT_MAX_MANIFEST_BYTES
from ..source_fingerprint import lexical_repository_path
from ..storage.models import NamespaceIdentity
from ..storage.protocols import ReceiptRetainingObjectStore, RetainedSnapshotCatalog
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
)
from .context import ServerContext

_Result = TypeVar("_Result")

_OWNER_EMPTY = object()
_OWNER_CLOSED = object()


def _retain_ordered_cleanup_error(
    current: BaseException | None,
    later: BaseException,
    *,
    earlier_label: str,
    later_label: str,
) -> BaseException:
    """Retain cleanup order while never demoting cancellation-class failures."""

    if current is None:
        return later
    if isinstance(current, Exception) and not isinstance(later, Exception):
        _annotate_secondary_error(later, earlier_label, current)
        return later
    _annotate_secondary_error(current, later_label, later)
    return current


@dataclass(frozen=True, slots=True)
class RetainedServerContextResult:
    """Detached result for one activated retained query context."""

    materialization: RepoManifestMaterializationResult
    loaded_views: tuple[str, ...]
    view_error_items: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self) is not RetainedServerContextResult:
            raise TypeError("retained server context result must use the exact type")
        if type(self.materialization) is not RepoManifestMaterializationResult:
            raise TypeError("retained server context materialization is invalid")
        if type(self.loaded_views) is not tuple or any(
            type(view) is not str for view in self.loaded_views
        ):
            raise TypeError("retained server loaded views must be an exact text tuple")
        if type(self.view_error_items) is not tuple:
            raise TypeError("retained server view errors must be an exact tuple")

        selected = self.materialization.artifact.views
        selected_set = set(selected)
        loaded_set = set(self.loaded_views)
        if (
            len(loaded_set) != len(self.loaded_views)
            or not loaded_set <= selected_set
            or self.loaded_views
            != tuple(view for view in selected if view in loaded_set)
        ):
            raise ValueError(
                "retained server loaded views differ from materialization order"
            )

        error_views: list[str] = []
        previous = ""
        for item in self.view_error_items:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                or not item[1]
            ):
                raise TypeError("retained server view error entries are invalid")
            view = item[0]
            if view <= previous or view not in selected_set or view in loaded_set:
                raise ValueError("retained server view errors are not canonical")
            previous = view
            error_views.append(view)
        if loaded_set | set(error_views) != selected_set:
            raise ValueError("retained server result does not account for every view")


class RetainedServerContextOwner:
    """PID-bound one-shot owner for a retained context and its publication."""

    __slots__ = (
        "_slot",
        "_reservation",
        "_result",
        "_receipt_owner",
        "_source_owner",
        "_lock",
        "_owner_pid",
        "_process_locks",
        "_context_close_complete",
        "_source_close_complete",
        "_close_failed",
    )

    def __init__(self) -> None:
        self._slot: object = _OWNER_EMPTY
        self._reservation: object | None = None
        self._result: RetainedServerContextResult | None = None
        # The publication destination is pre-created with this aggregate.  No
        # materializer return/store seam ever becomes the sole receipt owner.
        self._receipt_owner = PublishedWorkspaceReceiptOwner()
        # Source capture can acquire native authority before returning a
        # binding.  Keep its cleanup owner reachable for the entire one-shot
        # lifecycle, including cancellation between binding and context
        # installation.
        self._source_owner = SourceBindingCleanupOwner()
        self._lock = _CancellationSafeRLock()
        self._owner_pid = os.getpid()
        self._process_locks = {self._owner_pid: self._lock}
        self._context_close_complete = False
        self._source_close_complete = False
        self._close_failed = False

    @property
    def state(self) -> str:
        self._require_owner_pid()
        return self._lock.run(self._state_locked)

    @property
    def context(self) -> ServerContext:
        self._require_owner_pid()

        def borrow() -> ServerContext:
            if (
                self._state_locked() != "active"
                or type(self._slot) is not ServerContext
            ):
                raise RuntimeError(
                    "retained server context owner is "
                    f"{self._state_locked()}, expected active"
                )
            return self._slot

        return self._lock.run(borrow)

    @property
    def result(self) -> RetainedServerContextResult:
        self._require_owner_pid()

        def borrow() -> RetainedServerContextResult:
            if self._state_locked() != "active" or self._result is None:
                raise RuntimeError(
                    "retained server context owner is "
                    f"{self._state_locked()}, expected active"
                )
            return self._result

        return self._lock.run(borrow)

    @property
    def closed(self) -> bool:
        return self.state == "closed"

    def _begin_loading(self, reservation: object) -> None:
        """Reserve this one-shot owner before any retained-storage operation."""

        self._require_owner_pid()

        def reserve() -> None:
            if self._slot is not _OWNER_EMPTY:
                raise RuntimeError(
                    "retained server context owner is "
                    f"{self._state_locked()}, expected empty"
                )
            if self._receipt_owner.state != "empty":
                raise RuntimeError(
                    "retained server context publication owner is not empty"
                )
            if not self._source_owner.closed:
                raise RuntimeError("retained server context source owner is not empty")
            # Retain the caller-created identity first.  Cleanup can therefore
            # recognize this invocation even if cancellation lands before the
            # following slot store or before this method returns.
            self._reservation = reservation
            self._slot = reservation

        self._lock.run(reserve)

    def _owns_reservation(self, reservation: object) -> bool:
        """Return whether this invocation acquired the one-shot owner."""

        self._require_owner_pid()
        return self._lock.run(lambda: self._reservation is reservation)

    def _close_reservation(self, reservation: object) -> None:
        """Close only resources acquired by one exact load invocation."""

        if self._owns_reservation(reservation):
            self.close()

    def _run_loading(
        self,
        reservation: object,
        operation: Callable[[PublishedWorkspaceReceiptOwner], _Result],
    ) -> _Result:
        """Serialize materialization, receipt consumption, and activation."""

        if not callable(operation):
            raise TypeError("retained server context loader must be callable")
        self._require_owner_pid()
        if self._lock.held_by_current_thread():
            raise RuntimeError("retained server context loading is reentrant")

        def run() -> _Result:
            if (
                self._reservation is not reservation
                or self._slot is not reservation
                or self._result is not None
            ):
                raise RuntimeError(
                    "retained server context loading reservation changed"
                )
            return operation(self._receipt_owner)

        return self._lock.run(run)

    def _install_context(self, context: ServerContext) -> None:
        """Install the live context before its first runtime resource load."""

        self._require_owner_pid()

        def install() -> None:
            if type(context) is not ServerContext:
                raise TypeError("retained server context must use the exact type")
            if (
                self._reservation is None
                or self._state_locked() != "loading"
                or type(self._slot) is ServerContext
            ):
                raise RuntimeError("retained server context installation changed")
            self._slot = context
            self._context_close_complete = False

        self._lock.run(install)

    def _install_result(self, result: RetainedServerContextResult) -> None:
        """Make the pure result visible only after every runtime gate passes."""

        self._require_owner_pid()

        def install() -> None:
            if type(result) is not RetainedServerContextResult:
                raise TypeError("retained server result must use the exact type")
            if type(self._slot) is not ServerContext or self._result is not None:
                raise RuntimeError("retained server result installation changed")
            self._result = result

        self._lock.run(install)

    def close(self) -> None:
        """In the owning process, close context and source before the receipt."""

        current_pid = os.getpid()
        owner_changed = current_pid != self._owner_pid
        if owner_changed:
            lifecycle_lock = self._process_locks.setdefault(
                current_pid,
                _CancellationSafeRLock(),
            )
        else:
            if self._lock.held_by_current_thread():
                raise RuntimeError("retained server context close is reentrant")
            lifecycle_lock = self._lock

        close_error: BaseException | None = None
        try:
            lifecycle_lock.run(
                self._close_inherited_locked if owner_changed else self._close_locked
            )
        except BaseException as exc:  # noqa: B036 - reconcile before PID report
            close_error = exc

        if owner_changed:
            boundary_error = RuntimeError(
                "retained server context owner cannot cross a PID boundary"
            )
            if close_error is not None:
                if not isinstance(close_error, Exception):
                    _annotate_secondary_error(
                        close_error,
                        "retained server context PID boundary also observed",
                        boundary_error,
                    )
                    raise close_error
                raise boundary_error from close_error
            raise boundary_error
        if close_error is not None:
            owner_closed = False
            try:
                owner_closed = self.closed
            except BaseException as observation_error:  # noqa: B036
                close_error = _retain_ordered_cleanup_error(
                    close_error,
                    observation_error,
                    earlier_label="retained owner cleanup also failed",
                    later_label="retained owner close-state observation also failed",
                )
            if not owner_closed:
                _attach_publication_cleanup_owner(close_error, self)
            raise close_error

    def _close_inherited_locked(self) -> None:
        """Revoke inherited source and publication handles without context locks."""

        # A lock in the copied ServerContext may have been owned by a different
        # thread at fork time.  That thread does not exist in the child, so
        # context.close() could block forever before reaching either authority.
        # Both lower-level owners have their own child-process cleanup paths;
        # visit both even though each reports its PID boundary after cleanup.
        first_error: BaseException | None = None
        try:
            self._source_owner.close()
        except BaseException as exc:  # noqa: B036 - visit receipt after source
            first_error = exc
        try:
            self._receipt_owner.close()
        except BaseException as exc:  # noqa: B036 - preserve first child fault
            first_error = _retain_ordered_cleanup_error(
                first_error,
                exc,
                earlier_label="retained source cleanup also failed",
                later_label="retained publication cleanup also failed",
            )
        self._finish_closed_locked()
        if first_error is not None:
            raise first_error

    def _close_locked(self) -> None:
        if self._slot is _OWNER_CLOSED:
            return
        self._close_failed = False

        primary_error: BaseException | None = None
        context = self._slot if type(self._slot) is ServerContext else None
        if context is None:
            self._context_close_complete = True
        elif not self._context_close_complete:
            try:
                context.close()
            except BaseException as exc:  # noqa: B036 - still visit source owner
                primary_error = exc
            else:
                self._context_close_complete = True

        if not self._source_close_complete:
            source_error: BaseException | None = None
            try:
                self._source_owner.close()
            except BaseException as exc:  # noqa: B036 - observe retry authority
                source_error = exc
            try:
                source_closed = self._source_owner.closed
            except BaseException as observation_error:  # noqa: B036
                source_closed = False
                source_error = _retain_ordered_cleanup_error(
                    source_error,
                    observation_error,
                    earlier_label="retained source cleanup also failed",
                    later_label=("retained source close-state observation also failed"),
                )
            self._source_close_complete = source_closed
            if source_error is not None:
                primary_error = _retain_ordered_cleanup_error(
                    primary_error,
                    source_error,
                    earlier_label="retained context cleanup also failed",
                    later_label="retained source cleanup also failed",
                )
            elif not source_closed:
                incomplete = RuntimeError("retained source cleanup is incomplete")
                primary_error = _retain_ordered_cleanup_error(
                    primary_error,
                    incomplete,
                    earlier_label="retained context cleanup also failed",
                    later_label="retained source cleanup also failed",
                )

        if not self._context_close_complete or not self._source_close_complete:
            self._close_failed = True
            failure = primary_error or RuntimeError(
                "retained context or source cleanup is incomplete"
            )
            _attach_publication_cleanup_owner(failure, self)
            raise failure

        try:
            self._receipt_owner.close()
        except BaseException as exc:  # noqa: B036 - observe retry authority
            primary_error = _retain_ordered_cleanup_error(
                primary_error,
                exc,
                earlier_label="retained context or source cleanup also failed",
                later_label="retained publication cleanup also failed",
            )
        try:
            receipt_closed = self._receipt_owner.closed
        except BaseException as observation_error:  # noqa: B036
            receipt_closed = False
            primary_error = _retain_ordered_cleanup_error(
                primary_error,
                observation_error,
                earlier_label="retained earlier cleanup also failed",
                later_label=(
                    "retained publication close-state observation also failed"
                ),
            )
        if not receipt_closed:
            self._close_failed = True
            failure = primary_error or RuntimeError(
                "retained publication cleanup is incomplete"
            )
            _attach_publication_cleanup_owner(failure, self)
            raise failure

        self._finish_closed_locked()
        if primary_error is not None:
            # A close may report cancellation after fully revoking its owner.
            # Preserve that exact fault without retaining an already-closed
            # aggregate for retry.
            raise primary_error

    def _finish_closed_locked(self) -> None:
        self._slot = _OWNER_CLOSED
        self._reservation = None
        self._result = None
        self._context_close_complete = True
        self._source_close_complete = True
        self._close_failed = False

    def _state_locked(self) -> str:
        if self._slot is _OWNER_EMPTY:
            return "empty"
        if self._slot is _OWNER_CLOSED:
            return "closed"
        if self._close_failed:
            return "close-failed"
        if self._result is not None:
            return "active"
        return "loading"

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "retained server context owner cannot cross a PID boundary"
            )

    def __enter__(self) -> "RetainedServerContextOwner":
        self._require_owner_pid()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def _require_materialization_result(
    value: object,
) -> RepoManifestMaterializationResult:
    if type(value) is not RepoManifestMaterializationResult:
        raise TypeError("retained MCP materializer returned an invalid result")
    return value


def _optional_lexical_repository_path(value: object) -> Path | None:
    """Validate source-binding intent without opening the repository."""

    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise TypeError("repo_path must be text or a Path")
    raw = os.fspath(value)
    if not raw or "\x00" in raw:
        raise ValueError("repo_path must be non-empty path text without NUL")
    return lexical_repository_path(value)


def _require_requested_materialization(
    value: object,
    *,
    repository_key: str,
    namespace_name: str,
    destination: Path,
    ref_name: str | None,
    snapshot_id: str | None,
    expected_generation: int | None,
) -> RepoManifestMaterializationResult:
    """Bind a materializer result back to the exact cold-start selector."""

    result = _require_materialization_result(value)
    receipt = result.export_receipt
    expected_namespace_id = NamespaceIdentity(namespace_name).namespace_id
    expected_destination = lexical_directory_path(destination)
    if (
        receipt.repository_key != repository_key
        or receipt.namespace_id != expected_namespace_id
        or result.artifact.repository != repository_key
    ):
        raise RuntimeError(
            "retained MCP materializer returned a different repository or namespace"
        )
    if result.artifact.output_dir != expected_destination:
        raise RuntimeError("retained MCP materializer returned a different destination")

    if ref_name is not None:
        if (
            snapshot_id is not None
            or receipt.ref_name != ref_name
            or (
                expected_generation is not None
                and receipt.ref_generation != expected_generation
            )
        ):
            raise RuntimeError("retained MCP ref observation differs from the request")
    elif (
        snapshot_id is None
        or receipt.snapshot_id != snapshot_id
        or receipt.ref_name is not None
        or receipt.ref_generation is not None
        or receipt.ref_updated_at is not None
        or expected_generation is not None
    ):
        raise RuntimeError("retained MCP snapshot observation differs from the request")
    return result


def _cross_check_binding(
    materialization: RepoManifestMaterializationResult,
    receipt: PublishedWorkspaceReceipt,
    publication: PublicationDirectoryReader,
    *,
    repo_path: str | Path | None,
    source_cleanup_owner: SourceBindingCleanupOwner,
) -> ContextArtifactBinding:
    """Verify and cross-bind every data and authority projection."""

    artifact_result = materialization.artifact
    export_receipt = materialization.export_receipt
    if type(artifact_result) is not ContextArtifactResult:
        raise TypeError("retained MCP materialization artifact is invalid")
    if (
        not isinstance(artifact_result.output_dir, Path)
        or not isinstance(artifact_result.metadata_path, Path)
        or not isinstance(artifact_result.manifest_path, Path)
        or type(artifact_result.repository) is not str
        or type(artifact_result.commit) is not str
        or type(artifact_result.views) is not tuple
        or any(type(view) is not str for view in artifact_result.views)
        or type(artifact_result.file_count) is not int
        or artifact_result.file_count <= 0
        or type(artifact_result.byte_count) is not int
        or artifact_result.byte_count < 0
    ):
        raise TypeError("retained MCP materialization artifact fields are invalid")
    root = lexical_directory_path(artifact_result.output_dir)
    ownership = receipt.ownership
    if receipt.path != root:
        raise RuntimeError("retained MCP receipt root differs from materialization")
    if artifact_result.output_dir != root:
        raise RuntimeError("retained MCP materialization root is not canonical")
    if artifact_result.metadata_path != root / CONTEXT_ARTIFACT_MANIFEST:
        raise RuntimeError("retained MCP metadata path differs from materialization")
    if artifact_result.manifest_path != root / MANIFEST_FILENAME:
        raise RuntimeError("retained MCP manifest path differs from materialization")
    if publication._require_expected_ownership_token() != ownership:
        raise RuntimeError("retained MCP publication ownership changed")
    records = tuple(directory_ownership_file_records(ownership))  # type: ignore[arg-type]
    manifest_records = tuple(
        record for record in records if record.path == MANIFEST_FILENAME
    )
    if (
        len(manifest_records) != 1
        or manifest_records[0].sha256 != export_receipt.manifest_digest
        or manifest_records[0].size != export_receipt.manifest_byte_size
    ):
        raise RuntimeError(
            "retained MCP export receipt differs from the materialized manifest"
        )

    binding = (
        query_context_artifact_reader(
            receipt,
            publication,
            expected_root=root,
            expected_ownership=ownership,
            expected_repository=artifact_result.repository,
            expected_commit=artifact_result.commit,
        )
        if repo_path is None
        else bind_context_artifact_reader(
            receipt,
            publication,
            repo_path,
            expected_root=root,
            expected_ownership=ownership,
            source_cleanup_owner=source_cleanup_owner,
            expected_repository=artifact_result.repository,
            expected_commit=artifact_result.commit,
        )
    )
    verified = binding.artifact
    if (
        verified.root != root
        or verified.ownership != ownership
        or verified.metadata_path != artifact_result.metadata_path
        or verified.manifest_path != artifact_result.manifest_path
    ):
        raise RuntimeError("retained MCP verified artifact authority changed")
    if (
        verified.repository != artifact_result.repository
        or export_receipt.repository_key != artifact_result.repository
        or verified.commit != artifact_result.commit
        or verified.views != artifact_result.views
        or export_receipt.views != artifact_result.views
        or verified.file_count != artifact_result.file_count
        or verified.byte_count != artifact_result.byte_count
    ):
        raise RuntimeError("retained MCP materialization identity is inconsistent")

    inventoried = tuple(
        record for record in records if record.path != CONTEXT_ARTIFACT_MANIFEST
    )
    if (
        len(inventoried) != artifact_result.file_count
        or sum(record.size for record in inventoried) != artifact_result.byte_count
        or len(records) != artifact_result.file_count + 1
    ):
        raise RuntimeError("retained MCP receipt counts differ from materialization")
    return binding


def _activate_materialization(
    materialization: RepoManifestMaterializationResult,
    receipt_owner: PublishedWorkspaceReceiptOwner,
    runtime_owner: RetainedServerContextOwner,
    *,
    repo_path: str | Path | None,
) -> RetainedServerContextResult:
    """Consume one publication reader and activate its selected runtime."""

    artifact_result = materialization.artifact

    def activate(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> RetainedServerContextResult:
        binding = _cross_check_binding(
            materialization,
            receipt,
            publication,
            repo_path=repo_path,
            source_cleanup_owner=runtime_owner._source_owner,
        )
        if "bm25" not in artifact_result.views:
            raise ValueError("retained MCP contexts require a selected BM25 view")
        artifact = binding.artifact
        metadata: Mapping[str, object] = {
            "verified": True,
            "schema": CONTEXT_ARTIFACT_SCHEMA,
            "repository": artifact.repository,
            "commit": artifact.commit,
            "views": list(artifact.views),
        }
        context = ServerContext.load(
            binding.manifest,
            views=artifact_result.views,
            artifact=metadata,
            artifact_binding=binding,
            artifact_reader=publication,
            source_binding=binding.source_binding,
            _context_owner=runtime_owner._install_context,
        )
        if runtime_owner._slot is not context:
            raise RuntimeError("retained MCP context ownership changed during load")
        if (
            context.manifest is not binding.manifest
            or context._artifact_binding is not binding
            or context._native_index_authorization is not None
            or context.artifact != metadata
        ):
            raise RuntimeError("retained MCP context binding changed")
        if repo_path is None:
            if context._source_binding is not None or context.source_verified:
                raise RuntimeError("retained MCP query-only source binding changed")
        elif context._source_binding is None or not context.source_verified:
            raise RuntimeError("retained MCP source authority was not installed")
        if context.bm25 is None:
            detail = context.errors.get("bm25", "BM25 view did not load")
            raise RuntimeError(f"retained MCP BM25 view is unavailable: {detail}")
        if "vector" in artifact_result.views and context.vector is not None:
            raise RuntimeError("retained MCP vector view did not remain native-inert")

        loaded = context.loaded_views
        loaded_views = tuple(view for view in artifact_result.views if view in loaded)
        view_error_items = tuple(
            (view, context.errors.get(view, "view did not load"))
            for view in sorted(set(artifact_result.views) - set(loaded_views))
        )
        pure_result = RetainedServerContextResult(
            materialization=materialization,
            loaded_views=loaded_views,
            view_error_items=view_error_items,
        )
        runtime_owner._install_result(pure_result)
        return pure_result

    result = receipt_owner.consume(activate)
    if type(result) is not RetainedServerContextResult:
        raise RuntimeError("retained MCP receipt consumer returned an invalid result")
    if runtime_owner.result is not result:
        raise RuntimeError("retained MCP activated result ownership changed")
    return result


def _load_retained_server_context(
    runtime_owner: RetainedServerContextOwner,
    materialize: Callable[
        [PublishedWorkspaceReceiptOwner], RepoManifestMaterializationResult
    ],
    *,
    repository_key: str,
    namespace_name: str,
    destination: Path,
    ref_name: str | None,
    snapshot_id: str | None,
    expected_generation: int | None,
    repo_path: str | Path | None,
) -> RetainedServerContextResult:
    if type(runtime_owner) is not RetainedServerContextOwner:
        raise TypeError("runtime_owner must be a RetainedServerContextOwner")
    if not callable(materialize):
        raise TypeError("retained MCP materializer must be callable")
    # Fail before catalog/object-store/provider work, while leaving the actual
    # source capture inside the authenticated publication-reader callback.
    resolved_repo_path = _optional_lexical_repository_path(repo_path)

    reservation = object()
    cleanup = (
        _OrderedAction(
            label="retained MCP context cleanup also failed",
            action=lambda: runtime_owner._close_reservation(reservation),
            complete=lambda: not runtime_owner._owns_reservation(reservation)
            or runtime_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=runtime_owner,
        ),
    )
    with _run_context_with_cleanup_actions(cleanup, cleanup_on_success=False):
        runtime_owner._begin_loading(reservation)
        return runtime_owner._run_loading(
            reservation,
            lambda receipt_owner: _activate_materialization(
                _require_requested_materialization(
                    materialize(receipt_owner),
                    repository_key=repository_key,
                    namespace_name=namespace_name,
                    destination=destination,
                    ref_name=ref_name,
                    snapshot_id=snapshot_id,
                    expected_generation=expected_generation,
                ),
                receipt_owner,
                runtime_owner,
                repo_path=resolved_repo_path,
            ),
        )


def load_retained_server_context_ref(
    repository_key: str,
    destination: Path,
    *,
    catalog: RetainedSnapshotCatalog,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    runtime_owner: RetainedServerContextOwner,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int | None = None,
    repo_path: str | Path | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    environ: Mapping[str, str] | None = None,
) -> RetainedServerContextResult:
    """Resolve one retained ref and activate its exact portable context."""

    return _load_retained_server_context(
        runtime_owner,
        lambda receipt_owner: materialize_retained_repo_manifest_ref(
            repository_key,
            destination,
            catalog=catalog,
            object_store=object_store,
            workspace_provider=workspace_provider,
            output_receipt_owner=receipt_owner,
            namespace_name=namespace_name,
            ref_name=ref_name,
            expected_generation=expected_generation,
            max_manifest_bytes=max_manifest_bytes,
            max_projection_bytes=max_projection_bytes,
            max_context_files=max_context_files,
            max_context_bytes=max_context_bytes,
            max_bundle_files=max_bundle_files,
            max_bundle_bytes=max_bundle_bytes,
            max_bundle_metadata_bytes=max_bundle_metadata_bytes,
            environ=environ,
        ),
        repository_key=repository_key,
        namespace_name=namespace_name,
        destination=destination,
        ref_name=ref_name,
        snapshot_id=None,
        expected_generation=expected_generation,
        repo_path=repo_path,
    )


def load_retained_server_context_snapshot(
    repository_key: str,
    snapshot_id: str,
    destination: Path,
    *,
    catalog: RetainedSnapshotCatalog,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    runtime_owner: RetainedServerContextOwner,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    repo_path: str | Path | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    environ: Mapping[str, str] | None = None,
) -> RetainedServerContextResult:
    """Read one retained snapshot and activate its exact portable context."""

    return _load_retained_server_context(
        runtime_owner,
        lambda receipt_owner: materialize_retained_repo_manifest_snapshot(
            repository_key,
            snapshot_id,
            destination,
            catalog=catalog,
            object_store=object_store,
            workspace_provider=workspace_provider,
            output_receipt_owner=receipt_owner,
            namespace_name=namespace_name,
            max_manifest_bytes=max_manifest_bytes,
            max_projection_bytes=max_projection_bytes,
            max_context_files=max_context_files,
            max_context_bytes=max_context_bytes,
            max_bundle_files=max_bundle_files,
            max_bundle_bytes=max_bundle_bytes,
            max_bundle_metadata_bytes=max_bundle_metadata_bytes,
            environ=environ,
        ),
        repository_key=repository_key,
        namespace_name=namespace_name,
        destination=destination,
        ref_name=None,
        snapshot_id=snapshot_id,
        expected_generation=None,
        repo_path=repo_path,
    )


__all__ = [
    "RetainedServerContextOwner",
    "RetainedServerContextResult",
    "load_retained_server_context_ref",
    "load_retained_server_context_snapshot",
]
