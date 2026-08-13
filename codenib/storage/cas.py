# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Crash-safe local content-addressed storage."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from .. import _atomic_directory as _atomic
from .._owned_file_publication import (
    OwnedFileConflictError,
    PublishedFileReceipt,
    PublishedFileRecord,
    publish_owned_file,
    require_owned_file_publication_support,
)
from .models import StorageIntegrityError, StorageValidationError

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COPY_BUFFER_SIZE = 1024 * 1024
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


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """Identity and location of one immutable CAS object."""

    digest: str
    byte_size: int
    storage_key: str


class LocalCAS:
    """A SHA-256 content-addressed store backed by regular local files.

    Objects are stored below ``sha256/<first two hex digits>/<remaining hex>``.
    A digest is always the bare, lowercase, 64-character hexadecimal value.

    ``provision()`` plus ``require_preprovisioned=True`` is the strict
    publication mode. It opens every internal component relative to a held
    directory descriptor and never creates a directory during ``put``. The
    default lazy layout remains a cooperative compatibility mode because POSIX
    cannot bind ``mkdir`` and the following ``open`` into one authority step.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        require_preprovisioned: bool = False,
    ) -> None:
        if not isinstance(require_preprovisioned, bool):
            raise TypeError("require_preprovisioned must be a boolean")
        # This capability check is deliberately before lstat/mkdir.  In
        # particular, unsupported Windows publication must not create even the
        # legacy cooperative layout before it fails.
        _require_local_cas_support()
        requested_root = Path(root).expanduser()
        self.root = Path(os.path.abspath(os.fspath(requested_root)))
        self._require_preprovisioned = require_preprovisioned
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
            self._strict_root_identity = strict_root_identity
            self._strict_sha256_identity = strict_sha256_identity
            self._strict_shard_identities = strict_shard_identities
            self._strict_resources = (
                strict_resources if require_preprovisioned else None
            )
            self._strict_root_descriptor = strict_root_descriptor
            self._strict_sha256_descriptor = strict_sha256_descriptor
            self._strict_shard_descriptors = strict_shard_descriptors
            self._owner_pid = os.getpid()
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

        resources = self._strict_resources
        if resources is None:
            return
        _close_posix_resources(resources)
        self._strict_resources = None
        self._strict_root_descriptor = None
        self._strict_sha256_descriptor = None
        self._strict_shard_descriptors.clear()

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

        if not isinstance(data, bytes):
            raise TypeError("CAS payload must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        return self._publish_object(digest, len(data), data)

    def put_file(self, source: str | Path) -> BlobInfo:
        """Store a stable snapshot of a regular file.

        The source is read twice.  The first pass establishes its digest and
        destination; the second feeds an owned destination-directory stage.
        Identity and metadata checks around both passes reject a source that is
        replaced or modified while it is being stored.
        """

        source_path = Path(source)
        digest, byte_size, source_signature = _hash_stable_file(source_path)
        chunks = _stable_file_chunks(
            source_path,
            expected_signature=source_signature,
            expected_digest=digest,
            expected_size=byte_size,
        )
        return self._publish_object(digest, byte_size, chunks)

    def has(self, digest: str) -> bool:
        """Return whether a regular object exists for *digest*.

        This is an existence check, not a full-content read.  Use :meth:`verify`
        before trusting an object at a publication boundary.
        """

        try:
            handle = self.open(digest)
        except FileNotFoundError:
            return False
        handle.close()
        return True

    def open(self, digest: str) -> BinaryIO:
        """Open a CAS object for binary reading without following symlinks."""

        digest = _validate_digest(digest)
        with self._open_shard(digest) as (shard_descriptor, shard_path):
            return _open_regular_at(
                shard_descriptor,
                shard_path,
                digest[2:],
                label="CAS object",
            )

    def read_bytes(self, digest: str) -> bytes:
        """Read an object and reject content that does not match its key."""

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

    def verify(self, digest: str) -> BlobInfo:
        """Fully verify an object and return its canonical metadata."""

        observed_digest, byte_size = self._consume_object(digest)
        if observed_digest != digest:
            raise StorageIntegrityError(f"CAS object digest mismatch for {digest}")
        return self._blob_info(digest, byte_size)

    def materialize(self, digest: str, destination: str | Path) -> Path:
        """Atomically copy a verified object to a regular destination path."""

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
        if self._strict_resources is None:
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

    def _consume_object(
        self,
        digest: str,
        *,
        chunks: list[bytes] | None = None,
    ) -> tuple[str, int]:
        digest = _validate_digest(digest)
        observed = hashlib.sha256()
        byte_size = 0
        with self.open(digest) as handle:
            signature = _object_signature(os.fstat(handle.fileno()))
            while block := handle.read(_COPY_BUFFER_SIZE):
                observed.update(block)
                byte_size += len(block)
                if chunks is not None:
                    chunks.append(block)
            if _object_signature(os.fstat(handle.fileno())) != signature:
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

    flags = os.O_RDONLY | os.O_NOFOLLOW
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


def _validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise StorageValidationError(
            "digest must be 64 lowercase hexadecimal characters"
        )
    return digest


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

    flags = os.O_RDONLY
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
