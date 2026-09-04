# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Minimal local content-addressed object store for the H1 experiment."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .contracts import StorageIntegrityError, StorageValidationError

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COPY_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BlobInfo:
    digest: str
    byte_size: int


def _real_directory(path: Path, *, create: bool, label: str) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StorageValidationError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageValidationError(f"{label} is not a real directory: {path}")
    return path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise StorageValidationError(
            "digest must be 64 lowercase hexadecimal characters"
        )
    return value


def _file_signature(handle: BinaryIO) -> tuple[int, int, int, int, int, int]:
    metadata = os.fstat(handle.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageValidationError("CAS source must be a regular file")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IFMT(metadata.st_mode),
    )


def _object_signature(handle: BinaryIO) -> tuple[int, int, int, int, int]:
    metadata = os.fstat(handle.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageIntegrityError("CAS object is not a regular file")
    # A concurrent deduplicated publisher may unlink another hard link, which
    # changes ctime/link-count without changing the immutable object bytes.
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IFMT(metadata.st_mode),
    )


class LocalCAS:
    """Store immutable files by SHA-256 on one trusted local filesystem.

    H1 intentionally supports only the operations used by ``IndexRepository``.
    It is not a generic storage API and does not defend against a hostile process
    running as the same user.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        requested = Path(root).expanduser()
        if requested.exists() or requested.is_symlink():
            _real_directory(requested, create=False, label="CAS root")
        self.root = requested.resolve(strict=False)
        _real_directory(self.root, create=True, label="CAS root")
        self._objects = _real_directory(
            self.root / "sha256",
            create=True,
            label="CAS object root",
        )
        self._temporary = _real_directory(
            self.root / "tmp",
            create=True,
            label="CAS temporary root",
        )
        _fsync_directory(self.root)

    def _object_path(self, digest: str) -> Path:
        digest = _validate_digest(digest)
        return self._objects / digest[:2] / digest[2:]

    def _ensure_shard(self, digest: str) -> Path:
        shard = self._objects / digest[:2]
        created = False
        try:
            shard.mkdir()
            created = True
        except FileExistsError:
            pass
        _real_directory(shard, create=False, label="CAS digest shard")
        if created:
            _fsync_directory(self._objects)
        return shard

    def put_file(self, source: str | os.PathLike[str]) -> BlobInfo:
        """Copy one stable regular file and publish it without replacement."""

        source_path = Path(source)
        try:
            source_metadata = source_path.lstat()
        except OSError as exc:
            raise StorageValidationError("CAS source is unavailable") from exc
        if not stat.S_ISREG(source_metadata.st_mode):
            raise StorageValidationError("CAS source must be a regular file")

        source_handle = source_path.open("rb")
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="put-",
                suffix=".tmp",
                dir=self._temporary,
            )
        except BaseException:
            source_handle.close()
            raise
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with source_handle, os.fdopen(descriptor, "wb") as target:
                before = _file_signature(source_handle)
                while True:
                    block = source_handle.read(_COPY_BYTES)
                    if not block:
                        break
                    target.write(block)
                    digest.update(block)
                    byte_size += len(block)
                after = _file_signature(source_handle)
                target.flush()
                os.fsync(target.fileno())
            if before != after or byte_size != before[2]:
                raise OSError(f"CAS source changed while being copied: {source_path}")

            info = BlobInfo(digest=digest.hexdigest(), byte_size=byte_size)
            shard = self._ensure_shard(info.digest)
            destination = self._object_path(info.digest)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                self.verify(info.digest, expected_size=info.byte_size)
                _fsync_directory(shard)
            else:
                _fsync_directory(shard)
            return info
        finally:
            temporary.unlink(missing_ok=True)
            _fsync_directory(self._temporary)

    def verify(self, digest: str, *, expected_size: int | None = None) -> BlobInfo:
        """Read and hash the complete object before returning its receipt."""

        digest = _validate_digest(digest)
        if expected_size is not None and (
            type(expected_size) is not int or expected_size < 0
        ):
            raise StorageValidationError("expected object size must be non-negative")
        shard = self._objects / digest[:2]
        _real_directory(shard, create=False, label="CAS digest shard")
        path = self._object_path(digest)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise StorageIntegrityError(f"CAS object is unavailable: {digest}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise StorageIntegrityError(f"CAS object is not a regular file: {digest}")

        observed = hashlib.sha256()
        byte_size = 0
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                before = _object_signature(handle)
                while True:
                    block = handle.read(_COPY_BYTES)
                    if not block:
                        break
                    observed.update(block)
                    byte_size += len(block)
                after = _object_signature(handle)
        except OSError as exc:
            raise StorageIntegrityError(f"CAS object cannot be read: {digest}") from exc
        if before != after or byte_size != before[2]:
            raise StorageIntegrityError(f"CAS object changed while reading: {digest}")
        if observed.hexdigest() != digest:
            raise StorageIntegrityError(f"CAS object digest mismatch: {digest}")
        if expected_size is not None and byte_size != expected_size:
            raise StorageIntegrityError(f"CAS object size mismatch: {digest}")
        return BlobInfo(digest=digest, byte_size=byte_size)

    def verified_path(self, digest: str, *, expected_size: int | None = None) -> Path:
        self.verify(digest, expected_size=expected_size)
        return self._object_path(digest)


__all__ = ["BlobInfo", "LocalCAS"]
