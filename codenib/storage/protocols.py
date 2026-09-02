# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Minimal replaceable interfaces for catalog and object-store backends."""

from __future__ import annotations

import math
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Callable,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
    runtime_checkable,
)

from .models import (
    INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH,
    INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS,
    INDEX_JOB_EVENT_PAYLOAD_MAX_NODES,
    INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS,
    MAX_INDEX_JOB_EVENTS_PER_ATTEMPT,
    IndexJobAttemptCompletionRecord,
    IndexJobAttemptHeartbeat,
    IndexJobAttemptRecord,
    IndexJobCompletion,
    IndexJobCurrentResult,
    IndexJobEffectiveMode,
    IndexJobEventRecord,
    IndexJobRecord,
    IndexJobRunnableCursor,
    IndexJobRunnableCycle,
    IndexJobRunnablePage,
    IndexJobViewOutcome,
    IndexJobViewOutput,
    IndexJobViewRecord,
    RefJobLease,
    StorageIntegrityError,
)

if TYPE_CHECKING:
    from .cas import BlobInfo

RETAINED_IMPORT_RESPONSE_MAX_DEPTH = 64
RETAINED_IMPORT_RESPONSE_MAX_NODES = 250_000
RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS = 64 * 1024 * 1024
RETAINED_IMPORT_RESPONSE_MAX_KEY_CHARS = 4_096
RETAINED_IMPORT_CATALOG_CONTRACT = "codenib.retained-import-catalog.v1"
_RetainedResult = TypeVar("_RetainedResult")


def snapshot_retained_import_response(value: object, *, label: str) -> Any:
    """Detach and enforce the public exact-JSON retained response budget."""

    nodes = 0
    text_size = 0

    def snapshot(current: object, depth: int) -> Any:
        nonlocal nodes, text_size
        nodes += 1
        if nodes > RETAINED_IMPORT_RESPONSE_MAX_NODES:
            raise StorageIntegrityError(f"{label} exceeds its node limit")
        if depth > RETAINED_IMPORT_RESPONSE_MAX_DEPTH:
            raise StorageIntegrityError(f"{label} exceeds its depth limit")
        if current is None or type(current) is bool:
            return current
        if type(current) is str:
            text_size += len(current)
            if text_size > RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS or "\x00" in current:
                raise StorageIntegrityError(f"{label} contains invalid text")
            return current
        if type(current) is int:
            if not -(2**63) <= current < 2**63:
                raise StorageIntegrityError(f"{label} contains an invalid integer")
            return current
        if type(current) is float:
            if not math.isfinite(current):
                raise StorageIntegrityError(f"{label} contains a non-finite number")
            return current
        if type(current) is list:
            if len(current) > RETAINED_IMPORT_RESPONSE_MAX_NODES - nodes:
                raise StorageIntegrityError(f"{label} exceeds its node limit")
            return [snapshot(child, depth + 1) for child in current]
        if type(current) is dict:
            if len(current) > RETAINED_IMPORT_RESPONSE_MAX_NODES - nodes:
                raise StorageIntegrityError(f"{label} exceeds its node limit")
            result: dict[str, Any] = {}
            try:
                for key, child in current.items():
                    if (
                        type(key) is not str
                        or not key
                        or len(key) > RETAINED_IMPORT_RESPONSE_MAX_KEY_CHARS
                    ):
                        raise StorageIntegrityError(
                            f"{label} contains an invalid object key"
                        )
                    text_size += len(key)
                    if (
                        text_size > RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS
                        or "\x00" in key
                    ):
                        raise StorageIntegrityError(
                            f"{label} contains an invalid object key"
                        )
                    result[key] = snapshot(child, depth + 1)
            except StorageIntegrityError:
                raise
            except Exception as exc:
                raise StorageIntegrityError(
                    f"{label} could not be snapshotted"
                ) from exc
            if len(result) != len(current):
                raise StorageIntegrityError(f"{label} changed while snapshotted")
            return result
        raise StorageIntegrityError(f"{label} contains a non-exact JSON value")

    return snapshot(value, 1)


@runtime_checkable
class ObjectStore(Protocol):
    """Immutable byte-object operations required by artifact publication.

    ``BlobInfo`` is an immutable identity receipt, not a retention pin.  A
    receipt says which bytes were durably observed at one completed operation;
    it does not promise that a future GC cannot remove an unleased object.
    Consumers that require exact receipt revalidation can additionally require
    :class:`ReceiptVerifyingObjectStore` without breaking existing object-store
    implementations.  A separate ``open`` after verification is not pinned by
    that earlier result.  This protocol gains a longer lifetime only with an
    explicit pin or lease.  Publication coordinators use the additive
    :class:`ReceiptRetainingObjectStore` callback scope for that longer
    lifetime.
    """

    def put_bytes(self, data: bytes) -> BlobInfo: ...

    def put_file(self, source: str | Path) -> BlobInfo: ...

    def has(self, digest: str) -> bool: ...

    def open(self, digest: str) -> BinaryIO:
        """Open an unverified stream whose bytes the caller must authenticate."""

        ...

    def read_bytes(self, digest: str) -> bytes:
        """Return bytes authenticated against the digest during this read."""

        ...

    def verify(self, digest: str) -> BlobInfo: ...

    def materialize(self, digest: str, destination: str | Path) -> Path:
        """Materialize bytes authenticated during the copy; return a locator."""

        ...


@runtime_checkable
class ReceiptVerifyingObjectStore(ObjectStore, Protocol):
    """Additive capability for exact point-in-time receipt revalidation."""

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        """Revalidate one exact digest/size/storage-key receipt."""

        ...


@runtime_checkable
class InterruptibleReceiptVerifyingObjectStore(
    ReceiptVerifyingObjectStore,
    Protocol,
):
    """Additive capability for cancellable exact receipt revalidation.

    The separate method keeps the legacy one-argument ``verify_receipt``
    contract structurally compatible while allowing cancellation-aware callers
    to fail closed unless a backend explicitly implements this stronger seam.
    The callback is polled only before future content reads.  If it raises, the
    implementation propagates that exact exception object unless integrity
    already observable on the opened object is primary.  Once the expected
    receipt size has been read, bounded EOF/trailing-byte detection plus final
    digest, size, and open-object attestation complete without another poll so
    a current result cannot be masked by a newly observed stop.
    """

    def verify_receipt_interruptibly(
        self,
        expected: BlobInfo,
        *,
        check_cancelled: Callable[[], None],
    ) -> BlobInfo:
        """Revalidate one receipt; ``check_cancelled`` must be callable."""

        ...


@runtime_checkable
class ReceiptRetainingObjectStore(ReceiptVerifyingObjectStore, Protocol):
    """Additive capability for callback-scoped exact receipt retention."""

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _RetainedResult],
    ) -> _RetainedResult:
        """Verify and retain exact objects while *callback* uses them.

        Implementations must serialize garbage collection or reclamation
        against this scope.  Every receipt is revalidated before ``callback``
        starts, and its canonical storage key remains resolvable until the
        callback returns or raises.  This callback-shaped lease prevents a
        caller from accidentally escaping a backend-specific pin token.

        Implementations must invoke ``callback`` synchronously and exactly
        once, and must not return while that invocation is still running.  The
        method returns the exact object returned by ``callback`` or propagates
        the exact exception it raises; retention cleanup must not replace a
        callback failure.
        """

        ...


@runtime_checkable
class StreamingObjectStore(ObjectStore, Protocol):
    """Additive capability for bounded, expected-identity object ingestion.

    The producer is not part of the baseline :class:`ObjectStore` contract so
    existing backends remain structurally compatible.  Implementations must
    validate the expected identity and any reusable object before asking the
    producer for its first chunk.
    """

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        expected_digest: str,
        expected_size: int,
    ) -> BlobInfo:
        """Stream bytes that must match the exact expected identity."""

        ...


@runtime_checkable
class InterruptibleStreamingObjectStore(StreamingObjectStore, Protocol):
    """Additive capability for cancellable expected-identity ingestion.

    Implementations poll only before future existing-object reads or producer
    items.  A callback failure propagates as the exact exception object unless
    already-observable object or producer integrity is primary.  After the
    expected byte count is reached, the first producer EOF/trailing-item guard
    runs without another cancellation poll.  A zero-length guard item remains
    valid, after which polling resumes before each later future item; digest,
    size, durability, and receipt attestation remain current-result postflight.
    """

    def put_chunks_interruptibly(
        self,
        chunks: Iterable[bytes],
        expected_digest: str,
        expected_size: int,
        *,
        check_cancelled: Callable[[], None],
    ) -> BlobInfo:
        """Ingest or reuse one expected object with a callable stop check."""

        ...


@runtime_checkable
class RetainedImportObjectStore(
    StreamingObjectStore,
    ReceiptRetainingObjectStore,
    Protocol,
):
    """Capabilities required by retained artifact import coordinators.

    This intersection remains additive: streaming and receipt retention are
    not added to the baseline :class:`ObjectStore`, so existing adapters keep
    their runtime protocol shape until they explicitly implement retained
    imports.
    """


@runtime_checkable
class IndexCatalog(Protocol):
    """Transactional identities and publication, excluding query engines.

    Object registration is a metadata operation, not a byte upload.  A
    coordinator must first obtain and verify the corresponding ``ObjectStore``
    receipt and must compare digest, size, and storage key before publication.

    """

    def create_namespace(self, name: str) -> str:
        """Return the backend-neutral :class:`NamespaceIdentity` ID."""

        ...

    def create_repository(
        self,
        repository_key: str,
        *,
        namespace_id: str,
    ) -> str:
        """Return the exact backend-neutral :class:`RepositoryIdentity` ID."""

        ...

    def create_source_revision(
        self,
        repository_id: str,
        *,
        commit_sha: str | None = None,
        tree_sha: str | None = None,
        dirty: bool = False,
        source_fingerprint: str | None = None,
    ) -> str:
        """Return the exact backend-neutral :class:`SourceRevision` ID."""

        ...

    def create_view_profile(
        self,
        view_type: str,
        config: Mapping[str, Any] | None = None,
        *,
        name: str = "default",
    ) -> str:
        """Return the exact backend-neutral :class:`ViewProfile` ID."""

        ...

    def register_object(
        self,
        digest: str,
        *,
        storage_key: str,
        byte_size: int,
        media_type: str = "application/octet-stream",
    ) -> str:
        """Return the exact canonical digest of the registered object."""

        ...

    def stage_view_generation(
        self,
        repository_id: str,
        source_revision_id: str,
        profile_id: str,
        view_type: str,
        object_digest: str,
        *,
        schema_version: str,
        metadata: Mapping[str, Any] | None = None,
        member_object_digests: Sequence[str] = (),
    ) -> str:
        """Return the exact backend-neutral :class:`ViewGeneration` ID."""

        ...

    def publish_snapshot(
        self,
        repository_id: str,
        source_revision_id: str,
        view_generation_ids: Sequence[str],
        *,
        ref_name: str = "main",
        expected_generation: int = 0,
    ) -> dict[str, Any]:
        """Publish a desired snapshot or return its unchanged current ref.

        Implementations return ``changed=False`` without advancing the ref when
        it already targets the fully validated desired snapshot.  Otherwise,
        ``expected_generation`` is a compare-and-swap precondition and a
        successful advance returns ``changed=True``.

        The returned mapping contains core fields ``snapshot_id``,
        ``repository_id``, ``ref_name``, ``generation``, ``updated_at``, and
        ``changed``.
        """

        ...

    def resolve_ref(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> dict[str, Any]:
        """Return the ref core plus its identity-closed manifest.

        Ref generations are positive, monotonic signed 64-bit integers.
        For one repository/ref, ``publish_snapshot`` and ``resolve_ref`` are
        linearizable: a resolve after publish observes that publication or a
        strictly newer generation, never an older generation.
        """

        ...

    def get_manifest_summary(self, snapshot_id: str) -> dict[str, Any]:
        """Return a ready snapshot with namespace/repository identity closure.

        Identity-bearing core fields and ordered member lists are required.
        """

        ...


@runtime_checkable
class RetainedSnapshotCatalog(Protocol):
    """Read-only catalog surface for retained snapshot attestation.

    Every response uses exact built-in JSON values with finite floats and
    signed 64-bit integers. Keys are nonempty, NUL-free text no longer than
    ``RETAINED_IMPORT_RESPONSE_MAX_KEY_CHARS`` and all text is NUL-free. One
    response is bounded by ``RETAINED_IMPORT_RESPONSE_MAX_DEPTH`` levels,
    ``RETAINED_IMPORT_RESPONSE_MAX_NODES`` values, and
    ``RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS`` aggregate key/value characters,
    including extensions. Backends may add fields outside the core below;
    consumers ignore extensions only after authenticating exact core types and
    identities.

    ``resolve_ref`` returns repository/ref/snapshot/generation/updated_at and
    has a ``manifest`` with the summary shape below. Ref generations are
    positive, monotonic signed 64-bit integers.

    A manifest summary contains ``snapshot_id``, ``repository_id``, ``status``
    (``ready``), canonical ``published_at``, ``namespace``
    (``namespace_id``, ``name``), ``repository`` (``namespace_id``,
    ``repository_key``), ``source`` (``source_revision_id``, ``kind``,
    nullable ``commit_sha``/``tree_sha``, nonempty ``source_fingerprint``), and
    ``views``.
    Each view contains ``view_generation_id``, ``schema_version``, exact
    ``metadata``, ``profile`` (``profile_id``, ``name``, exact ``config``),
    ``object``, and ordered ``member_objects``. Object summaries contain
    ``digest``, ``storage_key``, integer ``byte_size``, and ``media_type``.
    The summary returned directly and the one nested in ``resolve_ref`` repeat
    the same immutable snapshot ``published_at``. Generation membership is
    bounded by :data:`codenib.storage.MAX_VIEW_GENERATION_MEMBERS`, keeping its
    canonical metadata plus object envelopes inside the response node budget.
    """

    def retained_import_contract(self) -> str:
        """Opt in to the exact retained-import response contract above."""

        ...

    def resolve_ref(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> dict[str, Any]: ...

    def get_manifest_summary(self, snapshot_id: str) -> dict[str, Any]: ...


@runtime_checkable
class RetainedImportCatalog(IndexCatalog, RetainedSnapshotCatalog, Protocol):
    """Mutation-capable retained import catalog.

    In addition to the read-only retained snapshot contract, publication
    results use exact ``snapshot_id``, ``repository_id``, ``ref_name``,
    positive ``generation``, exact ``changed`` boolean, and canonical
    ``updated_at``. For one repository/ref, ``publish_snapshot`` and
    ``resolve_ref`` are linearizable: a later resolve sees that publication or
    a strictly newer valid identity-closed ref.
    """

    pass


@runtime_checkable
class IndexJobPlanningCatalog(Protocol):
    """Least-authority source/profile registration and ref-fence lookup.

    Planning may create only content-addressed source and profile identities.
    The ref lookup returns ``0`` when the named ref has not been published and
    otherwise returns its positive generation without exposing snapshots,
    manifests, jobs, leases, or publication authority.
    """

    def create_source_revision(
        self,
        repository_id: str,
        *,
        commit_sha: str | None = None,
        tree_sha: str | None = None,
        dirty: bool = False,
        source_fingerprint: str | None = None,
    ) -> str: ...

    def create_view_profile(
        self,
        view_type: str,
        config: Mapping[str, Any] | None = None,
        *,
        name: str = "default",
    ) -> str: ...

    def read_ref_generation(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> int: ...


@runtime_checkable
class JobCreationCatalog(Protocol):
    """Least-authority atomic creation for one repository/ref job slot.

    Exact idempotent replay returns the original job. A different request is
    created only when the repository/ref has no queued or running job and its
    current generation matches the request's publication fence, so Web and
    other control planes cannot race stale or concurrent updates into the same
    publication slot.
    """

    def create_job_if_idle(
        self,
        repository_id: str,
        source_revision_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        *,
        ref_name: str = "main",
        expected_ref_generation: int = 0,
        max_attempts: int = 3,
    ) -> IndexJobRecord: ...


@runtime_checkable
class JobCreationReplayCatalog(JobCreationCatalog, Protocol):
    """Atomic job creation plus one exact idempotency replay lookup.

    The lookup is deliberately narrower than :class:`JobQueryCatalog`: callers
    cannot enumerate active work or read events, leases, attempts, or
    publication state.  It lets a control plane recover the immutable request
    after a committed response is lost without consulting a mutable planner.
    """

    def find_job_by_idempotency(
        self,
        repository_id: str,
        idempotency_key: str,
    ) -> IndexJobRecord | None: ...


@runtime_checkable
class JobResultCatalog(Protocol):
    """Least-authority reads for durable runtime reconciliation.

    ``get_job`` authenticates an exact worker callback. The current-result
    lookup returns a success only when its immutable publication snapshot,
    generation, and update timestamp still equal the named ref. Consumers
    cannot enumerate queued work, leases, attempts, events, manifests, or
    publication authority through this surface.
    """

    def get_job(self, job_id: str) -> IndexJobRecord: ...

    def find_current_successful_job(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> IndexJobCurrentResult | None: ...


@runtime_checkable
class JobResultActivationCatalog(JobResultCatalog, Protocol):
    """Current-result reads plus one writer-fenced runtime transfer boundary.

    ``run_current_successful_job_guarded`` revalidates the exact detached
    result under a backend-specific fence that excludes ref publication, calls
    the synchronous transfer exactly once while retaining that fence, and
    releases it only after the transfer returns. The callback must not reenter
    this catalog and must return ``None``. This surface grants no catalog
    mutation, job enumeration, lease, event, manifest, or publication access.
    """

    def run_current_successful_job_guarded(
        self,
        expected: IndexJobCurrentResult,
        transfer: Callable[[], None],
    ) -> None: ...


@runtime_checkable
class JobCatalog(Protocol):
    """Durable index-job coordination, separate from snapshot publication."""

    def create_job(
        self,
        repository_id: str,
        source_revision_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        *,
        ref_name: str = "main",
        expected_ref_generation: int = 0,
        max_attempts: int = 3,
    ) -> IndexJobRecord: ...

    def get_job(self, job_id: str) -> IndexJobRecord: ...

    def get_job_views(self, job_id: str) -> tuple[IndexJobViewRecord, ...]: ...

    def acquire_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_duration_ms: int,
    ) -> RefJobLease: ...

    def renew_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        lease_duration_ms: int,
    ) -> RefJobLease: ...

    def request_job_cancel(self, job_id: str) -> IndexJobRecord: ...

    def finish_job_attempt(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        outcome: IndexJobCompletion,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IndexJobRecord: ...


@runtime_checkable
class JobQueryCatalog(Protocol):
    """Read-only durable job surface for status consumers.

    ``find_active_job`` returns the running job for one repository/ref when it
    exists, otherwise the oldest queued job that can become the next active
    attempt. Terminal jobs remain addressable through ``get_job`` and event
    pages stay bounded by the caller-supplied limit.
    """

    def get_job(self, job_id: str) -> IndexJobRecord: ...

    def get_job_views(self, job_id: str) -> tuple[IndexJobViewRecord, ...]: ...

    def find_active_job(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> IndexJobRecord | None: ...

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> tuple[IndexJobEventRecord, ...]: ...


@runtime_checkable
class JobExecutionCatalog(JobCatalog, Protocol):
    """Additive, backend-neutral execution control for durable index jobs.

    Acquisition remains the authoritative claim and atomically records one
    immutable attempt start. The runnable scan is advisory and ordered by
    ``(created_at_ms, job_id)``; callers must still acquire. Attempt and
    completion history is returned in ascending attempt order and includes
    only schema-v6 starts after the immutable legacy baseline. New jobs start
    queued at one canonical database time. New active lease authority also
    starts at one database time with a bounded exact duration; released slots
    arise only by fenced release, never by direct insertion.
    Within the visible modeled suffix, each successor starts no earlier than
    the preceding requeue closure and carries a strictly greater fencing token.
    An initial claim response has acquisition and heartbeat equal to its attempt
    start and expiry exactly one requested duration later. Renewals preserve the
    acquisition time, keep heartbeat nondecreasing, strictly advance expiry,
    and keep expiry at least one requested duration beyond heartbeat.

    Event keys are attempt-local idempotency keys. Exact replay returns the
    original row, while a different closure conflicts. One ``view_result`` is
    allowed per attempt/view. Closing an attempt atomically records the exact
    event count, maximum sequence, and maximum event time; later events and
    replay against a different event prefix fail closed. Within one modeled
    attempt, event, cancellation, completion, and publication times follow one
    nondecreasing database-clock causal order. Heartbeat has its own
    nondecreasing lease-clock domain: after database-clock rollback it may lag
    an already committed content/cancellation high-water mark, but it cannot
    authorize a later event or closure below that content floor. Each attempt is limited to
    ``MAX_INDEX_JOB_EVENTS_PER_ATTEMPT`` events; canonical payload JSON is
    limited by the exported text, depth, node, and key bounds and rejects
    shared-classifier secret fields. A root ``Mapping`` is detached through a
    bounded key iterator without trusting its reported length; nested values
    require exact JSON containers and scalars. ``None`` means an empty payload.
    Event sequences are allocator-assigned; capacity or signed-int64 allocator
    exhaustion conflicts before mutation. Sequence, cursor, page-limit,
    attempt, lease-duration, and fencing values use exact integers, with
    persisted identities restricted to signed SQLite int64. Missing records,
    stale authority, replay mismatches, expired leases, and corrupt history
    fail closed using the backend's not-found, conflict, or validation errors.

    Queued cancellation commits a terminal cancelled job atomically. Running
    cancellation records the exact attempt/owner/fence and observed heartbeat
    for cooperative stop. Once cancellation is requested, only a
    ``cancelled`` non-success closure is valid; ``requeue`` and ``failed``
    require an uncancelled attempt. Success has no completion-row API and is
    durable only through ``publish_job_outputs``.
    """

    def scan_runnable_jobs(
        self,
        *,
        cursor: IndexJobRunnableCursor | None = None,
        limit: int = 64,
    ) -> IndexJobRunnablePage: ...

    def get_job_attempt(
        self,
        job_id: str,
        attempt_count: int,
    ) -> IndexJobAttemptRecord: ...

    def list_job_attempts(self, job_id: str) -> tuple[IndexJobAttemptRecord, ...]: ...

    def get_job_attempt_completion(
        self,
        job_id: str,
        attempt_count: int,
    ) -> IndexJobAttemptCompletionRecord: ...

    def list_job_attempt_completions(
        self,
        job_id: str,
    ) -> tuple[IndexJobAttemptCompletionRecord, ...]: ...

    def heartbeat_job_attempt(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        lease_duration_ms: int,
    ) -> IndexJobAttemptHeartbeat:
        """Renew exact live authority and atomically observe cancellation."""

        ...

    def append_job_event(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
    ) -> IndexJobEventRecord: ...

    def record_job_view_result(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        view_type: str,
        effective_mode: IndexJobEffectiveMode,
        outcome: IndexJobViewOutcome,
        payload: Mapping[str, Any] | None = None,
    ) -> IndexJobEventRecord: ...

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> tuple[IndexJobEventRecord, ...]: ...

    def complete_job_attempt(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        outcome: IndexJobCompletion,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IndexJobRecord:
        """Persist or replay an authority- and frontier-exact non-success."""

        ...


# Keep these names live in generated protocol documentation.
_JOB_EVENT_CONTRACT_BOUNDS = (
    MAX_INDEX_JOB_EVENTS_PER_ATTEMPT,
    INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS,
    INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH,
    INDEX_JOB_EVENT_PAYLOAD_MAX_NODES,
    INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS,
)


@runtime_checkable
class JobPublicationCatalog(JobCatalog, Protocol):
    """Job catalog supporting one atomic object-to-ref publication."""

    def publish_job_outputs(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        outputs: tuple[IndexJobViewOutput, ...],
    ) -> IndexJobRecord:
        """Publish one exact receipt-retained output closure.

        Object registration, compound generation membership, snapshot/ref CAS,
        the immutable publication receipt, job success, and lease release must
        commit or roll back together. A committed-response-loss retry with the
        same authority and closure returns the existing success without moving
        the ref; a different authority or closure conflicts without mutation.
        """

        ...


@runtime_checkable
class JobWorkerCatalog(JobExecutionCatalog, JobPublicationCatalog, Protocol):
    """Additive catalog capabilities required by an index-job worker.

    Keeping this intersection separate preserves the structural compatibility
    of publication-only adapters while letting a worker require durable
    execution control and fenced publication from each thread-local catalog
    session it opens.
    """

    pass


@runtime_checkable
class JobCycleWorkerCatalog(JobWorkerCatalog, Protocol):
    """Additive worker catalog surface for finite scheduler cycles.

    A cycle freezes the catalog's current immutable insertion sequence. Scans
    carrying that exact token exclude jobs created after the cycle began while
    preserving the ordinary advisory scan and fenced claim semantics.
    """

    def begin_runnable_job_cycle(self) -> IndexJobRunnableCycle:
        """Freeze one bounded insertion watermark for cursor traversal."""

        ...

    def scan_runnable_jobs(
        self,
        *,
        cursor: IndexJobRunnableCursor | None = None,
        cycle: IndexJobRunnableCycle | None = None,
        limit: int = 64,
    ) -> IndexJobRunnablePage: ...


__all__ = [
    "IndexCatalog",
    "InterruptibleReceiptVerifyingObjectStore",
    "InterruptibleStreamingObjectStore",
    "IndexJobPlanningCatalog",
    "JobCatalog",
    "JobCreationCatalog",
    "JobCreationReplayCatalog",
    "JobCycleWorkerCatalog",
    "JobExecutionCatalog",
    "JobPublicationCatalog",
    "JobResultActivationCatalog",
    "JobResultCatalog",
    "JobWorkerCatalog",
    "ObjectStore",
    "ReceiptRetainingObjectStore",
    "ReceiptVerifyingObjectStore",
    "RETAINED_IMPORT_CATALOG_CONTRACT",
    "RETAINED_IMPORT_RESPONSE_MAX_DEPTH",
    "RETAINED_IMPORT_RESPONSE_MAX_KEY_CHARS",
    "RETAINED_IMPORT_RESPONSE_MAX_NODES",
    "RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS",
    "RetainedImportCatalog",
    "RetainedImportObjectStore",
    "RetainedSnapshotCatalog",
    "snapshot_retained_import_response",
    "StreamingObjectStore",
]
