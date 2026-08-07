# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Crash-safe local content-addressed storage."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

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
    and os.unlink in os.supports_dir_fd
)
_LINK_UNSUPPORTED_ERRNOS = {
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
_PUBLISH_LOCKS = tuple(threading.Lock() for _ in range(64))


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

    On platforms with ``dir_fd``, ``O_DIRECTORY``, and ``O_NOFOLLOW`` support,
    every internal path component is opened relative to its verified parent.
    Other platforms retain explicit lstat/fstat checks, but cannot eliminate a
    concurrent directory-replacement race as completely as the anchored path.
    """

    def __init__(self, root: str | Path) -> None:
        requested_root = Path(root).expanduser()
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
        _ensure_directory(self.root)
        self._sha256_root = self.root / "sha256"
        with _open_directory_path(self.root, label="CAS root") as root_descriptor:
            with _open_or_create_child_directory(
                root_descriptor,
                self.root,
                "sha256",
                label="CAS SHA-256 root",
            ):
                pass

    def put_bytes(self, data: bytes) -> BlobInfo:
        """Store *data*, deduplicating an already-valid immutable object."""

        if not isinstance(data, bytes):
            raise TypeError("CAS payload must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        info = self._blob_info(digest, len(data))
        with self._open_shard(digest, create=True) as (
            shard_descriptor,
            shard_path,
        ):
            if self._verify_existing_at(shard_descriptor, shard_path, info) is not None:
                # A concurrent publisher may have linked this entry but not
                # yet flushed the shard.  A successful deduplicated put is a
                # durability receipt too, so it must not return before the
                # canonical directory entry is persistent.
                _fsync_directory_handle(shard_descriptor, shard_path)
                return info

            descriptor, temporary_name = _create_temporary_file(
                shard_descriptor,
                shard_path,
                digest[2:],
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._publish_temporary_at(
                    shard_descriptor,
                    shard_path,
                    temporary_name,
                    info,
                )
                return info
            finally:
                _unlink_at(shard_descriptor, shard_path, temporary_name)
                _fsync_directory_handle(shard_descriptor, shard_path)

    def put_file(self, source: str | Path) -> BlobInfo:
        """Store a stable snapshot of a regular file.

        The source is read twice.  The first pass establishes its digest and
        destination; the second writes the destination-directory temporary.
        Identity and metadata checks around both passes reject a source that is
        replaced or modified while it is being stored.
        """

        source_path = Path(source)
        digest, byte_size, source_signature = _hash_stable_file(source_path)
        info = self._blob_info(digest, byte_size)
        with self._open_shard(digest, create=True) as (
            shard_descriptor,
            shard_path,
        ):
            if self._verify_existing_at(shard_descriptor, shard_path, info) is not None:
                _fsync_directory_handle(shard_descriptor, shard_path)
                return info

            descriptor, temporary_name = _create_temporary_file(
                shard_descriptor,
                shard_path,
                digest[2:],
            )
            try:
                with os.fdopen(descriptor, "wb") as target:
                    copied_digest, copied_size = _copy_stable_file(
                        source_path,
                        target,
                        expected_signature=source_signature,
                    )
                    target.flush()
                    os.fsync(target.fileno())
                if copied_digest != digest or copied_size != byte_size:
                    raise OSError(f"source changed while being stored: {source_path}")
                self._publish_temporary_at(
                    shard_descriptor,
                    shard_path,
                    temporary_name,
                    info,
                )
                return info
            finally:
                _unlink_at(shard_descriptor, shard_path, temporary_name)
                _fsync_directory_handle(shard_descriptor, shard_path)

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
        with _open_directory_path(self.root, label="CAS root") as root_descriptor:
            with _open_child_directory(
                root_descriptor,
                self.root,
                "sha256",
                label="CAS SHA-256 root",
            ) as (sha256_descriptor, sha256_path):
                open_child = (
                    _open_or_create_child_directory if create else _open_child_directory
                )
                with open_child(
                    sha256_descriptor,
                    sha256_path,
                    digest[:2],
                    label="CAS digest shard",
                ) as shard:
                    yield shard

    @staticmethod
    def _blob_info(digest: str, byte_size: int) -> BlobInfo:
        digest = _validate_digest(digest)
        return BlobInfo(
            digest=digest,
            byte_size=byte_size,
            storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
        )

    def _verify_existing_at(
        self,
        shard_descriptor: int | None,
        shard_path: Path,
        expected: BlobInfo,
    ) -> BlobInfo | None:
        path = shard_path / expected.digest[2:]
        try:
            with _open_regular_at(
                shard_descriptor,
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

    def _publish_temporary_at(
        self,
        shard_descriptor: int | None,
        shard_path: Path,
        temporary_name: str,
        info: BlobInfo,
    ) -> None:
        destination_name = info.digest[2:]
        # Another writer may have completed while this writer populated its
        # temporary.  A valid winner is reused; an invalid one is never healed
        # silently because that could hide corruption of a published object.
        if self._verify_existing_at(shard_descriptor, shard_path, info) is not None:
            return

        try:
            _link_at(
                shard_descriptor,
                shard_path,
                temporary_name,
                destination_name,
            )
            return
        except FileExistsError:
            # A concurrent publisher won the no-replace link operation.
            if self._verify_existing_at(shard_descriptor, shard_path, info) is None:
                raise StorageIntegrityError(
                    f"CAS object disappeared during publication: {info.digest}"
                ) from None
            return
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise

        # Hard links may be unavailable on some filesystems.  Serialize local
        # writers before the atomic-replace fallback; cross-process writers can
        # still race here, but they can only publish identical verified bytes.
        lock = _PUBLISH_LOCKS[int(info.digest[:2], 16) % len(_PUBLISH_LOCKS)]
        with lock:
            if self._verify_existing_at(shard_descriptor, shard_path, info) is not None:
                return
            _replace_at(
                shard_descriptor,
                shard_path,
                temporary_name,
                destination_name,
            )

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


@contextmanager
def _open_directory_path(path: Path, *, label: str) -> Iterator[int | None]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageIntegrityError(f"{label} is not a real directory: {path}")

    if not _SAFE_DIRECTORY_FDS:
        # The visible component is still rejected when it is a link.  Without
        # anchored directory descriptors, however, another process can replace
        # it between this check and a later child operation.
        yield None
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise StorageIntegrityError(f"{label} is not a directory: {path}")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise StorageIntegrityError(f"{label} changed while opening: {path}")
        yield descriptor
    finally:
        os.close(descriptor)


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

    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageIntegrityError(f"{label} is not a real directory: {path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise StorageIntegrityError(f"{label} is not a directory: {path}")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise StorageIntegrityError(f"{label} changed while opening: {path}")
        yield descriptor, path
    finally:
        os.close(descriptor)


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


def _create_temporary_file(
    directory_descriptor: int | None,
    directory_path: Path,
    object_name: str,
) -> tuple[int, str]:
    if directory_descriptor is None:
        descriptor, temporary_path = tempfile.mkstemp(
            dir=str(directory_path),
            prefix=f".{object_name}.",
            suffix=".tmp",
        )
        return descriptor, Path(temporary_path).name

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    for _attempt in range(100):
        name = f".{object_name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise FileExistsError(f"could not allocate CAS temporary in {directory_path}")


def _link_at(
    directory_descriptor: int | None,
    directory_path: Path,
    source_name: str,
    destination_name: str,
) -> None:
    if directory_descriptor is not None and os.link in os.supports_dir_fd:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        return
    os.link(directory_path / source_name, directory_path / destination_name)


def _replace_at(
    directory_descriptor: int | None,
    directory_path: Path,
    source_name: str,
    destination_name: str,
) -> None:
    if directory_descriptor is not None:
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        return
    os.replace(directory_path / source_name, directory_path / destination_name)


def _unlink_at(
    directory_descriptor: int | None,
    directory_path: Path,
    name: str,
) -> None:
    try:
        if directory_descriptor is None:
            (directory_path / name).unlink()
        else:
            os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        pass


def _validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise StorageValidationError(
            "digest must be 64 lowercase hexadecimal characters"
        )
    return digest


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


def _copy_stable_file(
    path: Path,
    target: BinaryIO,
    *,
    expected_signature: tuple[int, ...],
) -> tuple[str, int]:
    observed = hashlib.sha256()
    byte_size = 0
    with _open_regular_file(path, label="CAS source") as source:
        signature = _file_signature(os.fstat(source.fileno()))
        if signature != expected_signature:
            raise OSError(f"source changed before it could be stored: {path}")
        while block := source.read(_COPY_BUFFER_SIZE):
            observed.update(block)
            byte_size += len(block)
            target.write(block)
        if _file_signature(os.fstat(source.fileno())) != signature:
            raise OSError(f"source changed while being stored: {path}")
    _require_path_signature(path, signature, label="CAS source")
    return observed.hexdigest(), byte_size


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
