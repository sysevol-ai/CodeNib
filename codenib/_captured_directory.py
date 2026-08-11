# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Descriptor-bound reads and writes for already-captured directory trees."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from ._atomic_directory import (
    TreeFileRecord,
    capture_directory_ownership,
    directory_ownership_entry_identities,
    directory_ownership_file_records,
    directory_ownership_root_identity,
    directory_ownership_root_version_identity,
    discard_owned_directory,
    lexical_directory_path,
    publish_staged_directory,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_RELATIVE_PATH_BYTES = 4_096
_MAX_COMPONENTS = 256
_COPY_BYTES = 1024 * 1024


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


def _root_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _captured_version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        getattr(metadata, "st_file_attributes", 0),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _relative_path(value: str | Path | PurePosixPath) -> PurePosixPath:
    raw = value.as_posix() if isinstance(value, (Path, PurePosixPath)) else value
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError("captured directory path must be relative POSIX")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
        or len(relative.parts) > _MAX_COMPONENTS
        or len(os.fsencode(raw)) > _MAX_RELATIVE_PATH_BYTES
    ):
        raise ValueError("captured directory path must be normalized and bounded")
    return relative


class AuthenticatedFile:
    """One opened file whose bytes must match the initial tree record."""

    def __init__(
        self,
        descriptor: int,
        opened: os.stat_result,
        record: TreeFileRecord,
    ) -> None:
        self.descriptor = descriptor
        self.opened = opened
        self.record = record
        self._hasher = hashlib.sha256()
        self._consumed = 0
        self._authenticated = False
        self._closed = False

    @property
    def size(self) -> int:
        return self.record.size

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("authenticated file is closed")
        remaining = self.record.size - self._consumed
        if size is None or size < 0:
            requested = remaining + 1
        else:
            requested = min(size, remaining + 1)
        if requested == 0:
            return b""
        try:
            block = os.read(self.descriptor, requested)
        except OSError as exc:
            raise ValueError(
                f"captured file is not readable: {self.record.path}"
            ) from exc
        if self._consumed + len(block) > self.record.size:
            raise ValueError(f"captured file grew while reading: {self.record.path}")
        self._hasher.update(block)
        self._consumed += len(block)
        return block

    def authenticate(self) -> None:
        if self._authenticated:
            return
        while self._consumed < self.record.size:
            block = self.read(min(_COPY_BYTES, self.record.size - self._consumed))
            if not block:
                raise ValueError(f"captured file was truncated: {self.record.path}")
        try:
            if os.read(self.descriptor, 1):
                raise ValueError(
                    f"captured file grew while reading: {self.record.path}"
                )
            after = os.fstat(self.descriptor)
        except OSError as exc:
            raise ValueError(
                f"captured file changed while reading: {self.record.path}"
            ) from exc
        if (
            _file_identity(after) != _file_identity(self.opened)
            or self._hasher.hexdigest() != self.record.sha256
        ):
            raise ValueError(
                f"captured file differs from its initial record: {self.record.path}"
            )
        self._authenticated = True

    def rewind_authenticated(self) -> int:
        """Authenticate the bytes, rewind the same fd, and return that fd."""

        self.authenticate()
        try:
            os.lseek(self.descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise ValueError(
                f"captured file cannot be rewound: {self.record.path}"
            ) from exc
        return self.descriptor

    def verify_unchanged(self) -> None:
        self.authenticate()
        try:
            after = os.fstat(self.descriptor)
        except OSError as exc:
            raise ValueError(
                f"captured file changed after authentication: {self.record.path}"
            ) from exc
        if _file_identity(after) != _file_identity(self.opened):
            raise ValueError(
                f"captured file changed after authentication: {self.record.path}"
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.descriptor)
        except OSError:
            pass

    def __enter__(self) -> AuthenticatedFile:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.authenticate()
        finally:
            self.close()


class CapturedDirectoryReader:
    """Read a fixed ownership token through one pinned no-follow root fd."""

    def __init__(self, root: Path, ownership: object) -> None:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError(
                "captured directory reads require no-follow directory descriptors"
            )
        self.root = lexical_directory_path(root)
        self.ownership = ownership
        self._descriptor = -1
        self._records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)  # type: ignore[arg-type]
        }
        self._entry_identities = {
            path: (kind, identity)
            for path, kind, identity in directory_ownership_entry_identities(
                ownership  # type: ignore[arg-type]
            )
        }
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            self._descriptor = os.open(self.root, flags)
            opened = os.fstat(self._descriptor)
            if _root_identity(opened) != directory_ownership_root_identity(
                ownership
            ) or _captured_version_identity(
                opened
            ) != directory_ownership_root_version_identity(
                ownership
            ):
                raise RuntimeError("captured directory root changed after capture")
            self._opened_root = opened
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1

    def __enter__(self) -> CapturedDirectoryReader:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def file_records(self) -> tuple[TreeFileRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda record: record.path))

    def record(self, relative: str | Path | PurePosixPath) -> TreeFileRecord:
        normalized = _relative_path(relative).as_posix()
        try:
            return self._records[normalized]
        except KeyError as exc:
            raise ValueError(
                f"captured directory has no initial file record: {normalized}"
            ) from exc

    def verify_root(self) -> None:
        try:
            opened = os.fstat(self._descriptor)
            path_metadata = self.root.lstat()
        except OSError as exc:
            raise RuntimeError("captured directory root changed") from exc
        if _file_identity(opened) != _file_identity(
            self._opened_root
        ) or _root_identity(path_metadata) != _root_identity(opened):
            raise RuntimeError("captured directory root changed")

    def _open(self, relative: PurePosixPath) -> tuple[int, os.stat_result]:
        record = self.record(relative)
        directory_descriptor = os.dup(self._descriptor)
        source_descriptor = -1
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(
                os, "O_NONBLOCK", 0
            )
            prefix: list[str] = []
            for part in relative.parts[:-1]:
                prefix.append(part)
                prefix_text = "/".join(prefix)
                expected_kind, expected_identity = self._entry_identities.get(
                    prefix_text,
                    (None, None),
                )
                before = os.stat(
                    part,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    expected_kind != "directory"
                    or expected_identity is None
                    or _captured_version_identity(before) != expected_identity
                    or not stat.S_ISDIR(before.st_mode)
                    or bool(
                        getattr(before, "st_file_attributes", 0)
                        & _FILE_ATTRIBUTE_REPARSE_POINT
                    )
                ):
                    raise ValueError(
                        f"captured path is not a real directory: {relative}"
                    )
                child = -1
                try:
                    child = os.open(
                        part,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                    opened = os.fstat(child)
                    if _root_identity(opened) != _root_identity(before):
                        raise ValueError(
                            f"captured directory changed while opening: {relative}"
                        )
                except BaseException:
                    if child >= 0:
                        os.close(child)
                    raise
                os.close(directory_descriptor)
                directory_descriptor = child

            name = relative.parts[-1]
            expected_kind, expected_identity = self._entry_identities.get(
                relative.as_posix(),
                (None, None),
            )
            before = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or expected_kind != "file"
                or expected_identity is None
                or _captured_version_identity(before) != expected_identity
                or before.st_nlink != 1
                or bool(
                    getattr(before, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT
                )
            ):
                raise ValueError(f"captured path is not a private file: {relative}")
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            source_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            opened = os.fstat(source_descriptor)
            if (
                _file_identity(opened) != _file_identity(before)
                or stat.S_IMODE(opened.st_mode) != record.mode
                or opened.st_size != record.size
            ):
                raise ValueError(f"captured file changed while opening: {relative}")
            owned = source_descriptor
            source_descriptor = -1
            return owned, opened
        except OSError as exc:
            raise ValueError(
                f"captured file is not safely readable: {relative}"
            ) from exc
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            os.close(directory_descriptor)

    def open_file(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> AuthenticatedFile:
        normalized = _relative_path(relative)
        record = self.record(normalized)
        if max_bytes is not None and record.size > max_bytes:
            raise ValueError(
                f"captured file exceeds its {max_bytes}-byte limit: {normalized}"
            )
        descriptor, opened = self._open(normalized)
        return AuthenticatedFile(descriptor, opened, record)

    def read_bytes(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        with self.open_file(relative, max_bytes=max_bytes) as source:
            payload = bytearray()
            while block := source.read(_COPY_BYTES):
                payload.extend(block)
            return bytes(payload)

    def authenticate(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> TreeFileRecord:
        with self.open_file(relative, max_bytes=max_bytes) as source:
            source.authenticate()
            return source.record

    @contextmanager
    def authenticated_descriptor(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[tuple[int, TreeFileRecord]]:
        source = self.open_file(relative, max_bytes=max_bytes)
        try:
            descriptor = source.rewind_authenticated()
            yield descriptor, source.record
            source.verify_unchanged()
        finally:
            source.close()

    def copy_chunks(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[bytes]:
        source = self.open_file(relative, max_bytes=max_bytes)
        try:
            while block := source.read(_COPY_BYTES):
                yield block
            source.authenticate()
        finally:
            source.close()


class OwnedDirectoryStage:
    """Build one new private sibling tree without path-based file mutation."""

    def __init__(self, destination: Path) -> None:
        self.destination = lexical_directory_path(destination)
        self.path = lexical_directory_path(
            Path(
                tempfile.mkdtemp(
                    prefix=f".{self.destination.name}.normalize-",
                    dir=str(self.destination.parent),
                )
            )
        )
        self._initial = capture_directory_ownership(self.path)
        self._cleanup_ownership = self._initial
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        self._descriptor = os.open(self.path, flags)
        self._published = False

    def close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1

    def discard(self) -> None:
        if not self._published:
            try:
                self._cleanup_ownership = self._capture_current_ownership()
            except (OSError, RuntimeError, ValueError):
                pass
        self.close()
        if not self._published:
            discard_owned_directory(self.path, self._cleanup_ownership)

    def _capture_current_ownership(self):
        observed = capture_directory_ownership(self.path)
        if directory_ownership_root_identity(observed) != (
            directory_ownership_root_identity(self._initial)
        ):
            raise RuntimeError("owned stage root changed")
        return observed

    def _parent_descriptor(self, relative: PurePosixPath) -> int:
        descriptor = os.dup(self._descriptor)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = -1
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                    opened = os.fstat(child)
                    if not stat.S_ISDIR(opened.st_mode):
                        raise ValueError(
                            f"owned stage component is not a directory: {relative}"
                        )
                except BaseException:
                    if child >= 0:
                        os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def write_file(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
        *,
        mode: int = 0o600,
        max_bytes: int | None = None,
    ) -> None:
        normalized = _relative_path(relative)
        parent = self._parent_descriptor(normalized)
        temporary = f".{normalized.name}.tmp-{secrets.token_hex(12)}"
        descriptor = -1
        renamed = False
        byte_count = 0
        iterator = iter(chunks)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(temporary, flags, mode, dir_fd=parent)
            for chunk in iterator:
                if not isinstance(chunk, bytes):
                    raise TypeError("owned stage file chunks must be bytes")
                byte_count += len(chunk)
                if max_bytes is not None and byte_count > max_bytes:
                    raise ValueError(
                        f"owned stage file exceeds its {max_bytes}-byte limit: "
                        f"{normalized}"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("could not write owned stage file")
                    view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.stat(
                    normalized.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise ValueError(f"owned stage file already exists: {normalized}")
            os.rename(
                temporary,
                normalized.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            renamed = True
            os.fsync(parent)
        finally:
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                close_iterator()
            if descriptor >= 0:
                os.close(descriptor)
            if not renamed:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass
            os.close(parent)

    def publish(
        self,
        *,
        expected_destination_ownership: object,
        validate_staged_directory=None,
        validate_published_destination=None,
    ) -> None:
        self._cleanup_ownership = self._capture_current_ownership()
        self.close()
        publish_staged_directory(
            self.path,
            self.destination,
            expected_stage_root_ownership=self._cleanup_ownership,
            expected_destination_ownership=expected_destination_ownership,
            validate_staged_directory=validate_staged_directory,
            validate_published_destination=validate_published_destination,
        )
        self._published = True


__all__ = [
    "AuthenticatedFile",
    "CapturedDirectoryReader",
    "OwnedDirectoryStage",
]
