# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Crash-safe local content-addressed storage."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, TypeVar

from .. import _atomic_directory as _atomic
from .._owned_file_publication import (
    OwnedFileConflictError,
    PublishedFileReceipt,
    PublishedFileRecord,
    _CancellationSafeRLock,
    publish_owned_file,
    require_owned_file_publication_support,
)
from .models import StorageIntegrityError, StorageValidationError

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COPY_BUFFER_SIZE = 1024 * 1024
_MAX_OBJECT_BYTES = 64 << 30
_SAFE_DIRECTORY_FDS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.mkdir in os.supports_dir_fd
)
_CAS_OBJECT_MODE = 0o600
_SHARD_NAMES = tuple(f"{value:02x}" for value in range(256))
_DIRECTORY_CLOSE_RECOVERY_LIMIT = 64
_PORTABLE_LINK_UNSUPPORTED_ERRNOS = {
    value
    for value in (
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "EXDEV", None),
    )
    if value is not None
}
_PORTABLE_PUBLISH_LOCKS = tuple(threading.Lock() for _ in range(64))
_STRICT_STATE_INACTIVE = "inactive"
_STRICT_STATE_INITIALIZING = "initializing"
_STRICT_STATE_ACTIVE = "active"
_STRICT_STATE_CLOSING = "closing"
_STRICT_STATE_CLOSED = "closed"
_CASResult = TypeVar("_CASResult")


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """Point-in-time identity receipt for one immutable CAS object.

    This value is not a retention lease.  Revalidate it at each later metadata
    boundary and authenticate bytes inside the actual read operation.  Use
    :meth:`LocalCAS.retain_receipts` when catalog publication needs a bounded
    retention scope.
    """

    digest: str
    byte_size: int
    storage_key: str


def _install_strict_state(
    store: LocalCAS,
    *,
    require_preprovisioned: bool,
    strict_root_identity: tuple[int, ...] | None,
    strict_sha256_identity: tuple[int, ...] | None,
    strict_shard_identities: dict[str, tuple[int, ...]],
    strict_resources: _atomic._PosixResourceOwner,
    strict_root_descriptor: int | None,
    strict_sha256_descriptor: int | None,
    strict_shard_descriptors: dict[str, int],
) -> None:
    """Transfer constructor-local strict resources to a LocalCAS instance."""

    store._strict_root_identity = strict_root_identity
    store._strict_sha256_identity = strict_sha256_identity
    store._strict_shard_identities = strict_shard_identities
    store._strict_resources = strict_resources if require_preprovisioned else None
    store._strict_root_descriptor = strict_root_descriptor
    store._strict_sha256_descriptor = strict_sha256_descriptor
    store._strict_shard_descriptors = strict_shard_descriptors
    store._owner_pid = os.getpid()
    store._strict_lifecycle_state = (
        _STRICT_STATE_ACTIVE if require_preprovisioned else _STRICT_STATE_INACTIVE
    )


class LocalCAS:
    """A SHA-256 content-addressed store backed by regular local files.

    Objects are stored below ``sha256/<first two hex digits>/<remaining hex>``.
    A digest is always the bare, lowercase, 64-character hexadecimal value.

    ``provision()`` plus ``require_preprovisioned=True`` is the strict
    publication mode. It opens every internal component relative to a held
    directory descriptor and never creates a directory during ``put``. The
    default lazy layout remains a cooperative compatibility mode because POSIX
    cannot bind ``mkdir`` and the following ``open`` into one authority step.
    Platforms without owned publication retain the pre-existing portable lazy
    backend with explicit lstat/fstat checks and weaker replacement-race
    protection; strict mode remains fail-closed there.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        require_preprovisioned: bool = False,
    ) -> None:
        self._lifecycle_lock = _CancellationSafeRLock()
        self._owner_pid = os.getpid()
        self._process_locks = {self._owner_pid: self._lifecycle_lock}
        self._strict_lifecycle_state = _STRICT_STATE_INACTIVE
        if not isinstance(require_preprovisioned, bool):
            raise TypeError("require_preprovisioned must be a boolean")
        try:
            _require_local_cas_support()
        except RuntimeError:
            if require_preprovisioned:
                # Strict mode must reject unsupported hosts before even
                # interpreting or inspecting the requested filesystem path.
                raise
            portable_lazy = True
        else:
            portable_lazy = False
        requested_root = Path(root).expanduser()
        self._portable_lazy = portable_lazy
        if portable_lazy:
            try:
                requested_metadata = requested_root.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISDIR(requested_metadata.st_mode):
                    raise StorageValidationError(
                        f"path is not a real directory: {requested_root}"
                    )
            self.root = requested_root.resolve()
            self._require_preprovisioned = False
            _ensure_directory(self.root)
            self._sha256_root = self.root / "sha256"
            with _open_portable_directory_path(
                self.root,
                label="CAS root",
            ) as root_descriptor:
                with _open_or_create_portable_child_directory(
                    root_descriptor,
                    self.root,
                    "sha256",
                    label="CAS SHA-256 root",
                ):
                    pass
            self._strict_root_identity = None
            self._strict_sha256_identity = None
            self._strict_shard_identities = {}
            self._strict_resources = None
            self._strict_root_descriptor = None
            self._strict_sha256_descriptor = None
            self._strict_shard_descriptors = {}
            self._owner_pid = os.getpid()
            return

        self.root = Path(os.path.abspath(os.fspath(requested_root)))
        self._require_preprovisioned = require_preprovisioned
        if require_preprovisioned:
            self._strict_lifecycle_state = _STRICT_STATE_INITIALIZING
        strict_root_identity: tuple[int, ...] | None = None
        strict_sha256_identity: tuple[int, ...] | None = None
        strict_shard_identities: dict[str, tuple[int, ...]] = {}
        strict_resources = _atomic._PosixResourceOwner()
        strict_root_descriptor: int | None = None
        strict_sha256_descriptor: int | None = None
        strict_shard_descriptors: dict[str, int] = {}
        if not require_preprovisioned:
            try:
                _ensure_cas_root(self.root)
            except ValueError as exc:
                raise StorageValidationError(
                    f"path is not a real directory: {self.root}"
                ) from exc
        self._sha256_root = self.root / "sha256"
        try:
            with _open_directory_path(
                self.root,
                label="CAS root",
            ) as root_descriptor:
                if require_preprovisioned:
                    strict_root_descriptor = _retain_directory_descriptor(
                        strict_resources,
                        root_descriptor,
                    )
                    strict_root_identity = _directory_resource_identity(
                        strict_root_descriptor,
                        path=self.root,
                        label="CAS root",
                    )
                open_sha256 = (
                    _open_child_directory
                    if require_preprovisioned
                    else _open_or_create_child_directory
                )
                with open_sha256(
                    root_descriptor,
                    self.root,
                    "sha256",
                    label="CAS SHA-256 root",
                ) as (sha256_descriptor, sha256_path):
                    if require_preprovisioned:
                        strict_sha256_descriptor = _retain_directory_descriptor(
                            strict_resources,
                            sha256_descriptor,
                        )
                        strict_sha256_identity = _directory_resource_identity(
                            strict_sha256_descriptor,
                            path=sha256_path,
                            label="CAS SHA-256 root",
                        )
                        for shard_name in _SHARD_NAMES:
                            with _open_child_directory(
                                sha256_descriptor,
                                sha256_path,
                                shard_name,
                                label="CAS digest shard",
                            ) as (shard_descriptor, shard_path):
                                retained_shard = _retain_directory_descriptor(
                                    strict_resources,
                                    shard_descriptor,
                                )
                                strict_shard_descriptors[shard_name] = retained_shard
                                strict_shard_identities[shard_name] = (
                                    _directory_resource_identity(
                                        retained_shard,
                                        path=shard_path,
                                        label="CAS digest shard",
                                    )
                                )
            if not require_preprovisioned:
                _close_posix_resources(strict_resources)
            # Keep the complete local owner inside this exception boundary
            # until every retained descriptor is reachable from ``self``.
            # Otherwise cancellation after the last shard is retained but before
            # the first attribute store can strand all 258 strict anchors.
            _install_strict_state(
                self,
                require_preprovisioned=require_preprovisioned,
                strict_root_identity=strict_root_identity,
                strict_sha256_identity=strict_sha256_identity,
                strict_shard_identities=strict_shard_identities,
                strict_resources=strict_resources,
                strict_root_descriptor=strict_root_descriptor,
                strict_sha256_descriptor=strict_sha256_descriptor,
                strict_shard_descriptors=strict_shard_descriptors,
            )
        except FileNotFoundError as exc:
            _close_posix_resources(strict_resources, primary_error=exc)
            if require_preprovisioned:
                try:
                    self.root.lstat()
                except FileNotFoundError:
                    raise StorageValidationError(
                        f"preprovisioned CAS root does not exist: {self.root}"
                    ) from exc
                raise StorageValidationError(
                    "preprovisioned CAS layout must contain sha256 and all 256 "
                    "digest shards"
                ) from exc
            raise
        except ValueError as exc:
            _close_posix_resources(strict_resources, primary_error=exc)
            raise StorageValidationError(
                f"path is not a real directory: {self.root}"
            ) from exc
        except BaseException as exc:
            _close_posix_resources(strict_resources, primary_error=exc)
            raise

    def close(self) -> None:
        """Release strict generation anchors retained for this store."""

        if not getattr(self, "_require_preprovisioned", False):
            return
        current_pid = os.getpid()
        if current_pid != self._owner_pid:
            child_lock = self._process_locks.setdefault(
                current_pid,
                _CancellationSafeRLock(),
            )
            close_error: BaseException | None = None
            try:
                child_lock.run(self._close_strict_resources_locked)
            except BaseException as exc:  # noqa: B036 - report PID boundary
                close_error = exc
            boundary_error = StorageIntegrityError(
                "strict CAS authority crossed a PID boundary"
            )
            if close_error is not None:
                raise boundary_error from close_error
            raise boundary_error
        self._run_lifecycle(self._close_strict_resources_locked)

    def _run_lifecycle(self, callback: Callable[[], _CASResult]) -> _CASResult:
        """Run one same-process strict lifecycle transition linearly."""

        return self._lifecycle_lock.run(callback)

    def _close_strict_resources_locked(self) -> None:
        state = self._strict_lifecycle_state
        if state in {_STRICT_STATE_INACTIVE, _STRICT_STATE_CLOSED}:
            return
        if state in {_STRICT_STATE_ACTIVE, _STRICT_STATE_INITIALIZING}:
            # Store the terminal intent before touching the first descriptor.
            # A failed or interrupted close can then be retried, while no later
            # operation can enter against a partially released anchor set.
            self._strict_lifecycle_state = _STRICT_STATE_CLOSING
        elif state != _STRICT_STATE_CLOSING:
            raise RuntimeError(f"strict CAS lifecycle state is invalid: {state}")

        resources = getattr(self, "_strict_resources", None)
        if resources is None:
            self._strict_lifecycle_state = _STRICT_STATE_CLOSED
            return
        primary_error: BaseException | None = None
        try:
            _close_posix_resources(resources)
        except BaseException as exc:  # noqa: B036 - settle closed ownership
            primary_error = exc
        try:
            closed = resources.closed
        except BaseException as observation_error:  # noqa: B036
            if primary_error is None:
                primary_error = observation_error
            else:
                _atomic._annotate_secondary_error(
                    primary_error,
                    "strict CAS close-state observation also failed",
                    observation_error,
                )
            closed = False
        if closed:
            # State remains ``closing`` while these stores run, so an
            # interruption cannot make a later operation observe an active
            # authority after any descriptor has been released.
            self._strict_resources = None
            self._strict_root_descriptor = None
            self._strict_sha256_descriptor = None
            self._strict_shard_descriptors = {}
            self._strict_lifecycle_state = _STRICT_STATE_CLOSED
        if primary_error is not None:
            raise primary_error.with_traceback(primary_error.__traceback__)

    def _run_strict_operation(
        self,
        callback: Callable[[], _CASResult],
    ) -> _CASResult:
        """Serialize one strict operation with anchor cleanup."""

        if not self._require_preprovisioned:
            return callback()
        if os.getpid() != self._owner_pid:
            raise StorageIntegrityError("strict CAS authority crossed a PID boundary")

        def run_locked() -> _CASResult:
            if (
                self._strict_lifecycle_state != _STRICT_STATE_ACTIVE
                or self._strict_resources is None
            ):
                raise StorageIntegrityError("strict CAS authority is closed")
            return callback()

        return self._run_lifecycle(run_locked)

    def __enter__(self) -> LocalCAS:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @classmethod
    def provision(cls, root: str | Path) -> LocalCAS:
        """Create the complete layout inside a trusted, quiescent boundary.

        POSIX ``mkdir`` followed by ``open`` cannot prove that a same-UID actor
        did not replace the new directory in between.  Provisioning therefore
        creates every shard before strict publication begins; it is not a safe
        online operation in the adversarial replacement model.
        """

        _require_local_cas_support()
        requested_root = Path(root).expanduser()
        lexical_root = Path(os.path.abspath(os.fspath(requested_root)))
        root_parent = lexical_root.parent
        if root_parent == lexical_root or not lexical_root.name:
            raise StorageValidationError("CAS root must have a lexical parent")
        try:
            with _open_directory_path(
                root_parent,
                label="CAS root parent",
            ) as root_parent_descriptor:
                # Only the final root leaf may be created.  Requiring its
                # lexical parent to pre-exist makes the unconditional replay
                # below a complete durability chain, including after an
                # ambiguous parent-fsync failure on an earlier attempt.
                with _open_or_create_child_directory(
                    root_parent_descriptor,
                    root_parent,
                    lexical_root.name,
                    label="CAS root",
                ) as (root_descriptor, rebound_root):
                    with _open_or_create_child_directory(
                        root_descriptor,
                        rebound_root,
                        "sha256",
                        label="CAS SHA-256 root",
                    ) as (sha256_descriptor, sha256_path):
                        for shard_name in _SHARD_NAMES:
                            with _open_or_create_child_directory(
                                sha256_descriptor,
                                sha256_path,
                                shard_name,
                                label="CAS digest shard",
                            ):
                                pass
                        # Replay the complete durability chain unconditionally.
                        # A previous mkdir may have committed before its parent
                        # fsync raised, so an existing entry is not a receipt.
                        os.fsync(sha256_descriptor)
                    os.fsync(root_descriptor)
                os.fsync(root_parent_descriptor)
        except FileNotFoundError as exc:
            raise StorageValidationError(
                f"CAS root parent must already exist: {root_parent}"
            ) from exc
        except ValueError as exc:
            raise StorageValidationError(
                f"CAS root parent must be a real directory: {root_parent}"
            ) from exc
        return cls(lexical_root, require_preprovisioned=True)

    def put_bytes(self, data: bytes) -> BlobInfo:
        """Store *data*, deduplicating an already-valid immutable object."""

        def put() -> BlobInfo:
            if not isinstance(data, bytes):
                raise TypeError("CAS payload must be bytes")
            digest = hashlib.sha256(data).hexdigest()
            return self._publish_object(digest, len(data), data)

        return self._run_strict_operation(put)

    def put_file(self, source: str | Path) -> BlobInfo:
        """Store a stable snapshot of a regular file.

        The source is read twice.  The first pass establishes its digest and
        destination; the second feeds an owned destination-directory stage.
        Identity and metadata checks around both passes reject a source that is
        replaced or modified while it is being stored.
        """

        def put() -> BlobInfo:
            source_path = Path(source)
            digest, byte_size, source_signature = _hash_stable_file(source_path)
            chunks = _stable_file_chunks(
                source_path,
                expected_signature=source_signature,
                expected_digest=digest,
                expected_size=byte_size,
            )
            return self._publish_object(digest, byte_size, chunks)

        return self._run_strict_operation(put)

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        expected_digest: str,
        expected_size: int,
    ) -> BlobInfo:
        """Stream one expected-identity object into the CAS.

        Platform support, the expected identity, and any existing canonical
        object are checked before the producer is iterated.  Reuse therefore
        performs no producer reads.  New bytes are bounded and authenticated
        while the owned publication stage is written, before its final name
        can be installed.
        """

        return self._put_chunks(
            chunks,
            expected_digest,
            expected_size,
            check_cancelled=None,
        )

    def put_chunks_interruptibly(
        self,
        chunks: Iterable[bytes],
        expected_digest: str,
        expected_size: int,
        *,
        check_cancelled: Callable[[], None],
    ) -> BlobInfo:
        """Stream or reuse an expected object with cancellation between reads."""

        if not callable(check_cancelled):
            raise TypeError("CAS chunk cancellation check must be callable")
        return self._put_chunks(
            chunks,
            expected_digest,
            expected_size,
            check_cancelled=check_cancelled,
        )

    def _put_chunks(
        self,
        chunks: Iterable[bytes],
        expected_digest: str,
        expected_size: int,
        *,
        check_cancelled: Callable[[], None] | None,
    ) -> BlobInfo:
        if type(expected_digest) is not str:
            raise StorageValidationError(
                "digest must be 64 lowercase hexadecimal characters"
            )
        digest = _validate_digest(expected_digest)
        byte_size = _validate_expected_object_size(expected_size)
        _require_local_cas_support()
        reused = (
            self._reuse_expected_object(digest, byte_size)
            if check_cancelled is None
            else self._reuse_expected_object(
                digest,
                byte_size,
                check_cancelled=check_cancelled,
            )
        )
        if reused is not None:
            return reused
        validated_chunks = (
            _ValidatedObjectChunks(
                chunks,
                expected_digest=digest,
                expected_size=byte_size,
            )
            if check_cancelled is None
            else _ValidatedObjectChunks(
                chunks,
                expected_digest=digest,
                expected_size=byte_size,
                check_cancelled=check_cancelled,
            )
        )
        try:
            return self._publish_object(digest, byte_size, validated_chunks)
        except _InterruptibleChunkStop as signal:
            error = signal.error
            _inherit_interruptible_exception_settlement(signal, error)
            raise error  # noqa: B904 - preserve exact callback exception

    def has(self, digest: str) -> bool:
        """Return whether a regular object exists for *digest*.

        This is an existence check, not a full-content read.  Use :meth:`verify`
        before trusting an object at a publication boundary.
        """

        def check() -> bool:
            try:
                handle = self.open(digest)
            except FileNotFoundError:
                return False
            handle.close()
            return True

        return self._run_strict_operation(check)

    def open(self, digest: str) -> BinaryIO:
        """Open a CAS object for binary reading without following symlinks."""

        def open_object() -> BinaryIO:
            validated = _validate_digest(digest)
            with self._open_shard(validated) as (shard_descriptor, shard_path):
                return _open_regular_at(
                    shard_descriptor,
                    shard_path,
                    validated[2:],
                    label="CAS object",
                )

        return self._run_strict_operation(open_object)

    def read_bytes(self, digest: str) -> bytes:
        """Read an object and reject content that does not match its key."""

        def read() -> bytes:
            chunks: list[bytes] = []
            observed_digest, byte_size = self._consume_object(digest, chunks=chunks)
            if observed_digest != digest:
                raise StorageIntegrityError(f"CAS object digest mismatch for {digest}")
            # ``byte_size`` is consumed to keep the full-read and verify paths
            # symmetrical; joining the recorded chunks cannot change its value.
            payload = b"".join(chunks)
            if len(payload) != byte_size:
                raise StorageIntegrityError(
                    f"CAS object size changed while reading {digest}"
                )
            return payload

        return self._run_strict_operation(read)

    def verify(self, digest: str) -> BlobInfo:
        """Fully verify an object and return its canonical metadata."""

        def verify_object() -> BlobInfo:
            observed_digest, byte_size = self._consume_object(digest)
            if observed_digest != digest:
                raise StorageIntegrityError(f"CAS object digest mismatch for {digest}")
            return self._blob_info(digest, byte_size)

        return self._run_strict_operation(verify_object)

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        """Revalidate an exact object receipt without claiming a retention pin.

        The full digest/size/storage-key tuple is compared after one backend
        verification.  This is deliberately a point-in-time validation gate:
        future pin- and lease-aware GC must provide any longer-lived retention
        guarantee.
        """

        return self._verify_receipt(expected, check_cancelled=None)

    def verify_receipt_interruptibly(
        self,
        expected: BlobInfo,
        *,
        check_cancelled: Callable[[], None],
    ) -> BlobInfo:
        """Revalidate an exact receipt with cancellation between future reads."""

        if not callable(check_cancelled):
            raise TypeError("object receipt cancellation check must be callable")
        return self._verify_receipt(expected, check_cancelled=check_cancelled)

    def _verify_receipt(
        self,
        expected: BlobInfo,
        *,
        check_cancelled: Callable[[], None] | None,
    ) -> BlobInfo:
        if (
            type(expected) is not BlobInfo
            or type(expected.digest) is not str
            or type(expected.byte_size) is not int
            or type(expected.storage_key) is not str
        ):
            raise TypeError("expected object receipt must be BlobInfo")
        digest = _validate_digest(expected.digest)
        if isinstance(expected.byte_size, bool) or not isinstance(
            expected.byte_size, int
        ):
            raise StorageValidationError(
                "object receipt byte size must be a nonnegative integer"
            )
        if expected.byte_size < 0:
            raise StorageValidationError(
                "object receipt byte size must be a nonnegative integer"
            )
        canonical = self._blob_info(digest, expected.byte_size)
        if expected != canonical:
            raise StorageValidationError("object receipt is not canonical")
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("object receipt cancellation check must be callable")
        if check_cancelled is None:
            observed = self.verify(digest)
        else:

            def verify_expected_object() -> BlobInfo:
                observed_digest, byte_size = self._consume_object(
                    digest,
                    expected_size=expected.byte_size,
                    check_cancelled=check_cancelled,
                )
                if observed_digest != digest:
                    raise StorageIntegrityError(
                        f"CAS object digest mismatch for {digest}"
                    )
                return self._blob_info(digest, byte_size)

            observed = self._run_strict_operation(verify_expected_object)
        if observed != expected:
            raise StorageIntegrityError(
                f"verified CAS object receipt does not match expected receipt: {digest}"
            )
        return observed

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _CASResult],
    ) -> _CASResult:
        """Run *callback* while exact receipts are protected from local GC.

        LocalCAS has no garbage collector today.  The lifecycle lock is the
        retention fence that any future local reclamation path must share.
        Verification and the callback run under one cancellation-safe lock, so
        no compliant collector can enter between the receipt gate and catalog
        publication.
        """

        if type(expected) is not tuple:
            raise TypeError("retained object receipts must be an exact tuple")
        if not expected:
            raise StorageValidationError("retained object receipts must not be empty")
        if not callable(callback):
            raise TypeError("retained object callback must be callable")
        receipts = tuple(expected)
        for receipt in receipts:
            if type(receipt) is not BlobInfo:
                raise TypeError("retained object receipt must be BlobInfo")

        def retained() -> _CASResult:
            seen: set[str] = set()
            for receipt in receipts:
                verified = self.verify_receipt(receipt)
                if verified.digest in seen:
                    raise StorageValidationError(
                        "retained object receipts must have unique digests"
                    )
                seen.add(verified.digest)
            return callback()

        return self._run_lifecycle(retained)

    def materialize(self, digest: str, destination: str | Path) -> Path:
        """Atomically copy a verified object to a regular destination path."""

        return self._run_strict_operation(
            lambda: self._materialize_locked(digest, destination)
        )

    def _materialize_locked(
        self,
        digest: str,
        destination: str | Path,
    ) -> Path:
        """Copy an object while the strict lifecycle lease is held."""

        digest = _validate_digest(digest)
        source = self._object_path(digest)
        destination_path = Path(destination)
        _ensure_directory(destination_path.parent)
        _validate_materialization_target(destination_path)

        if _same_lexical_path(source, destination_path):
            self.verify(digest)
            return destination_path

        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(destination_path.parent),
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            observed = hashlib.sha256()
            byte_size = 0
            with os.fdopen(descriptor, "wb") as target, self.open(digest) as stored:
                source_signature = _object_signature(os.fstat(stored.fileno()))
                while block := stored.read(_COPY_BUFFER_SIZE):
                    observed.update(block)
                    byte_size += len(block)
                    target.write(block)
                if _object_signature(os.fstat(stored.fileno())) != source_signature:
                    raise StorageIntegrityError(
                        f"CAS object changed while reading {digest}"
                    )
                target.flush()
                os.fsync(target.fileno())

            if observed.hexdigest() != digest:
                raise StorageIntegrityError(f"CAS object digest mismatch for {digest}")

            # Check again immediately before replacement so a destination that
            # became a symlink or special file is never silently accepted.
            _validate_materialization_target(destination_path)
            os.replace(temporary, destination_path)
            _fsync_directory(destination_path.parent)
            return destination_path
        finally:
            temporary.unlink(missing_ok=True)

    def _object_path(self, digest: str) -> Path:
        digest = _validate_digest(digest)
        parent = self._sha256_root / digest[:2]
        return parent / digest[2:]

    @contextmanager
    def _open_shard(
        self,
        digest: str,
        *,
        create: bool = False,
    ) -> Iterator[tuple[int | None, Path]]:
        digest = _validate_digest(digest)
        if self._portable_lazy:
            with _open_portable_directory_path(
                self.root,
                label="CAS root",
            ) as root_descriptor:
                with _open_portable_child_directory(
                    root_descriptor,
                    self.root,
                    "sha256",
                    label="CAS SHA-256 root",
                ) as (sha256_descriptor, sha256_path):
                    open_child = (
                        _open_or_create_portable_child_directory
                        if create
                        else _open_portable_child_directory
                    )
                    with open_child(
                        sha256_descriptor,
                        sha256_path,
                        digest[:2],
                        label="CAS digest shard",
                    ) as shard:
                        yield shard
            return

        self._require_strict_anchors(digest[:2])
        with _open_directory_path(self.root, label="CAS root") as root_descriptor:
            _require_directory_generation(
                root_descriptor,
                self._strict_root_identity,
                path=self.root,
                label="CAS root",
            )
            with _open_child_directory(
                root_descriptor,
                self.root,
                "sha256",
                label="CAS SHA-256 root",
            ) as (sha256_descriptor, sha256_path):
                _require_directory_generation(
                    sha256_descriptor,
                    self._strict_sha256_identity,
                    path=sha256_path,
                    label="CAS SHA-256 root",
                )
                open_child = (
                    _open_or_create_child_directory if create else _open_child_directory
                )
                with open_child(
                    sha256_descriptor,
                    sha256_path,
                    digest[:2],
                    label="CAS digest shard",
                ) as (shard_descriptor, shard_path):
                    _require_directory_generation(
                        shard_descriptor,
                        self._strict_shard_identities.get(digest[:2]),
                        path=shard_path,
                        label="CAS digest shard",
                    )
                    yield shard_descriptor, shard_path

    def _require_strict_anchors(self, shard_name: str) -> None:
        if not self._require_preprovisioned:
            return
        if os.getpid() != self._owner_pid:
            raise StorageIntegrityError("strict CAS authority crossed a PID boundary")
        if (
            self._strict_lifecycle_state != _STRICT_STATE_ACTIVE
            or self._strict_resources is None
        ):
            raise StorageIntegrityError("strict CAS authority is closed")
        anchors = (
            (
                self._strict_root_descriptor,
                self._strict_root_identity,
                self.root,
                "CAS root",
            ),
            (
                self._strict_sha256_descriptor,
                self._strict_sha256_identity,
                self._sha256_root,
                "CAS SHA-256 root",
            ),
            (
                self._strict_shard_descriptors.get(shard_name),
                self._strict_shard_identities.get(shard_name),
                self._sha256_root / shard_name,
                "CAS digest shard",
            ),
        )
        for descriptor, expected, path, label in anchors:
            if descriptor is None or expected is None:
                raise StorageIntegrityError(f"{label} generation anchor is absent")
            _require_directory_generation(
                descriptor,
                expected,
                path=path,
                label=label,
            )

    @staticmethod
    def _blob_info(digest: str, byte_size: int) -> BlobInfo:
        digest = _validate_digest(digest)
        return BlobInfo(
            digest=digest,
            byte_size=byte_size,
            storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
        )

    def _publish_object(
        self,
        digest: str,
        byte_size: int,
        chunks: Iterable[bytes] | bytes,
    ) -> BlobInfo:
        if self._portable_lazy:
            return self._publish_portable_object(digest, byte_size, chunks)

        expected = PublishedFileRecord(
            size=byte_size,
            sha256=digest,
            mode=_CAS_OBJECT_MODE,
        )
        with self._open_shard(
            digest,
            create=not self._require_preprovisioned,
        ) as (shard_descriptor, shard_path):
            if shard_descriptor is None:
                raise RuntimeError(
                    "strict CAS publication requires an anchored shard descriptor"
                )
            destination = shard_path / digest[2:]
            expected_parent_identity = _atomic.publication_parent_identity(
                shard_descriptor
            )

            def consume(receipt: PublishedFileReceipt) -> None:
                if (
                    receipt.path != destination
                    or receipt.record != expected
                    or receipt.parent_identity != expected_parent_identity
                ):
                    raise StorageIntegrityError(
                        f"CAS publication receipt conflicts for {digest}"
                    )

            try:
                publish_owned_file(
                    destination,
                    chunks,
                    max_bytes=byte_size,
                    mode=_CAS_OBJECT_MODE,
                    parent_resource=shard_descriptor,
                    consume=consume,
                )
            except OwnedFileConflictError as exc:
                raise StorageIntegrityError(
                    f"existing CAS object failed integrity validation: {destination}"
                ) from exc

        # BlobInfo is deliberately created only after the helper has installed,
        # synchronously consumed, and terminally closed the post-directory-fsync
        # receipt.  No descriptor-owning resource crosses this return boundary.
        return self._blob_info(digest, byte_size)

    def _publish_portable_object(
        self,
        digest: str,
        byte_size: int,
        chunks: Iterable[bytes] | bytes,
    ) -> BlobInfo:
        """Publish with the pre-owned-publication compatibility backend."""

        info = self._blob_info(digest, byte_size)
        with self._open_shard(digest, create=True) as (
            shard_descriptor,
            shard_path,
        ):
            if shard_descriptor is not None:
                raise RuntimeError("portable CAS unexpectedly acquired a directory fd")
            if self._verify_portable_existing(shard_path, info) is not None:
                _fsync_directory_handle(None, shard_path)
                return info

            descriptor, temporary_name = _create_portable_temporary_file(
                shard_path,
                digest[2:],
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    if isinstance(chunks, bytes):
                        handle.write(chunks)
                    else:
                        for block in chunks:
                            handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._publish_portable_temporary(
                    shard_path,
                    temporary_name,
                    info,
                )
                return info
            finally:
                _unlink_portable(shard_path, temporary_name)
                _fsync_directory_handle(None, shard_path)

    def _verify_portable_existing(
        self,
        shard_path: Path,
        expected: BlobInfo,
    ) -> BlobInfo | None:
        path = shard_path / expected.digest[2:]
        try:
            with _open_regular_at(
                None,
                shard_path,
                expected.digest[2:],
                label="existing CAS object",
            ) as handle:
                signature = _object_signature(os.fstat(handle.fileno()))
                observed = hashlib.sha256()
                byte_size = 0
                while block := handle.read(_COPY_BUFFER_SIZE):
                    observed.update(block)
                    byte_size += len(block)
                if _object_signature(os.fstat(handle.fileno())) != signature:
                    raise StorageIntegrityError(
                        f"existing CAS object changed while read: {path}"
                    )
        except FileNotFoundError:
            return None

        if byte_size != expected.byte_size or observed.hexdigest() != expected.digest:
            raise StorageIntegrityError(
                f"existing CAS object failed integrity validation: {path}"
            )
        return expected

    def _publish_portable_temporary(
        self,
        shard_path: Path,
        temporary_name: str,
        info: BlobInfo,
    ) -> None:
        destination_name = info.digest[2:]
        if self._verify_portable_existing(shard_path, info) is not None:
            return

        try:
            os.link(
                shard_path / temporary_name,
                shard_path / destination_name,
            )
            return
        except FileExistsError:
            if self._verify_portable_existing(shard_path, info) is None:
                raise StorageIntegrityError(
                    f"CAS object disappeared during publication: {info.digest}"
                ) from None
            return
        except OSError as exc:
            if exc.errno not in _PORTABLE_LINK_UNSUPPORTED_ERRNOS:
                raise

        lock = _PORTABLE_PUBLISH_LOCKS[
            int(info.digest[:2], 16) % len(_PORTABLE_PUBLISH_LOCKS)
        ]
        with lock:
            if self._verify_portable_existing(shard_path, info) is not None:
                return
            os.replace(
                shard_path / temporary_name,
                shard_path / destination_name,
            )

    def _reuse_expected_object(
        self,
        digest: str,
        byte_size: int,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> BlobInfo | None:
        """Authenticate and durably rebind an existing canonical object."""

        shard_opened = False
        try:
            with self._open_shard(digest) as (shard_descriptor, shard_path):
                shard_opened = True
                if shard_descriptor is None:
                    raise RuntimeError(
                        "strict CAS reuse requires an anchored shard descriptor"
                    )
                name = digest[2:]
                destination = shard_path / name
                try:
                    before = os.stat(
                        name,
                        dir_fd=shard_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return None
                if not _is_canonical_object(before, expected_size=byte_size):
                    raise _existing_object_conflict(destination)

                try:
                    handle = _open_regular_at(
                        shard_descriptor,
                        shard_path,
                        name,
                        label="CAS object",
                    )
                except FileNotFoundError as exc:
                    raise _existing_object_conflict(destination) from exc
                with _close_binary_handle(handle):
                    opened = os.fstat(handle.fileno())
                    signature = _canonical_object_signature(opened)
                    if signature != _canonical_object_signature(before):
                        raise _existing_object_conflict(destination)

                    observed = hashlib.sha256()
                    remaining = byte_size
                    while remaining:
                        if check_cancelled is not None:
                            try:
                                check_cancelled()
                            except BaseException as cancellation_error:  # noqa: B036
                                try:
                                    open_signature = _canonical_object_signature(
                                        os.fstat(handle.fileno())
                                    )
                                    rebound_signature = _canonical_object_signature(
                                        os.stat(
                                            name,
                                            dir_fd=shard_descriptor,
                                            follow_symlinks=False,
                                        )
                                    )
                                except OSError:
                                    raise _existing_object_conflict(
                                        destination
                                    ) from cancellation_error
                                if (
                                    open_signature != signature
                                    or rebound_signature != signature
                                ):
                                    raise _existing_object_conflict(
                                        destination
                                    ) from cancellation_error
                                raise
                        block = handle.read(min(_COPY_BUFFER_SIZE, remaining))
                        if not block or len(block) > remaining:
                            raise _existing_object_conflict(destination)
                        observed.update(block)
                        remaining -= len(block)
                    if handle.read(1):
                        raise _existing_object_conflict(destination)
                    if observed.hexdigest() != digest:
                        raise _existing_object_conflict(destination)
                    if (
                        _canonical_object_signature(os.fstat(handle.fileno()))
                        != signature
                    ):
                        raise _existing_object_conflict(destination)

                    # A prior publication may have committed before reporting
                    # an fsync failure. Replay both the object and directory
                    # durability boundaries before issuing a reuse receipt.
                    os.fsync(handle.fileno())
                    if (
                        _canonical_object_signature(os.fstat(handle.fileno()))
                        != signature
                    ):
                        raise _existing_object_conflict(destination)
                    rebound = os.stat(
                        name,
                        dir_fd=shard_descriptor,
                        follow_symlinks=False,
                    )
                    if _canonical_object_signature(rebound) != signature:
                        raise _existing_object_conflict(destination)
                    os.fsync(shard_descriptor)
                    if (
                        _canonical_object_signature(os.fstat(handle.fileno()))
                        != signature
                    ):
                        raise _existing_object_conflict(destination)
                    durable_rebound = os.stat(
                        name,
                        dir_fd=shard_descriptor,
                        follow_symlinks=False,
                    )
                    if _canonical_object_signature(durable_rebound) != signature:
                        raise _existing_object_conflict(destination)
        except FileNotFoundError:
            if shard_opened:
                raise
            return None
        return self._blob_info(digest, byte_size)

    def _consume_object(
        self,
        digest: str,
        *,
        chunks: list[bytes] | None = None,
        expected_size: int | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> tuple[str, int]:
        digest = _validate_digest(digest)
        observed = hashlib.sha256()
        byte_size = 0
        handle = self.open(digest)
        with _close_binary_handle(handle):
            opened = os.fstat(handle.fileno())
            interruptible_expected = (
                check_cancelled is not None and expected_size is not None
            )
            signature_function = (
                _canonical_object_signature
                if interruptible_expected
                else _object_signature
            )
            signature = signature_function(opened)
            if expected_size is not None and (
                opened.st_size != expected_size
                or (
                    interruptible_expected
                    and not _is_canonical_object(
                        opened,
                        expected_size=expected_size,
                    )
                )
            ):
                raise StorageIntegrityError(
                    f"verified CAS object receipt does not match expected receipt: "
                    f"{digest}"
                )
            while True:
                if check_cancelled is not None and (
                    expected_size is None or byte_size < expected_size
                ):
                    try:
                        check_cancelled()
                    except BaseException as cancellation_error:  # noqa: B036
                        integrity_error = StorageIntegrityError(
                            f"CAS object changed while reading {digest}"
                        )
                        try:
                            current_signature = signature_function(
                                os.fstat(handle.fileno())
                            )
                        except OSError as attestation_error:
                            _atomic._annotate_secondary_error(
                                integrity_error,
                                "CAS object cancellation reconciliation also failed",
                                attestation_error,
                            )
                            raise integrity_error from cancellation_error
                        if current_signature != signature:
                            raise integrity_error from cancellation_error
                        raise
                read_size = _COPY_BUFFER_SIZE
                if expected_size is not None:
                    remaining = expected_size - byte_size
                    read_size = min(_COPY_BUFFER_SIZE, remaining) if remaining else 1
                block = handle.read(read_size)
                if not block:
                    break
                observed.update(block)
                byte_size += len(block)
                if chunks is not None:
                    chunks.append(block)
                if expected_size is not None and byte_size > expected_size:
                    raise StorageIntegrityError(
                        "verified CAS object receipt does not match expected receipt: "
                        f"{digest}"
                    )
            if signature_function(os.fstat(handle.fileno())) != signature:
                raise StorageIntegrityError(
                    f"CAS object changed while reading {digest}"
                )
        return observed.hexdigest(), byte_size


def _directory_resource_identity(
    descriptor: int | None,
    *,
    path: Path,
    label: str,
) -> tuple[int, ...]:
    if descriptor is None:
        raise RuntimeError(f"{label} has no anchored directory descriptor: {path}")
    try:
        return _atomic.publication_parent_identity(descriptor)
    except OSError as exc:
        raise StorageIntegrityError(
            f"{label} authority could not be identified: {path}"
        ) from exc


def _retain_directory_descriptor(
    resources: _atomic._PosixResourceOwner,
    descriptor: int | None,
) -> int:
    if descriptor is None:
        raise RuntimeError("strict CAS has no directory descriptor to retain")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    retained = resources.open(".", flags, dir_fd=descriptor)
    os.set_inheritable(retained, False)
    return retained


def _require_directory_generation(
    descriptor: int | None,
    expected: tuple[int, ...] | None,
    *,
    path: Path,
    label: str,
) -> None:
    if expected is None:
        return
    if _directory_resource_identity(descriptor, path=path, label=label) != expected:
        raise StorageIntegrityError(f"{label} generation changed: {path}")


def _close_posix_resources(
    resources: _atomic._PosixResourceOwner,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Close every directory fd, retrying while retaining the first error."""

    first_error = primary_error
    for _attempt in range(_DIRECTORY_CLOSE_RECOVERY_LIMIT):
        if resources.closed:
            break
        try:
            resources.close_all()
        except BaseException as close_error:  # noqa: B036 - converge ownership
            if first_error is None:
                first_error = close_error
            else:
                _atomic._annotate_secondary_error(
                    first_error,
                    "directory descriptor cleanup also failed",
                    close_error,
                )
    if not resources.closed:
        convergence_error = RuntimeError(
            "directory descriptor cleanup did not converge"
        )
        if first_error is None:
            first_error = convergence_error
        else:
            _atomic._annotate_secondary_error(
                first_error,
                "directory descriptor cleanup did not converge",
                convergence_error,
            )
    if primary_error is None and first_error is not None:
        raise first_error


def _close_publication_authority_owner(
    authority_owner: _atomic._PublicationAuthorityOwner,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Retry atomic authority cleanup without directory-offset cookies."""

    first_error = primary_error
    for _attempt in range(_DIRECTORY_CLOSE_RECOVERY_LIMIT):
        if authority_owner.authority is None:
            break
        try:
            authority_owner.close()
        except BaseException as close_error:  # noqa: B036 - converge ownership
            if first_error is None:
                first_error = close_error
            else:
                _atomic._annotate_secondary_error(
                    first_error,
                    "directory authority cleanup also failed",
                    close_error,
                )
    if authority_owner.authority is not None:
        convergence_error = RuntimeError("directory authority cleanup did not converge")
        if first_error is None:
            first_error = convergence_error
        else:
            _atomic._annotate_secondary_error(
                first_error,
                "directory authority cleanup did not converge",
                convergence_error,
            )
    if primary_error is None and first_error is not None:
        raise first_error


@contextmanager
def _open_portable_directory_path(
    path: Path,
    *,
    label: str,
) -> Iterator[int | None]:
    """Open a directory through the legacy path-only compatibility checks."""

    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageIntegrityError(f"{label} is not a real directory: {path}")
    # Unsupported platforms cannot anchor later child operations to this exact
    # generation.  Keep this intentionally separate from the POSIX authority
    # helpers so a supported host never falls through to weaker semantics.
    yield None


@contextmanager
def _open_portable_child_directory(
    parent_descriptor: int | None,
    parent_path: Path,
    name: str,
    *,
    label: str,
) -> Iterator[tuple[int | None, Path]]:
    if parent_descriptor is not None:
        raise RuntimeError("portable CAS unexpectedly acquired a directory fd")
    path = parent_path / name
    with _open_portable_directory_path(path, label=label) as descriptor:
        yield descriptor, path


@contextmanager
def _open_or_create_portable_child_directory(
    parent_descriptor: int | None,
    parent_path: Path,
    name: str,
    *,
    label: str,
) -> Iterator[tuple[int | None, Path]]:
    if parent_descriptor is not None:
        raise RuntimeError("portable CAS unexpectedly acquired a directory fd")
    created = False
    path = parent_path / name
    try:
        path.mkdir()
        created = True
    except FileExistsError:
        pass
    if created:
        _fsync_directory(parent_path)
    with _open_portable_child_directory(
        None,
        parent_path,
        name,
        label=label,
    ) as child:
        yield child


@contextmanager
def _open_directory_path(path: Path, *, label: str) -> Iterator[int | None]:
    authority_owner = _atomic._PublicationAuthorityOwner()
    try:
        _atomic._open_publication_authority(
            path,
            parent_resource=None,
            expected_parent_identity=None,
            create_missing=False,
            authority_owner=authority_owner,
        )
        authority = authority_owner.authority
        if authority is None:
            raise RuntimeError(f"{label} authority was not installed: {path}")
        authority.verify_path_binding()
        yield authority.resource
        authority.verify_path_binding()
    except BaseException as primary_error:
        _close_publication_authority_owner(
            authority_owner,
            primary_error=primary_error,
        )
        raise
    else:
        _close_publication_authority_owner(authority_owner)


@contextmanager
def _open_child_directory(
    parent_descriptor: int | None,
    parent_path: Path,
    name: str,
    *,
    label: str,
) -> Iterator[tuple[int | None, Path]]:
    path = parent_path / name
    if parent_descriptor is None:
        with _open_directory_path(path, label=label) as descriptor:
            yield descriptor, path
        return

    resources = _atomic._PosixResourceOwner()
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageIntegrityError(f"{label} is not a real directory: {path}")

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = resources.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise StorageIntegrityError(f"{label} is not a directory: {path}")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise StorageIntegrityError(f"{label} changed while opening: {path}")
        yield descriptor, path
        rebound = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino):
            raise StorageIntegrityError(f"{label} binding changed: {path}")
    except BaseException as primary_error:
        _close_posix_resources(resources, primary_error=primary_error)
        raise
    else:
        _close_posix_resources(resources)


@contextmanager
def _open_or_create_child_directory(
    parent_descriptor: int | None,
    parent_path: Path,
    name: str,
    *,
    label: str,
) -> Iterator[tuple[int | None, Path]]:
    created = False
    try:
        if parent_descriptor is None:
            (parent_path / name).mkdir()
        else:
            os.mkdir(name, mode=0o755, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        pass

    if created:
        # Persist the directory entry in its parent before an object published
        # inside the new shard can be reported durable.
        _fsync_directory_handle(parent_descriptor, parent_path)

    with _open_child_directory(
        parent_descriptor,
        parent_path,
        name,
        label=label,
    ) as child:
        yield child


def _open_regular_at(
    directory_descriptor: int | None,
    directory_path: Path,
    name: str,
    *,
    label: str,
) -> BinaryIO:
    if directory_descriptor is None:
        return _open_regular_file(directory_path / name, label=label)

    metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageIntegrityError(
            f"{label} is not a regular file: {directory_path / name}"
        )

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise StorageIntegrityError(
                f"{label} is not a regular file: {directory_path / name}"
            )
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise StorageIntegrityError(
                f"{label} changed while opening: {directory_path / name}"
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _create_portable_temporary_file(
    directory_path: Path,
    object_name: str,
) -> tuple[int, str]:
    descriptor, temporary_path = tempfile.mkstemp(
        dir=str(directory_path),
        prefix=f".{object_name}.",
        suffix=".tmp",
    )
    return descriptor, Path(temporary_path).name


def _unlink_portable(directory_path: Path, name: str) -> None:
    try:
        (directory_path / name).unlink()
    except FileNotFoundError:
        pass


class _InterruptibleChunkStop(BaseException):
    """Carry an exact StopIteration through an iterator protocol boundary."""

    __slots__ = ("error",)

    def __init__(self, error: StopIteration) -> None:
        self.error = error


def _inherit_interruptible_exception_settlement(
    source: BaseException,
    target: BaseException,
) -> None:
    """Transfer cleanup diagnostics and retry owners before exact unwrapping."""

    for attribute in ("__notes__", "_codenib_cleanup_notes"):
        try:
            values = BaseException.__getattribute__(source, attribute)
        except BaseException:  # noqa: B036 - best-effort diagnostics only
            continue
        if type(values) not in (list, tuple):
            continue
        for note in values:
            if type(note) is not str:
                continue
            try:
                add_note = getattr(BaseException, "add_note", None)
                if add_note is not None:
                    add_note(target, note)
                    continue
                try:
                    retained = BaseException.__getattribute__(
                        target,
                        "_codenib_cleanup_notes",
                    )
                except AttributeError:
                    retained = ()
                if type(retained) is not tuple:
                    retained = ()
                BaseException.__setattr__(
                    target,
                    "_codenib_cleanup_notes",
                    (*retained, note),
                )
            except BaseException:  # noqa: B036 - never mask the exact stop
                pass

    try:
        publication_owners = BaseException.__getattribute__(
            source,
            "publication_cleanup_owners",
        )
    except BaseException:  # noqa: B036 - no retained publication cleanup
        publication_owners = ()
    if type(publication_owners) is tuple:
        for owner in publication_owners:
            _atomic._attach_publication_cleanup_owner(target, owner)

    try:
        bundle_owners = BaseException.__getattribute__(
            source,
            "_codenib_bundle_stream_cleanup_owners",
        )
    except BaseException:  # noqa: B036 - no retained bundle cleanup
        bundle_owners = ()
    if type(bundle_owners) is tuple:
        try:
            try:
                retained_bundle_owners = BaseException.__getattribute__(
                    target,
                    "_codenib_bundle_stream_cleanup_owners",
                )
            except AttributeError:
                retained_bundle_owners = ()
            if type(retained_bundle_owners) is not tuple:
                retained_bundle_owners = ()
            merged = list(retained_bundle_owners)
            for owner in bundle_owners:
                if not any(candidate is owner for candidate in merged):
                    merged.append(owner)
            BaseException.__setattr__(
                target,
                "_codenib_bundle_stream_cleanup_owners",
                tuple(merged),
            )
        except BaseException:  # noqa: B036 - exact stop remains primary
            pass


class _ValidatedObjectChunks(Iterator[bytes]):
    """One-shot producer adapter with exact terminal identity validation."""

    __slots__ = (
        "_byte_size",
        "_check_cancelled",
        "_chunks",
        "_closed",
        "_expected_digest",
        "_expected_size",
        "_iterator",
        "_observed",
        "_terminal_probe_observed",
    )

    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        expected_digest: str,
        expected_size: int,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        self._chunks: Iterable[bytes] | None = chunks
        self._iterator: Iterator[bytes] | None = None
        self._expected_digest = expected_digest
        self._expected_size = expected_size
        self._check_cancelled = check_cancelled
        self._observed = hashlib.sha256()
        self._byte_size = 0
        self._terminal_probe_observed = False
        self._closed = False

    def __iter__(self) -> _ValidatedObjectChunks:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        if self._check_cancelled is not None and (
            self._byte_size < self._expected_size or self._terminal_probe_observed
        ):
            self._poll_cancelled()
        iterator_was_missing = self._iterator is None
        try:
            iterator = self._start()
        except BaseException as primary_error:  # noqa: B036 - acquisition failed
            self._close_preserving(primary_error)
            if isinstance(primary_error, StopIteration):
                raise RuntimeError(
                    "CAS chunk iterator acquisition raised StopIteration"
                ) from primary_error
            raise
        if (
            iterator_was_missing
            and self._check_cancelled is not None
            and self._byte_size < self._expected_size
        ):
            self._poll_cancelled()
        try:
            block = next(iterator)
        except StopIteration:
            completion_error = self._completion_error()
            if completion_error is not None:
                self._close_preserving(completion_error)
                raise completion_error
            self.close()
            raise
        except BaseException as primary_error:  # noqa: B036 - close producer
            self._close_preserving(primary_error)
            raise

        try:
            if type(block) is not bytes:
                raise TypeError("CAS chunk producer must yield bytes")
            at_expected_size = self._byte_size == self._expected_size
            # The first post-size item is the unpolled current EOF/trailing
            # guard.  A legacy-compatible empty item advances the boundary so
            # later unknown items become cancellable future work.
            if at_expected_size and block:
                raise StorageIntegrityError(
                    "CAS chunk producer exceeds its expected object size"
                )
            if len(block) > self._expected_size - self._byte_size:
                raise StorageIntegrityError(
                    "CAS chunk producer exceeds its expected object size"
                )
            self._observed.update(block)
            self._byte_size += len(block)
            if at_expected_size:
                self._terminal_probe_observed = True
        except BaseException as primary_error:  # noqa: B036 - close producer
            self._close_preserving(primary_error)
            raise
        return block

    def _poll_cancelled(self) -> None:
        assert self._check_cancelled is not None
        try:
            self._check_cancelled()
        except BaseException as primary_error:  # noqa: B036 - exact stop
            self._close_preserving(primary_error)
            if isinstance(primary_error, StopIteration):
                raise _InterruptibleChunkStop(primary_error) from primary_error
            raise

    def close(self) -> None:
        """Terminally stop iteration and close its producer at most once.

        ``_closed`` is only the iteration-terminal marker.  The retained
        ``_iterator`` is the close authority: an interruption after the marker
        changes must not make a later cleanup attempt believe that the producer
        was already handled.  Converge the marker first, retaining its first
        interruption, and then hand off the producer exactly once.

        Detaching ``_iterator`` is the arbitrary-callback boundary.  Dynamic
        ``close`` lookup and invocation can run producer code which commits a
        side effect before raising, so neither operation may be replayed after
        that handoff.
        """

        transition_error: BaseException | None = None
        while not self._closed:
            try:
                self._closed = True
            except BaseException as error:  # noqa: B036 - finish transition
                if transition_error is None:
                    transition_error = error
                else:
                    _atomic._annotate_secondary_error(
                        transition_error,
                        "CAS chunk iterator terminal transition also failed",
                        error,
                    )

        try:
            self._close_producer_once()
        except BaseException as close_error:  # noqa: B036 - keep first error
            if transition_error is None:
                raise
            _atomic._annotate_secondary_error(
                transition_error,
                "CAS chunk producer cleanup also failed",
                close_error,
            )
        if transition_error is not None:
            raise transition_error

    def _close_producer_once(self) -> None:
        """Consume retained producer-close authority without replaying it."""

        iterator = self._iterator
        self._iterator = None
        self._chunks = None
        if iterator is None:
            return
        try:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        except StopIteration as exc:
            raise RuntimeError(
                "CAS chunk iterator cleanup raised StopIteration"
            ) from exc

    def _start(self) -> Iterator[bytes]:
        iterator = self._iterator
        if iterator is not None:
            return iterator
        chunks = self._chunks
        if chunks is None:
            self._closed = True
            raise RuntimeError("CAS chunk producer is no longer available")
        try:
            iterator = iter(chunks)
        except BaseException:  # noqa: B036 - make iterator acquisition terminal
            self._closed = True
            self._chunks = None
            raise
        self._iterator = iterator
        self._chunks = None
        return iterator

    def _completion_error(self) -> StorageIntegrityError | None:
        if self._byte_size != self._expected_size:
            return StorageIntegrityError(
                "CAS chunk producer size does not match its expected object size"
            )
        if self._observed.hexdigest() != self._expected_digest:
            return StorageIntegrityError(
                "CAS chunk producer digest does not match its expected digest"
            )
        return None

    def _close_preserving(self, primary_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as close_error:  # noqa: B036 - keep first primary
            _atomic._annotate_secondary_error(
                primary_error,
                "CAS chunk iterator cleanup also failed",
                close_error,
            )


@contextmanager
def _close_binary_handle(handle: BinaryIO) -> Iterator[None]:
    """Close one opened object while retaining an earlier verification error."""

    try:
        yield
    except BaseException as primary_error:  # noqa: B036 - retain first primary
        try:
            handle.close()
        except BaseException as close_error:  # noqa: B036 - diagnostic only
            _atomic._annotate_secondary_error(
                primary_error,
                "CAS object handle cleanup also failed",
                close_error,
            )
        raise
    else:
        handle.close()


def _validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise StorageValidationError(
            "digest must be 64 lowercase hexadecimal characters"
        )
    return digest


def _validate_expected_object_size(value: object) -> int:
    if type(value) is not int or value < 0:
        raise StorageValidationError(
            "expected object size must be a nonnegative integer"
        )
    if value > _MAX_OBJECT_BYTES:
        raise StorageValidationError(
            f"expected object size exceeds the {_MAX_OBJECT_BYTES}-byte limit"
        )
    return value


def _is_canonical_object(
    metadata: os.stat_result,
    *,
    expected_size: int,
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _CAS_OBJECT_MODE
        and metadata.st_size == expected_size
    )


def _existing_object_conflict(destination: Path) -> StorageIntegrityError:
    return StorageIntegrityError(
        f"existing CAS object failed integrity validation: {destination}"
    )


def _require_local_cas_support() -> None:
    """Fail before layout mutation unless anchored shard fds are available."""

    require_owned_file_publication_support()
    if not _SAFE_DIRECTORY_FDS:
        raise RuntimeError("LocalCAS requires POSIX directory-fd support")


def _ensure_cas_root(path: Path) -> None:
    """Create a cooperative root through one lexical no-follow authority."""

    authority_owner = _atomic._PublicationAuthorityOwner()
    try:
        _atomic._open_publication_authority(
            path,
            parent_resource=None,
            expected_parent_identity=None,
            create_missing=True,
            authority_owner=authority_owner,
        )
        authority = authority_owner.authority
        if authority is None:
            raise RuntimeError("CAS root authority was not installed")
        authority.verify_path_binding()
    except BaseException as primary_error:
        _close_publication_authority_owner(
            authority_owner,
            primary_error=primary_error,
        )
        raise
    else:
        _close_publication_authority_owner(authority_owner)


def _ensure_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise StorageValidationError(
                    f"directory hierarchy has no existing root: {path}"
                ) from None
            current = parent
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageValidationError(f"path is not a real directory: {current}")
        break

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            # A concurrent creator is acceptable only if it published the same
            # kind of real directory.  We fsync its entry below as well.
            pass
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageValidationError(f"path is not a real directory: {directory}")
        _fsync_directory(directory.parent)


def _open_regular_file(path: Path, *, label: str) -> BinaryIO:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageIntegrityError(f"{label} is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise StorageIntegrityError(f"{label} is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise StorageIntegrityError(f"{label} changed while opening: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _hash_stable_file(path: Path) -> tuple[str, int, tuple[int, ...]]:
    observed = hashlib.sha256()
    byte_size = 0
    with _open_regular_file(path, label="CAS source") as source:
        signature = _file_signature(os.fstat(source.fileno()))
        while block := source.read(_COPY_BUFFER_SIZE):
            observed.update(block)
            byte_size += len(block)
        if _file_signature(os.fstat(source.fileno())) != signature:
            raise OSError(f"source changed while being hashed: {path}")
    _require_path_signature(path, signature, label="CAS source")
    if byte_size != signature[3]:
        raise OSError(f"source size changed while being hashed: {path}")
    return observed.hexdigest(), byte_size, signature


def _stable_file_chunks(
    path: Path,
    *,
    expected_signature: tuple[int, ...],
    expected_digest: str,
    expected_size: int,
) -> Iterator[bytes]:
    """Yield pass-two bytes and reject drift before normal iterator completion."""

    observed = hashlib.sha256()
    byte_size = 0
    with _open_regular_file(path, label="CAS source") as source:
        signature = _file_signature(os.fstat(source.fileno()))
        if signature != expected_signature:
            raise OSError(f"source changed before it could be stored: {path}")
        while block := source.read(_COPY_BUFFER_SIZE):
            observed.update(block)
            byte_size += len(block)
            yield block
        if _file_signature(os.fstat(source.fileno())) != signature:
            raise OSError(f"source changed while being stored: {path}")
    _require_path_signature(path, signature, label="CAS source")
    if observed.hexdigest() != expected_digest or byte_size != expected_size:
        # This check runs before StopIteration reaches OwnedFile.write(), so a
        # pass-two mismatch can leave only an owned stage and is never renamed.
        raise OSError(f"source changed while being stored: {path}")


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _object_signature(metadata: os.stat_result) -> tuple[int, ...]:
    """Metadata expected to remain stable while immutable bytes are read.

    Link-count changes update ctime on POSIX.  A successful no-replace publish
    briefly gives the object both its temporary and canonical names, so ctime
    is deliberately excluded here.  The full SHA-256 check still detects any
    content change.
    """

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _canonical_object_signature(metadata: os.stat_result) -> tuple[int, ...]:
    """Exact metadata required throughout canonical-object reuse."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_path_signature(
    path: Path,
    expected: tuple[int, ...],
    *,
    label: str,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise OSError(f"{label} disappeared while being read: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or _file_signature(metadata) != expected:
        raise OSError(f"{label} changed while being read: {path}")


def _validate_materialization_target(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageValidationError(
            f"materialization target is not a regular file: {path}"
        )


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # Some supported platforms cannot open a directory descriptor.  File
        # content is still flushed before its atomic replacement there.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_handle(descriptor: int | None, path: Path) -> None:
    if descriptor is None:
        _fsync_directory(path)
    else:
        os.fsync(descriptor)


__all__ = ["BlobInfo", "LocalCAS"]
