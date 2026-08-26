# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Normalize owned query views into portable, query-only artifacts."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping

from .. import compat_pickle
from .._atomic_directory import (
    PublicationAuthenticatedFile,
    PublicationDirectoryReader,
    TreeFileRecord,
    _annotate_secondary_error,
    _run_callback_with_post_validations,
    capture_directory_ownership,
    directory_ownership_file_records,
    directory_ownership_inventory,
    directory_ownership_root_identity,
)
from .._bounded_json import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_KEY_BYTES,
    DEFAULT_MAX_NODES_PER_ELEMENT,
    canonical_json_value_chunks,
    iter_bounded_json_array,
    validate_bounded_json_stream,
    validate_json_complexity,
)
from .._contained_source import _SourceCleanupGroup, validate_repository_file
from .._secret_fields import assert_no_secret_fields
from ..index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_ROW_MAPPING_CONTRACT,
    VECTOR_VIEW_UPDATE_MARKER,
)
from ..index.embedding.model_policy import (
    resolve_embedding_artifact_load_policy_from_options,
    resolve_embedding_load_policy_from_options,
)
from ..native_index_authorization import (
    MissingNativeIndexAuthorizationError,
    NativeIndexAuthorization,
    require_native_index_authorization,
    require_native_index_authorization_preflight,
)
from ..provider_routes import normalize_provider, resolve_embedding_artifact_route
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceFileRecord,
    RepositorySourceIdentitySnapshot,
    is_secure_source_fingerprint_v2,
)
from ..storage.models import StorageIntegrityError
from ..storage.protocols import snapshot_retained_import_response
from .security import (
    _contains_pattern,
    _interitem_cancellation,
    _interruptible_sorted_security_items,
    assert_no_credential_fields,
    assert_publishable_json_value,
)

_VECTOR_LEVELS = ("l0", "l2")
_REMOVABLE_MUTABLE_VECTOR_FILES = frozenset(
    {
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    }
)
_MUTABLE_VECTOR_PREFIXES = (
    "chunk_store.",
    "embeddings_cache.",
    "incremental_state.",
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_PICKLE_SUFFIXES = frozenset({".pkl", ".pickle"})
_WINDOWS_REPARSE_POINT = 0x400
_MAX_CONFIG_JSON_BYTES = 16 * 1024 * 1024
MAX_PORTABLE_DOCUMENTS_JSON_BYTES = 256 * 1024 * 1024
MAX_PORTABLE_FAISS_INDEX_BYTES = 8 * 1024 * 1024 * 1024
_JSON_READ_CHUNK_BYTES = 1024 * 1024
_MAX_SOURCE_PATH_BYTES = 4_096
_MAX_SOURCE_PATH_COMPONENTS = 256
_SEMANTIC_SCAN_CHARS = 64 * 1024


class _CallbackIterationStop(BaseException):
    """Carry iteration-sentinel callback failures across generator boundaries."""

    def __init__(self, error: StopIteration | StopAsyncIteration) -> None:
        super().__init__(error)
        self.error = error


def _transfer_callback_exception_settlement(
    source: BaseException,
    target: BaseException,
) -> None:
    """Move cleanup observability from an internal carrier to the exact stop."""

    try:
        notes = BaseException.__getattribute__(source, "__notes__")
    except AttributeError:
        notes = ()
    if type(notes) is list:
        add_note = getattr(BaseException, "add_note", None)
        if add_note is not None:
            for note in notes:
                if type(note) is str:
                    try:
                        add_note(target, note)
                    except BaseException:  # noqa: B036 - diagnostics only
                        break
    try:
        fallback_notes = BaseException.__getattribute__(
            source,
            "_codenib_cleanup_notes",
        )
    except AttributeError:
        fallback_notes = ()
    if type(fallback_notes) is tuple:
        try:
            existing_notes = BaseException.__getattribute__(
                target,
                "_codenib_cleanup_notes",
            )
        except AttributeError:
            existing_notes = ()
        if type(existing_notes) is not tuple:
            existing_notes = ()
        try:
            BaseException.__setattr__(
                target,
                "_codenib_cleanup_notes",
                (*existing_notes, *fallback_notes),
            )
        except BaseException:  # noqa: B036 - diagnostics only
            pass
    try:
        owners = BaseException.__getattribute__(source, "publication_cleanup_owners")
    except AttributeError:
        owners = ()
    if type(owners) is tuple:
        try:
            existing_owners = BaseException.__getattribute__(
                target,
                "publication_cleanup_owners",
            )
        except AttributeError:
            existing_owners = ()
        if type(existing_owners) is not tuple:
            existing_owners = ()
        try:
            BaseException.__setattr__(
                target,
                "publication_cleanup_owners",
                (*existing_owners, *owners),
            )
        except BaseException:  # noqa: B036 - diagnostics only
            pass
    try:
        source_owner = BaseException.__getattribute__(source, "source_cleanup_owner")
    except AttributeError:
        source_owner = None
    if source_owner is not None:
        try:
            existing_owner = BaseException.__getattribute__(
                target,
                "source_cleanup_owner",
            )
        except AttributeError:
            existing_owner = None
        if existing_owner is None:
            try:
                BaseException.__setattr__(
                    target,
                    "source_cleanup_owner",
                    source_owner,
                )
            except BaseException:  # noqa: B036 - diagnostics only
                pass
        elif existing_owner is not source_owner:
            try:
                merged_owner = _SourceCleanupGroup(existing_owner, source_owner)
                BaseException.__setattr__(
                    target,
                    "source_cleanup_owner",
                    merged_owner,
                )
            except BaseException:  # noqa: B036 - exact callback stays primary
                pass


SourceTrust = Literal["portable-inert", "trusted-local"]


class _InterruptibleReader:
    __slots__ = ("_source", "_check_cancelled", "_remaining")

    def __init__(
        self,
        source: PublicationAuthenticatedFile,
        check_cancelled: Callable[[], None],
    ) -> None:
        self._source = source
        self._check_cancelled = check_cancelled
        self._remaining = source.size

    def read(self, size: int = -1) -> bytes:
        if self._remaining > 0:
            self._check_cancelled()
        payload = self._source.read(size)
        self._remaining = max(0, self._remaining - len(payload))
        return payload


def _view_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _view_binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Identify one directory entry without cache-sensitive timestamps."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


def _view_directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


class _OwnedViewReader:
    """Read one captured view through a pinned no-follow root descriptor."""

    def __init__(self, root: Path, ownership: object) -> None:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError(
                "portable view validation requires no-follow directory descriptors"
            )
        self.root = root
        self.ownership = ownership
        self._descriptor = -1
        self._cached_payloads: dict[PurePosixPath, bytearray] = {}
        self._authenticated_files: dict[PurePosixPath, tuple[int, os.stat_result]] = {}
        self._replacement_targets: dict[PurePosixPath, tuple[int, os.stat_result]] = {}
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            self._descriptor = os.open(root, flags)
            opened = os.fstat(self._descriptor)
            root_identity = (
                opened.st_dev,
                opened.st_ino,
                stat.S_IFMT(opened.st_mode),
                getattr(opened, "st_file_attributes", 0),
            )
            if root_identity != directory_ownership_root_identity(ownership):
                raise RuntimeError("portable view root changed after capture")
            self._root_identity = _view_file_identity(opened)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for descriptor, _metadata in self._authenticated_files.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._authenticated_files.clear()
        for descriptor, _metadata in self._replacement_targets.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._replacement_targets.clear()
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1

    def verify_root(self) -> None:
        try:
            current = os.fstat(self._descriptor)
            path_current = self.root.lstat()
        except OSError as exc:
            raise RuntimeError("portable view root changed during validation") from exc
        if _view_file_identity(current) != self._root_identity or (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
            getattr(current, "st_file_attributes", 0),
        ) != (
            path_current.st_dev,
            path_current.st_ino,
            stat.S_IFMT(path_current.st_mode),
            getattr(path_current, "st_file_attributes", 0),
        ):
            raise RuntimeError("portable view root changed during validation")

    @staticmethod
    def _relative(value: Path) -> PurePosixPath:
        relative = PurePosixPath(value.as_posix())
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or len(relative.parts) > 256
        ):
            raise ValueError(f"portable view path is invalid: {value}")
        return relative

    def _open_parent(self, relative: PurePosixPath) -> int:
        directory_descriptor = os.dup(self._descriptor)
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            for part in relative.parts[:-1]:
                before = os.stat(
                    part,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(before.st_mode) or bool(
                    getattr(before, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
                ):
                    raise ValueError(
                        f"portable view path is not a real directory: {relative}"
                    )
                child = os.open(part, directory_flags, dir_fd=directory_descriptor)
                opened = os.fstat(child)
                if (
                    opened.st_dev != before.st_dev
                    or opened.st_ino != before.st_ino
                    or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(before.st_mode)
                ):
                    os.close(child)
                    raise ValueError(f"portable view directory changed: {relative}")
                os.close(directory_descriptor)
                directory_descriptor = child
            owned = directory_descriptor
            directory_descriptor = -1
            return owned
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

    @staticmethod
    def _open_private_file(
        directory_descriptor: int,
        relative: PurePosixPath,
    ) -> tuple[int, os.stat_result]:
        source_descriptor = -1
        try:
            name = relative.parts[-1]
            before = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or bool(
                    getattr(before, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
                )
            ):
                raise ValueError(
                    f"portable view path is not a private file: {relative}"
                )
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            source_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            opened = os.fstat(source_descriptor)
            if _view_file_identity(opened) != _view_file_identity(before):
                raise ValueError(
                    f"portable view file changed while opening: {relative}"
                )
            owned = source_descriptor
            source_descriptor = -1
            return owned, opened
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)

    def _open_file(self, relative: PurePosixPath) -> tuple[int, os.stat_result]:
        replacement_target = self._replacement_targets.get(relative)
        directory_descriptor = -1
        source_descriptor = -1
        try:
            directory_descriptor = (
                os.dup(replacement_target[0])
                if replacement_target is not None
                else self._open_parent(relative)
            )
            source_descriptor, opened = self._open_private_file(
                directory_descriptor,
                relative,
            )
            if replacement_target is not None and _view_file_identity(
                opened
            ) != _view_file_identity(replacement_target[1]):
                raise ValueError(
                    f"portable view replacement target changed: {relative}"
                )

            owned = source_descriptor
            source_descriptor = -1
            return owned, opened
        except OSError as exc:
            raise ValueError(
                f"portable view file is not safely readable: {relative}"
            ) from exc
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

    def pin_replacement_target(self, path: Path) -> None:
        """Pin a target's parent and identity before validating its contents."""

        relative = self._relative(path.relative_to(self.root))
        if relative in self._replacement_targets:
            return
        directory_descriptor = -1
        source_descriptor = -1
        try:
            directory_descriptor = self._open_parent(relative)
            source_descriptor, opened = self._open_private_file(
                directory_descriptor,
                relative,
            )
            self._replacement_targets[relative] = (directory_descriptor, opened)
            directory_descriptor = -1
        except OSError as exc:
            raise ValueError(
                f"portable view replacement target is not safely writable: {relative}"
            ) from exc
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

    def _verify_replacement_parent(
        self,
        relative: PurePosixPath,
        pinned_descriptor: int,
    ) -> None:
        current_descriptor = -1
        try:
            current_descriptor = self._open_parent(relative)
            if _view_directory_identity(
                os.fstat(current_descriptor)
            ) != _view_directory_identity(os.fstat(pinned_descriptor)):
                raise RuntimeError(
                    f"portable view replacement parent changed: {relative.parent}"
                )
        except OSError as exc:
            raise RuntimeError(
                f"portable view replacement parent changed: {relative.parent}"
            ) from exc
        finally:
            if current_descriptor >= 0:
                os.close(current_descriptor)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("portable view temporary file write made no progress")
            written += count

    @staticmethod
    def _remove_owned_temporary(
        directory_descriptor: int,
        name: str,
        descriptor: int,
    ) -> None:
        try:
            observed = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _view_binding_identity(observed) != _view_binding_identity(opened)
        ):
            raise RuntimeError(
                "portable view canonicalization temporary file changed before cleanup"
            )
        os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)

    def replace_bytes(self, path: Path, payload: bytes) -> None:
        """Atomically replace a validated file through its pinned parent."""

        relative = self._relative(path.relative_to(self.root))
        replacement_target = self._replacement_targets.get(relative)
        if replacement_target is None:
            raise RuntimeError(
                f"portable view replacement target is not pinned: {relative}"
            )
        directory_descriptor, expected = replacement_target
        temporary_descriptor = -1
        temporary_name: str | None = None
        replaced = False
        try:
            self.verify_root()
            self._verify_replacement_parent(relative, directory_descriptor)
            current = os.stat(
                relative.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _view_file_identity(current) != _view_file_identity(expected):
                raise RuntimeError(
                    f"portable view replacement target changed: {relative}"
                )

            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            for _attempt in range(128):
                candidate = (
                    f".codenib-canonical-{os.getpid()}-" f"{os.urandom(16).hex()}.tmp"
                )
                try:
                    temporary_descriptor = os.open(
                        candidate,
                        flags,
                        stat.S_IMODE(expected.st_mode),
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_name is None:
                raise RuntimeError(
                    "portable view canonicalization could not reserve a temporary file"
                )

            created = os.fstat(temporary_descriptor)
            observed_created = os.stat(
                temporary_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(created.st_mode)
                or created.st_nlink != 1
                or _view_binding_identity(observed_created)
                != _view_binding_identity(created)
            ):
                raise RuntimeError(
                    "portable view canonicalization temporary file is not private"
                )
            self._write_all(temporary_descriptor, payload)
            os.fchmod(temporary_descriptor, stat.S_IMODE(expected.st_mode))
            os.fsync(temporary_descriptor)
            temporary_metadata = os.fstat(temporary_descriptor)
            observed_temporary = os.stat(
                temporary_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(temporary_metadata.st_mode)
                or temporary_metadata.st_nlink != 1
                or _view_binding_identity(observed_temporary)
                != _view_binding_identity(temporary_metadata)
            ):
                raise RuntimeError(
                    "portable view canonicalization temporary file changed"
                )

            current = os.stat(
                relative.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _view_file_identity(current) != _view_file_identity(expected):
                raise RuntimeError(
                    f"portable view replacement target changed: {relative}"
                )
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            replaced = True
            installed = os.stat(
                relative.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            installed_descriptor = os.fstat(temporary_descriptor)
            if _view_binding_identity(installed) != _view_binding_identity(
                temporary_metadata
            ) or _view_binding_identity(installed) != _view_binding_identity(
                installed_descriptor
            ):
                raise RuntimeError(
                    f"portable view canonical replacement changed: {relative}"
                )
            os.lseek(temporary_descriptor, 0, os.SEEK_SET)
            installed_payload = self._read_exact(
                temporary_descriptor,
                installed_descriptor,
                relative=relative,
                max_bytes=len(payload),
            )
            installed_after_read = os.stat(
                relative.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            descriptor_after_read = os.fstat(temporary_descriptor)
            if (
                installed_payload != payload
                or _view_file_identity(installed_after_read)
                != _view_file_identity(installed_descriptor)
                or _view_file_identity(descriptor_after_read)
                != _view_file_identity(installed_descriptor)
            ):
                raise RuntimeError(
                    f"portable view canonical replacement changed: {relative}"
                )
            os.fsync(directory_descriptor)
            self._verify_replacement_parent(relative, directory_descriptor)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"portable view could not canonicalize safely: {relative}"
            ) from exc
        finally:
            cleanup_error: Exception | None = None
            if (
                temporary_name is not None
                and temporary_descriptor >= 0
                and not replaced
            ):
                try:
                    self._remove_owned_temporary(
                        directory_descriptor,
                        temporary_name,
                        temporary_descriptor,
                    )
                except Exception as exc:
                    cleanup_error = exc
            if temporary_descriptor >= 0:
                try:
                    os.close(temporary_descriptor)
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None and sys.exc_info()[1] is None:
                raise RuntimeError(
                    "portable view canonicalization temporary cleanup failed closed"
                ) from cleanup_error

    @staticmethod
    def _read_exact(
        descriptor: int,
        opened: os.stat_result,
        *,
        relative: PurePosixPath,
        max_bytes: int | None,
    ) -> bytearray:
        if max_bytes is not None and opened.st_size > max_bytes:
            raise ValueError(
                f"portable view file exceeds its {max_bytes}-byte limit: {relative}"
            )
        payload = bytearray()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, _JSON_READ_CHUNK_BYTES))
            if not block:
                raise ValueError(f"portable view file was truncated: {relative}")
            payload.extend(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ValueError(f"portable view file grew while being read: {relative}")
        if _view_file_identity(os.fstat(descriptor)) != _view_file_identity(opened):
            raise ValueError(f"portable view file changed while being read: {relative}")
        return payload

    def read_bytes(self, path: Path, *, max_bytes: int) -> bytearray:
        relative = self._relative(path.relative_to(self.root))
        cached = self._cached_payloads.get(relative)
        if cached is not None:
            if len(cached) > max_bytes:
                raise ValueError(
                    f"portable view file exceeds its {max_bytes}-byte limit: {relative}"
                )
            return cached
        descriptor, opened = self._open_file(relative)
        try:
            return self._read_exact(
                descriptor,
                opened,
                relative=relative,
                max_bytes=max_bytes,
            )
        except OSError as exc:
            raise ValueError(f"portable view file is not readable: {relative}") from exc
        finally:
            os.close(descriptor)

    def authenticate(
        self,
        path: Path,
        expected: object,
        *,
        cache_bytes: bool,
        max_bytes: int | None = None,
        keep_descriptor: bool = False,
    ) -> None:
        relative = self._relative(path.relative_to(self.root))
        if not isinstance(expected, Mapping):
            raise ValueError(f"portable view fingerprint is invalid: {relative}")
        expected_fields = {"size", "sha256"}
        if "file" in expected:
            expected_fields.add("file")
        if set(expected) != expected_fields:
            raise ValueError(f"portable view fingerprint is invalid: {relative}")
        if "file" in expected and expected.get("file") != relative.name:
            raise ValueError(
                f"portable view fingerprint filename is invalid: {relative}"
            )
        size = expected.get("size")
        digest = expected.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError(f"portable view fingerprint is invalid: {relative}")
        descriptor, opened = self._open_file(relative)
        try:
            if opened.st_size != size or (
                max_bytes is not None and opened.st_size > max_bytes
            ):
                raise ValueError(f"portable view fingerprint size mismatch: {relative}")
            hasher = hashlib.sha256()
            payload = bytearray() if cache_bytes else None
            remaining = opened.st_size
            while remaining:
                block = os.read(descriptor, min(remaining, _JSON_READ_CHUNK_BYTES))
                if not block:
                    raise ValueError(f"portable view file was truncated: {relative}")
                hasher.update(block)
                if payload is not None:
                    payload.extend(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise ValueError(f"portable view file grew while hashing: {relative}")
            if hasher.hexdigest() != digest or _view_file_identity(
                os.fstat(descriptor)
            ) != _view_file_identity(opened):
                raise ValueError(f"portable view fingerprint mismatch: {relative}")
            if payload is not None:
                self._cached_payloads[relative] = payload
            if keep_descriptor:
                os.lseek(descriptor, 0, os.SEEK_SET)
                previous = self._authenticated_files.pop(relative, None)
                if previous is not None:
                    os.close(previous[0])
                self._authenticated_files[relative] = (descriptor, opened)
                descriptor = -1
        except OSError as exc:
            raise ValueError(f"portable view file is not readable: {relative}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def pin_file(self, path: Path, *, max_bytes: int | None = None) -> None:
        """Retain a stable file descriptor for a later parser."""

        relative = self._relative(path.relative_to(self.root))
        if relative in self._authenticated_files:
            return
        descriptor, opened = self._open_file(relative)
        try:
            if max_bytes is not None and opened.st_size > max_bytes:
                raise ValueError(
                    f"portable view file exceeds its {max_bytes}-byte limit: "
                    f"{relative}"
                )
            self._authenticated_files[relative] = (descriptor, opened)
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def fingerprint(
        self, path: Path, *, max_bytes: int | None = None
    ) -> dict[str, Any]:
        """Hash a stable regular file through the pinned view root."""

        relative = self._relative(path.relative_to(self.root))
        descriptor, opened = self._open_file(relative)
        try:
            if max_bytes is not None and opened.st_size > max_bytes:
                raise ValueError(
                    f"portable view file exceeds its {max_bytes}-byte limit: "
                    f"{relative}"
                )
            hasher = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                block = os.read(descriptor, min(remaining, _JSON_READ_CHUNK_BYTES))
                if not block:
                    raise ValueError(f"portable view file was truncated: {relative}")
                hasher.update(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise ValueError(f"portable view file grew while hashing: {relative}")
            if _view_file_identity(os.fstat(descriptor)) != _view_file_identity(opened):
                raise ValueError(
                    f"portable view file changed while hashing: {relative}"
                )
            return {"size": opened.st_size, "sha256": hasher.hexdigest()}
        except OSError as exc:
            raise ValueError(f"portable view file is not readable: {relative}") from exc
        finally:
            os.close(descriptor)

    def faiss_index(self, path: Path, faiss: Any) -> Any:
        relative = self._relative(path.relative_to(self.root))
        authenticated = self._authenticated_files.get(relative)
        if authenticated is None:
            raise ValueError(f"portable FAISS index is not authenticated: {relative}")
        descriptor, opened = authenticated
        os.lseek(descriptor, 0, os.SEEK_SET)
        remaining = opened.st_size

        def read_bytes(requested: int) -> bytes:
            nonlocal remaining
            if requested <= 0 or remaining <= 0:
                return b""
            block = os.read(descriptor, min(requested, remaining, 8 * 1024 * 1024))
            remaining -= len(block)
            return block

        reader = faiss.PyCallbackIOReader(read_bytes)
        index = faiss.read_index(reader)
        if _view_file_identity(os.fstat(descriptor)) != _view_file_identity(opened):
            raise ValueError(f"portable FAISS index changed while parsing: {relative}")
        return index


class _PublicationViewReader:
    """Portable-view facade over one callback-scoped publication authority."""

    def __init__(
        self,
        publication: PublicationDirectoryReader,
        ownership: object | None,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        # Synthetic only: callers use it for relative path arithmetic, never as
        # a filesystem authority.
        self.root = Path("/__codenib_publication_view__")
        self._publication = publication
        self._callback_failure: BaseException | None = None
        self._callback_failure_generation = 0
        if check_cancelled is None:
            self._check_cancelled = None
        else:

            def tracked_check_cancelled() -> None:
                try:
                    check_cancelled()
                except BaseException as failure:  # noqa: B036 - exact provenance
                    self._callback_failure = failure
                    self._callback_failure_generation += 1
                    raise

            self._check_cancelled = tracked_check_cancelled
        if ownership is None:
            # Private framework-sandwiched validators reuse the exact token
            # already installed in the callback reader. Fail closed rather
            # than letting fallback accessors recapture an unsandwiched tree.
            expected = publication._require_expected_ownership_token()
            if check_cancelled is None:
                records = tuple(directory_ownership_file_records(expected))
                inventory = tuple(directory_ownership_inventory(expected))
            else:
                records = directory_ownership_file_records(expected)
                inventory = directory_ownership_inventory(expected)
            expected_ownership = expected
            self.ownership = self
        else:
            if check_cancelled is None:
                records = tuple(
                    directory_ownership_file_records(ownership)  # type: ignore[arg-type]
                )
                inventory = tuple(
                    directory_ownership_inventory(ownership)  # type: ignore[arg-type]
                )
            else:
                records = directory_ownership_file_records(ownership)  # type: ignore[arg-type]
                inventory = directory_ownership_inventory(ownership)  # type: ignore[arg-type]
            expected_ownership = ownership
            self.ownership = ownership
        self._expected_ownership = expected_ownership
        self._inventory = inventory
        record_count = len(records)
        records_by_path: dict[str, TreeFileRecord] = {}
        for index in range(record_count):
            record = records[index]
            if type(record) is not TreeFileRecord or (
                type(record.path) is not str
                or type(record.mode) is not int
                or type(record.size) is not int
                or type(record.sha256) is not str
            ):
                raise TypeError("publication view file record fields are invalid")
            if (
                not record.path
                or record.size < 0
                or not re.fullmatch(r"[0-9a-f]{64}", record.sha256, re.ASCII)
            ):
                raise ValueError("publication view file record identity is invalid")
            path = record.path
            if path in records_by_path:
                raise RuntimeError("publication view repeats a file record")
            records_by_path[path] = record
            if check_cancelled is not None and index + 1 < record_count:
                try:
                    check_cancelled()
                except BaseException:  # noqa: B036 - preserve exact stop
                    observed = publication.capture_ownership()
                    if observed != expected_ownership:
                        raise RuntimeError(
                            "publication view changed during record derivation"
                        ) from None
                    raise
        self._records = records_by_path

    def _relative_path(self, path: Path | PurePosixPath) -> PurePosixPath:
        if isinstance(path, Path):
            try:
                relative = PurePosixPath(path.relative_to(self.root).as_posix())
            except ValueError as exc:
                raise ValueError("publication view path is outside its root") from exc
        elif isinstance(path, PurePosixPath):
            relative = path
        else:  # pragma: no cover - private callers are statically constrained
            raise TypeError("publication view path must be path-like")
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or len(relative.as_posix().encode("utf-8", errors="strict"))
            > _MAX_SOURCE_PATH_BYTES
            or len(relative.parts) > _MAX_SOURCE_PATH_COMPONENTS
        ):
            raise ValueError(f"publication view path is invalid: {relative}")
        return relative

    def record(self, path: Path | PurePosixPath) -> TreeFileRecord:
        relative = self._relative_path(path)
        try:
            return self._records[relative.as_posix()]
        except KeyError as exc:
            raise ValueError(
                f"publication view has no initial file record: {relative}"
            ) from exc

    def file_records(
        self,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> tuple[TreeFileRecord, ...]:
        if check_cancelled is None:
            values = tuple(self._records.values())
            return tuple(sorted(values, key=lambda record: record.path))
        values_list: list[TreeFileRecord] = []
        record_count = len(self._records)
        for index, record in enumerate(self._records.values()):
            values_list.append(record)
            if index + 1 < record_count:
                check_cancelled()
        return _interruptible_sorted_security_items(
            values_list,
            key=lambda record: record.path,
            check_cancelled=check_cancelled,
        )

    def inventory(self) -> tuple[tuple[str, str], ...]:
        return self._inventory

    @contextmanager
    def open_file(
        self,
        path: Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[PublicationAuthenticatedFile]:
        relative = self._relative_path(path)
        expected = self.record(relative)
        limit = expected.size if max_bytes is None else max_bytes
        callback_generation = self._callback_failure_generation
        callback_failure: BaseException | None = None
        try:
            with self._publication.open_authenticated_file(
                relative,
                max_bytes=limit,
            ) as source:
                try:
                    yield source
                except BaseException as failure:  # noqa: B036 - reconcile callback
                    if not (
                        self._callback_failure_generation > callback_generation
                        and failure is self._callback_failure
                    ):
                        raise
                    # Let the inner authenticated context take its successful
                    # exit path.  It drains only raw bytes, verifies the live
                    # binding/digest, and compares the captured record before
                    # the exact callback failure is re-raised below.
                    callback_failure = failure
        except BaseException as authentication_failure:  # noqa: B036
            if callback_failure is not None:
                raise authentication_failure from callback_failure
            raise
        observed = source.record
        if observed != expected:
            mismatch = RuntimeError(
                f"publication view file differs from its initial record: {relative}"
            )
            if callback_failure is not None:
                raise mismatch from callback_failure
            raise mismatch
        if callback_failure is not None:
            raise callback_failure

    def read_bytes(self, path: Path, *, max_bytes: int) -> bytes:
        relative = self._relative_path(path)
        payload = bytearray()
        with self.open_file(relative, max_bytes=max_bytes) as source:
            for chunk in source.iter_bytes(chunk_size=_JSON_READ_CHUNK_BYTES):
                payload.extend(chunk)
                if self._check_cancelled is not None and len(payload) < source.size:
                    self._check_cancelled()
        return bytes(payload)

    def validate_json(self, path: Path, *, label: str, max_bytes: int) -> None:
        """Apply bounded lexical JSON policy before any DOM allocation."""

        record = self.record(path)
        lexical_budget = max(1, record.size)
        with self.open_file(path, max_bytes=max_bytes) as source:
            validate_bounded_json_stream(
                (
                    source
                    if self._check_cancelled is None
                    else _InterruptibleReader(source, self._check_cancelled)
                ),
                label=label,
                max_bytes=max_bytes,
                max_nodes=lexical_budget,
                max_lexical_tokens=lexical_budget,
            )

    def require_fingerprint(
        self,
        path: Path,
        expected: object,
    ) -> PurePosixPath:
        relative = self._relative_path(path)
        record = self.record(relative)
        if not isinstance(expected, Mapping):
            raise ValueError(f"portable view fingerprint is invalid: {relative}")
        expected_fields = {"size", "sha256"}
        if "file" in expected:
            expected_fields.add("file")
        if not _mapping_has_exact_keys(
            expected,
            expected_fields,
            check_cancelled=self._check_cancelled,
        ) or ("file" in expected and expected.get("file") != relative.name):
            raise ValueError(f"portable view fingerprint is invalid: {relative}")
        if expected.get("size") != record.size or expected.get("sha256") != (
            record.sha256
        ):
            raise ValueError(f"portable view fingerprint mismatch: {relative}")
        return relative

    def authenticate(
        self,
        path: Path,
        expected: object,
        *,
        cache_bytes: bool,
        max_bytes: int | None = None,
        keep_descriptor: bool = False,
    ) -> None:
        # ``keep_descriptor`` is deliberately ignored. Content-bound portable
        # validation never grants native parsing authority.
        # The complete publication was already hashed into ``ownership``.
        # Config and document consumers subsequently reopen and compare their
        # exact records; inert FAISS bytes are intentionally never reopened by
        # this validator.  ``verify_root`` performs the full post-validation
        # capture that closes the race window for every record-only binding.
        del cache_bytes, keep_descriptor
        relative = self.require_fingerprint(path, expected)
        if max_bytes is not None:
            if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
                raise TypeError("portable publication stream limit must be an integer")
            if max_bytes < 0:
                raise ValueError("portable publication stream limit cannot be negative")
            if self.record(relative).size > max_bytes:
                raise ValueError(
                    f"portable view file exceeds its {max_bytes}-byte limit: {relative}"
                )

    def verify_root(self) -> None:
        if self.ownership is self:
            raise RuntimeError(
                "framework-sandwiched publication validation owns no final capture"
            )
        check_cancelled = self._check_cancelled
        cancellation_failure: BaseException | None = None
        if check_cancelled is None:
            observed = self._publication.capture_ownership()
        else:

            def poll() -> None:
                nonlocal cancellation_failure
                try:
                    check_cancelled()
                except BaseException as exc:  # noqa: B036 - exact provenance
                    cancellation_failure = exc
                    raise

            try:
                observed = self._publication.capture_ownership(
                    check_cancelled=poll,
                )
            except BaseException as exc:  # noqa: B036 - exact stop only
                if exc is not cancellation_failure:
                    raise
                observed = self._publication.capture_ownership()
        if observed != self.ownership:
            raise RuntimeError("publication view changed during validation")
        if cancellation_failure is not None:
            raise cancellation_failure

    def close(self) -> None:
        return None

    def faiss_index(self, _path: Path, _faiss: Any) -> Any:
        raise RuntimeError(
            "content-bound portable validation keeps native indexes inert"
        )


_ViewReader = _OwnedViewReader | _PublicationViewReader


def _view_inventory(ownership: object) -> tuple[tuple[str, str], ...]:
    if isinstance(ownership, _PublicationViewReader):
        return ownership.inventory()
    return tuple(directory_ownership_inventory(ownership))  # type: ignore[arg-type]


def _view_file_records(
    ownership: object,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[TreeFileRecord, ...]:
    if isinstance(ownership, _PublicationViewReader):
        return ownership.file_records(check_cancelled=check_cancelled)
    return tuple(directory_ownership_file_records(ownership))  # type: ignore[arg-type]


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _mapping_has_exact_keys(
    value: Mapping[Any, Any],
    expected: set[str],
    *,
    check_cancelled: Callable[[], None] | None,
) -> bool:
    """Compare exact key sets while polling only before a possible future key."""

    if check_cancelled is None:
        return set(value) == expected
    if len(value) != len(expected):
        return False
    item_count = len(value)
    observed: set[Any] = set()
    for index, key in enumerate(value):
        if key not in expected or key in observed:
            return False
        observed.add(key)
        if index + 1 < item_count:
            check_cancelled()
    return len(observed) == len(expected)


def _mapping_keys_are_subset(
    value: Mapping[Any, Any],
    allowed: set[str],
    *,
    check_cancelled: Callable[[], None] | None,
) -> bool:
    if check_cancelled is None:
        return set(value) <= allowed
    for key in _interitem_cancellation(value, check_cancelled):
        if key not in allowed:
            return False
    return True


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _attest_json_value_interruptibly(
    value: Any,
    *,
    label: str,
    check_cancelled: Callable[[], None],
    active: set[int] | None = None,
    depth: int = 1,
) -> tuple[Any, int, int]:
    """Detach one JSON value and attest its subtree before polling future input.

    The returned tuple contains the exact-builtin snapshot, its decoded node
    count, and its canonical byte size at the value's current indentation
    level.  Keeping those results together matters: a cancellation check after
    a current mapping item must not leave a caller-owned nested container live,
    or defer a current complexity/serialized-size failure until after that
    check.
    """

    if depth > DEFAULT_MAX_DEPTH:
        raise ValueError(f"{label} exceeds its {DEFAULT_MAX_DEPTH}-level depth limit")
    if value is None or type(value) is bool:
        if value is None:
            return None, 1, 4
        return value, 1, 4 if value else 5
    if isinstance(value, str):
        from json.encoder import encode_basestring_ascii

        value_length = len(value)
        if value_length > _MAX_CONFIG_JSON_BYTES:
            raise ValueError(f"{label} contains an oversized JSON string")
        encoded_size = 2
        detached_parts: list[str] | None = [] if type(value) is not str else None
        for offset in range(0, value_length, _SEMANTIC_SCAN_CHARS):
            end = min(value_length, offset + _SEMANTIC_SCAN_CHARS)
            piece = str.__getitem__(value, slice(offset, end))
            encoded_size += len(encode_basestring_ascii(piece)) - 2
            if encoded_size + 1 > _MAX_CONFIG_JSON_BYTES:
                raise ValueError(f"{label} contains an oversized JSON string")
            if detached_parts is not None:
                detached_parts.append(piece)
            if end < value_length:
                check_cancelled()
        detached_text = value if detached_parts is None else "".join(detached_parts)
        return detached_text, 1, encoded_size
    if isinstance(value, int):
        detached_integer = value if type(value) is int else int.__int__(value)
        digit_limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
        if digit_limit and int.bit_length(detached_integer) > (digit_limit + 1) * 4:
            raise ValueError(f"{label} contains an oversized JSON integer")
        rendered_integer = int.__repr__(detached_integer)
        return detached_integer, 1, len(rendered_integer)
    if isinstance(value, float):
        detached_float = value if type(value) is float else float.__float__(value)
        if not math.isfinite(detached_float):
            raise ValueError(f"{label} contains a non-finite JSON number")
        return detached_float, 1, len(repr(detached_float))
    if active is None:
        active = set()
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} contains a circular JSON object")
        active.add(identity)
        node_count = 1
        detached_object: dict[str, Any] = {}
        canonical_size = 2
        copied_items = 0

        def detach_current_key(key: Any) -> str:
            if not isinstance(key, str):
                raise TypeError(f"{label} contains a non-text JSON object key")
            if str.__len__(key) > DEFAULT_MAX_KEY_BYTES:
                raise ValueError(
                    f"{label} contains a key exceeding {DEFAULT_MAX_KEY_BYTES} bytes"
                )
            encoded_key = str.encode(key, "utf-8", errors="surrogatepass")
            if len(encoded_key) > DEFAULT_MAX_KEY_BYTES:
                raise ValueError(
                    f"{label} contains a key exceeding {DEFAULT_MAX_KEY_BYTES} bytes"
                )
            detached_key = encoded_key.decode("utf-8", errors="surrogatepass")
            if detached_key in detached_object:
                raise TypeError(f"{label} contains a duplicate JSON object key")
            return detached_key

        def add_current(detached_key: str, child: Any) -> None:
            nonlocal node_count, canonical_size, copied_items
            detached_child, child_nodes, child_size = _attest_json_value_interruptibly(
                child,
                label=label,
                check_cancelled=check_cancelled,
                active=active,
                depth=depth + 1,
            )
            node_count += child_nodes
            if node_count > DEFAULT_MAX_NODES_PER_ELEMENT:
                raise ValueError(
                    f"{label} exceeds its "
                    f"{DEFAULT_MAX_NODES_PER_ELEMENT}-node limit"
                )
            from json.encoder import encode_basestring_ascii

            key_size = len(encode_basestring_ascii(detached_key))
            if copied_items:
                canonical_size += 2
            canonical_size += 2 * depth + key_size + 2 + child_size
            copied_items += 1
            if canonical_size + 2 + 2 * (depth - 1) > _MAX_CONFIG_JSON_BYTES:
                raise ValueError(f"{label} contains an oversized JSON object")
            detached_object[detached_key] = detached_child

        try:
            if type(value) is dict:
                item_count = len(value)
                for index, (key, child) in enumerate(value.items()):
                    add_current(detach_current_key(key), child)
                    if index + 1 < item_count:
                        check_cancelled()
            else:
                check_cancelled()
                keys_source = value.keys()
                check_cancelled()
                keys = iter(keys_source)
                check_cancelled()
                while True:
                    try:
                        key = next(keys)
                    except StopIteration:
                        break
                    detached_key = detach_current_key(key)
                    check_cancelled()
                    child = value[key]
                    add_current(detached_key, child)
                    check_cancelled()
        finally:
            active.remove(identity)
        if copied_items:
            canonical_size += 2 + 2 * (depth - 1)
        return detached_object, node_count, canonical_size
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} contains a circular JSON array")
        active.add(identity)
        node_count = 1
        detached_array: list[Any] = []
        canonical_size = 2

        def add_current(child: Any) -> None:
            nonlocal node_count, canonical_size
            detached_child, child_nodes, child_size = _attest_json_value_interruptibly(
                child,
                label=label,
                check_cancelled=check_cancelled,
                active=active,
                depth=depth + 1,
            )
            node_count += child_nodes
            if node_count > DEFAULT_MAX_NODES_PER_ELEMENT:
                raise ValueError(
                    f"{label} exceeds its "
                    f"{DEFAULT_MAX_NODES_PER_ELEMENT}-node limit"
                )
            if detached_array:
                canonical_size += 2
            canonical_size += 2 * depth + child_size
            if canonical_size + 2 + 2 * (depth - 1) > _MAX_CONFIG_JSON_BYTES:
                raise ValueError(f"{label} contains an oversized JSON array")
            detached_array.append(detached_child)

        try:
            if type(value) is list:
                item_count = len(value)
                for index, child in enumerate(value):
                    add_current(child)
                    if index + 1 < item_count:
                        check_cancelled()
            else:
                check_cancelled()
                iterator = iter(value)
                check_cancelled()
                while True:
                    try:
                        child = next(iterator)
                    except StopIteration:
                        break
                    add_current(child)
                    check_cancelled()
        finally:
            active.remove(identity)
        if detached_array:
            canonical_size += 2 + 2 * (depth - 1)
        return detached_array, node_count, canonical_size
    raise TypeError(f"unsupported decoded JSON value: {type(value).__name__}")


def _canonical_json_value_size_interruptibly(
    value: Any,
    *,
    level: int,
    check_cancelled: Callable[[], None],
) -> int:
    """Count canonical bytes without materializing an attacker-sized scalar."""

    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, str):
        from json.encoder import encode_basestring_ascii

        size = 2
        value_length = len(value)
        for offset in range(0, value_length, _SEMANTIC_SCAN_CHARS):
            end = min(value_length, offset + _SEMANTIC_SCAN_CHARS)
            size += len(encode_basestring_ascii(value[offset:end])) - 2
            if size + 1 > _MAX_CONFIG_JSON_BYTES:
                raise ValueError("canonical JSON value exceeds its byte limit")
            if end < value_length:
                check_cancelled()
        return size
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        return len(repr(value))
    if isinstance(value, list):
        if not value:
            return 2
        size = 2
        item_count = len(value)
        for index, item in enumerate(value):
            if index:
                size += 2
            size += 2 * (level + 1)
            size += _canonical_json_value_size_interruptibly(
                item,
                level=level + 1,
                check_cancelled=check_cancelled,
            )
            if size + 1 > _MAX_CONFIG_JSON_BYTES:
                raise ValueError("canonical JSON value exceeds its byte limit")
            if index + 1 < item_count:
                check_cancelled()
        return size + 2 + 2 * level
    if isinstance(value, dict):
        if not value:
            return 2
        size = 2
        item_count = len(value)
        for index, (key, item) in enumerate(value.items()):
            if index:
                size += 2
            size += 2 * (level + 1)
            size += _canonical_json_value_size_interruptibly(
                key,
                level=level + 1,
                check_cancelled=check_cancelled,
            )
            size += 2
            size += _canonical_json_value_size_interruptibly(
                item,
                level=level + 1,
                check_cancelled=check_cancelled,
            )
            if size + 1 > _MAX_CONFIG_JSON_BYTES:
                raise ValueError("canonical JSON value exceeds its byte limit")
            if index + 1 < item_count:
                check_cancelled()
        return size + 2 + 2 * level
    raise TypeError(f"unsupported decoded JSON value: {type(value).__name__}")


def _bounded_json_object_snapshot_impl(
    value: Mapping[str, Any],
    *,
    label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Detach caller-owned config into one bounded JSON object."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} requires a mapping")
    callback_errors: list[BaseException] = []
    if check_cancelled is None:
        detached = dict(value)
        validate_json_complexity(detached, label=label)
        chunks = canonical_json_value_chunks(detached)
    else:
        if not callable(check_cancelled):
            raise TypeError("portable validation cancellation check must be callable")

        def poll() -> None:
            try:
                check_cancelled()
            except BaseException as error:  # noqa: B036 - exact provenance
                callback_errors.append(error)
                raise

        total_nodes = 1
        serialized_size = 2
        copied_items = 0
        detached: dict[str, Any] = {}

        def detach_outer_key(key: Any) -> str:
            if not isinstance(key, str):
                raise TypeError(f"{label} contains a non-text JSON object key")
            if str.__len__(key) > DEFAULT_MAX_KEY_BYTES:
                raise ValueError(
                    f"{label} contains a key exceeding {DEFAULT_MAX_KEY_BYTES} bytes"
                )
            encoded_key = str.encode(key, "utf-8", errors="surrogatepass")
            if len(encoded_key) > DEFAULT_MAX_KEY_BYTES:
                raise ValueError(
                    f"{label} contains a key exceeding {DEFAULT_MAX_KEY_BYTES} bytes"
                )
            detached_key = encoded_key.decode("utf-8", errors="surrogatepass")
            if detached_key in detached:
                raise TypeError(f"{label} contains a duplicate JSON object key")
            return detached_key

        def attest_outer_current(detached_key: str, current: Any) -> Any:
            nonlocal total_nodes, serialized_size, copied_items
            detached_current, current_nodes, current_size = (
                _attest_json_value_interruptibly(
                    current,
                    label=label,
                    check_cancelled=poll,
                    depth=2,
                )
            )
            total_nodes += current_nodes
            if total_nodes > DEFAULT_MAX_NODES_PER_ELEMENT:
                raise ValueError(
                    f"{label} exceeds its "
                    f"{DEFAULT_MAX_NODES_PER_ELEMENT}-node limit"
                )
            from json.encoder import encode_basestring_ascii

            key_size = len(encode_basestring_ascii(detached_key))
            if copied_items:
                serialized_size += 2
            serialized_size += 2 + key_size + 2 + current_size
            copied_items += 1
            # Add the root's final newline/indent/brace and file newline.
            if serialized_size + 3 > _MAX_CONFIG_JSON_BYTES:
                raise ValueError(
                    f"{label} exceeds its {_MAX_CONFIG_JSON_BYTES}-byte limit"
                )
            return detached_current

        # Reject an already-latched stop before invoking any caller-owned
        # Mapping method.  Exact dicts have a trustworthy item count, so their
        # final item needs no terminal poll.  Arbitrary Mapping implementations
        # may lie about ``__len__`` or eagerly override ``items``; consume those
        # through the minimal key/getitem protocol and poll before asking for
        # any possible future key instead.
        if type(value) is dict:
            item_count = len(value)
            for index, (key, current) in enumerate(value.items()):
                detached_key = detach_outer_key(key)
                detached_current = attest_outer_current(detached_key, current)
                detached[detached_key] = detached_current
                if index + 1 < item_count:
                    poll()
        else:
            poll()
            keys_source = value.keys()
            poll()
            keys = iter(keys_source)
            poll()
            while True:
                try:
                    key = next(keys)
                except StopIteration:
                    break
                detached_key = detach_outer_key(key)
                poll()
                current = value[key]
                detached_current = attest_outer_current(detached_key, current)
                detached[detached_key] = detached_current
                poll()
        chunks = _canonical_json_value_chunks_interruptibly(
            detached,
            check_cancelled=poll,
        )
    payload = bytearray()
    try:
        for chunk in chunks:
            payload.extend(chunk)
            if len(payload) + 1 > _MAX_CONFIG_JSON_BYTES:
                raise ValueError(
                    f"{label} exceeds its {_MAX_CONFIG_JSON_BYTES}-byte limit"
                )
        payload.extend(b"\n")
        if check_cancelled is not None:
            poll()
        snapshot = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        if any(exc is callback_error for callback_error in callback_errors):
            raise
        raise ValueError(f"{label} is not canonical JSON data") from exc
    if not isinstance(snapshot, dict):  # pragma: no cover - detached is a dict
        raise AssertionError(f"{label} snapshot is not an object")
    return snapshot


def _bounded_json_object_snapshot(
    value: Mapping[str, Any],
    *,
    label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Preserve iteration-sentinel callback identity across JSON generators."""

    if check_cancelled is None or not callable(check_cancelled):
        return _bounded_json_object_snapshot_impl(
            value,
            label=label,
            check_cancelled=check_cancelled,
        )

    iteration_error: StopIteration | StopAsyncIteration | None = None
    iteration_carrier: _CallbackIterationStop | None = None
    callback_errors: list[BaseException] = []

    def preserve_iteration_stop() -> None:
        nonlocal iteration_error, iteration_carrier
        try:
            check_cancelled()
        except (StopIteration, StopAsyncIteration) as error:
            if error is not iteration_error:
                iteration_error = error
                iteration_carrier = _CallbackIterationStop(error)
            assert iteration_carrier is not None
            raise iteration_carrier from None
        except BaseException as error:  # noqa: B036 - exact provenance
            callback_errors.append(error)
            raise

    try:
        return _bounded_json_object_snapshot_impl(
            value,
            label=label,
            check_cancelled=preserve_iteration_stop,
        )
    except _CallbackIterationStop as failure:
        if failure is not iteration_carrier:
            raise
        raise failure.error from None
    except (RecursionError, TypeError) as error:
        if any(error is callback_error for callback_error in callback_errors):
            raise
        raise ValueError(f"{label} is not canonical JSON data") from error
    except ValueError:
        # Complexity and canonical-byte budgets are policy results, just like
        # the pre-serialization checks in the legacy branch.  Callback-raised
        # ValueError objects and current policy errors both escape unchanged.
        raise


def _canonical_json_value_chunks_interruptibly(
    value: Any,
    *,
    check_cancelled: Callable[[], None],
    level: int = 0,
) -> Iterator[bytes]:
    """Encode canonical JSON while polling bounded containers and key sorts."""

    if isinstance(value, str):
        from json.encoder import encode_basestring_ascii

        yield b'"'
        value_length = len(value)
        for offset in range(0, value_length, 64 * 1024):
            encoded = encode_basestring_ascii(value[offset : offset + 64 * 1024])
            yield encoded[1:-1].encode("ascii")
            if offset + 64 * 1024 < value_length:
                check_cancelled()
        yield b'"'
        return
    if isinstance(value, list):
        if not value:
            yield b"[]"
            return
        yield b"[\n"
        item_count = len(value)
        for index, item in enumerate(value):
            if index:
                yield b",\n"
            yield b"  " * (level + 1)
            yield from _canonical_json_value_chunks_interruptibly(
                item,
                check_cancelled=check_cancelled,
                level=level + 1,
            )
            if index + 1 < item_count:
                check_cancelled()
        yield b"\n" + b"  " * level + b"]"
        return
    if isinstance(value, dict):
        if not value:
            yield b"{}"
            return
        keys = list(_interitem_cancellation(value, check_cancelled))
        ordered_keys = _interruptible_sorted_security_items(
            keys,
            key=None,
            check_cancelled=check_cancelled,
        )
        yield b"{\n"
        key_count = len(ordered_keys)
        for index, key in enumerate(ordered_keys):
            if index:
                yield b",\n"
            yield b"  " * (level + 1)
            yield from _canonical_json_value_chunks_interruptibly(
                key,
                check_cancelled=check_cancelled,
                level=level + 1,
            )
            yield b": "
            yield from _canonical_json_value_chunks_interruptibly(
                value[key],
                check_cancelled=check_cancelled,
                level=level + 1,
            )
            if index + 1 < key_count:
                check_cancelled()
        yield b"\n" + b"  " * level + b"}"
        return
    yield from canonical_json_value_chunks(value, level=level)


def _canonical_json_array_chunks_interruptibly(
    values: Iterable[Any],
    *,
    check_cancelled: Callable[[], None],
) -> Iterator[bytes]:
    """Encode a canonical array with interruptible nested value ordering."""

    iterator = iter(values)
    emitted = False
    try:
        for value in iterator:
            if not emitted:
                yield b"[\n"
                emitted = True
            else:
                yield b",\n"
            yield b"  "
            yield from _canonical_json_value_chunks_interruptibly(
                value,
                check_cancelled=check_cancelled,
                level=1,
            )
        yield b"\n]\n" if emitted else b"[]\n"
    finally:
        close_iterator = getattr(iterator, "close", None)
        if callable(close_iterator):
            close_iterator()


def _canonical_json_payload_matches_interruptibly(
    value: Any,
    payload: bytes,
    *,
    check_cancelled: Callable[[], None],
) -> bool:
    """Compare canonical bytes incrementally without one nested ``json.dumps``."""

    iteration_error: StopIteration | StopAsyncIteration | None = None
    iteration_carrier: _CallbackIterationStop | None = None

    def preserve_iteration_stop() -> None:
        nonlocal iteration_error, iteration_carrier
        try:
            check_cancelled()
        except (StopIteration, StopAsyncIteration) as error:
            if error is not iteration_error:
                iteration_error = error
                iteration_carrier = _CallbackIterationStop(error)
            assert iteration_carrier is not None
            raise iteration_carrier from None

    offset = 0
    chunks = _canonical_json_value_chunks_interruptibly(
        value,
        check_cancelled=preserve_iteration_stop,
    )
    try:
        try:
            for chunk in chunks:
                end = offset + len(chunk)
                if payload[offset:end] != chunk:
                    return False
                offset = end
        finally:
            chunks.close()
    except _CallbackIterationStop as failure:
        if failure is not iteration_carrier:
            raise
        raise failure.error from None
    return len(payload) == offset + 1 and payload[offset] == 10


def _environment_snapshot(
    environ: Mapping[str, str] | None,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        raise TypeError("portable validation environment must be a mapping")
    if check_cancelled is None:
        snapshot = dict(source)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in snapshot.items()
        ):
            raise TypeError("portable validation environment must contain text pairs")
    else:
        if not callable(check_cancelled):
            raise TypeError("portable validation cancellation check must be callable")
        snapshot = {}
        if type(source) is dict:
            item_count = len(source)
            for index, (key, value) in enumerate(source.items()):
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError(
                        "portable validation environment must contain text pairs"
                    )
                snapshot[key] = value
                if index + 1 < item_count:
                    check_cancelled()
        else:
            check_cancelled()
            keys = source.keys()
            check_cancelled()
            iterator = iter(keys)
            check_cancelled()
            while True:
                try:
                    key = next(iterator)
                except StopIteration:
                    break
                if not isinstance(key, str):
                    raise TypeError(
                        "portable validation environment must contain text pairs"
                    )
                check_cancelled()
                value = source[key]
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError(
                        "portable validation environment must contain text pairs"
                    )
                snapshot[key] = value
                check_cancelled()
    return snapshot


def _forbidden_paths_snapshot(
    forbidden_paths: Iterable[Path],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[Path, ...]:
    if check_cancelled is None:
        forbidden = tuple(forbidden_paths)
        if any(not isinstance(path, Path) for path in forbidden):
            raise TypeError("portable validation forbidden paths must be Path values")
        return forbidden
    if not callable(check_cancelled):
        raise TypeError("portable validation cancellation check must be callable")
    forbidden_items: list[Path] = []
    if type(forbidden_paths) in {tuple, list}:
        path_count = len(forbidden_paths)  # type: ignore[arg-type]
        for index, path in enumerate(forbidden_paths):
            if not isinstance(path, Path):
                raise TypeError(
                    "portable validation forbidden paths must be Path values"
                )
            forbidden_items.append(path)
            if index + 1 < path_count:
                check_cancelled()
    else:
        check_cancelled()
        iterator = iter(forbidden_paths)
        check_cancelled()
        while True:
            try:
                path = next(iterator)
            except StopIteration:
                break
            if not isinstance(path, Path):
                raise TypeError(
                    "portable validation forbidden paths must be Path values"
                )
            forbidden_items.append(path)
            check_cancelled()
    return tuple(forbidden_items)


def _assert_authenticated_publishable_json_value(
    value: Any,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Apply publication policy without resolving authority display paths."""

    if check_cancelled is None:
        assert_publishable_json_value(
            value,
            forbidden_paths=(),
            environ=environ,
            label=label,
        )
    else:
        assert_publishable_json_value(
            value,
            forbidden_paths=(),
            environ=environ,
            label=label,
            check_cancelled=check_cancelled,
        )
    forbidden: set[str] = set()
    selected_paths = (
        forbidden_paths
        if check_cancelled is None
        else _interitem_cancellation(forbidden_paths, check_cancelled)
    )
    for path in selected_paths:
        raw = os.fsdecode(os.fspath(path))
        lexical = os.path.abspath(raw)
        if os.path.isabs(raw):
            forbidden.add(raw)
        forbidden.update((lexical, Path(lexical).as_posix()))
    if check_cancelled is None:
        present_patterns = tuple(pattern for pattern in forbidden if pattern)
    else:
        present_pattern_items: list[str] = []
        pattern_count = len(forbidden)
        for index, pattern in enumerate(forbidden):
            if pattern:
                present_pattern_items.append(pattern)
            if index + 1 < pattern_count:
                check_cancelled()
        present_patterns = tuple(present_pattern_items)
    patterns = (
        tuple(sorted(present_patterns))
        if check_cancelled is None
        else _interruptible_sorted_security_items(
            present_patterns,
            key=None,
            check_cancelled=check_cancelled,
        )
    )
    if check_cancelled is not None:
        check_cancelled()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            item_count = len(current)
            for index, (key, child) in enumerate(current.items()):
                if _contains_pattern(
                    key,
                    patterns,
                    check_cancelled=check_cancelled,
                ):
                    raise ValueError(f"{label} contains an absolute build-machine path")
                stack.append(child)
                if check_cancelled is not None and index + 1 < item_count:
                    check_cancelled()
        elif isinstance(current, (list, tuple)):
            item_count = len(current)
            for index, child in enumerate(current):
                stack.append(child)
                if check_cancelled is not None and index + 1 < item_count:
                    check_cancelled()
        elif isinstance(current, str) and _contains_pattern(
            current,
            patterns,
            check_cancelled=check_cancelled,
        ):
            raise ValueError(f"{label} contains an absolute build-machine path")
        if check_cancelled is not None and stack:
            check_cancelled()


def _assert_no_secret_fields_interruptibly(
    value: Any,
    *,
    source: str,
    check_cancelled: Callable[[], None] | None,
) -> None:
    if check_cancelled is None:
        assert_no_secret_fields(value, source=source)
    else:
        assert_no_credential_fields(
            value,
            source=source,
            check_cancelled=check_cancelled,
        )


def _resolve_embedding_artifact_route_interruptibly(
    config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None,
    check_cancelled: Callable[[], None] | None,
) -> Any:
    if check_cancelled is None:
        return resolve_embedding_artifact_route(config, environ=environ)
    return resolve_embedding_artifact_route(
        config,
        environ=environ,
        check_cancelled=check_cancelled,
    )


def _route_compatibility_options_interruptibly(
    route: Any,
    check_cancelled: Callable[[], None] | None,
) -> Mapping[str, Any]:
    if check_cancelled is None:
        return route.compatibility_options
    return route.interruptible_compatibility_options(check_cancelled)


def _route_public_identity_interruptibly(
    route: Any,
    check_cancelled: Callable[[], None] | None,
) -> Mapping[str, Any]:
    if check_cancelled is None:
        return route.public_identity()
    return route.public_identity(check_cancelled=check_cancelled)


def _json_values_equal_interruptibly(
    left: Any,
    right: Any,
    *,
    check_cancelled: Callable[[], None],
) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if len(left) != len(right):
            return False
        item_count = len(left)
        for index, (key, item) in enumerate(left.items()):
            if key not in right or not _json_values_equal_interruptibly(
                item,
                right[key],
                check_cancelled=check_cancelled,
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) is list:
        if len(left) != len(right):
            return False
        item_count = len(left)
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            if not _json_values_equal_interruptibly(
                left_item,
                right_item,
                check_cancelled=check_cancelled,
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) in {str, bytes}:
        if left is right:
            return True
        if len(left) != len(right):
            return False
        value_length = len(left)
        for offset in range(0, value_length, _SEMANTIC_SCAN_CHARS):
            end = min(value_length, offset + _SEMANTIC_SCAN_CHARS)
            if left[offset:end] != right[offset:end]:
                return False
            if end < value_length:
                check_cancelled()
        return True
    return bool(left == right)


def _route_public_identities_match_interruptibly(
    left_route: Any,
    right_route: Any,
    check_cancelled: Callable[[], None] | None,
) -> bool:
    if check_cancelled is None:
        return _route_public_identity_interruptibly(
            left_route,
            None,
        ) == _route_public_identity_interruptibly(right_route, None)
    left = _route_public_identity_interruptibly(left_route, check_cancelled)
    right = _route_public_identity_interruptibly(right_route, check_cancelled)
    return _json_values_equal_interruptibly(
        left,
        right,
        check_cancelled=check_cancelled,
    )


def _vector_model_suffix(
    model: str,
    *,
    check_cancelled: Callable[[], None] | None,
) -> str:
    if check_cancelled is None:
        return model.replace("/", "__")
    pieces: list[str] = []
    model_length = len(model)
    for offset in range(0, model_length, _SEMANTIC_SCAN_CHARS):
        end = min(model_length, offset + _SEMANTIC_SCAN_CHARS)
        pieces.append(model[offset:end].replace("/", "__"))
        if end < model_length:
            check_cancelled()
    return "".join(pieces)


def _json_file_signature(metadata: os.stat_result) -> tuple[int, ...]:
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


def _read_bounded_json(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    check_cancelled: Callable[[], None] | None = None,
) -> bytearray:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError(f"{label} byte limit must be positive")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError(f"{label} cancellation check must be callable")
    try:
        before_path = path.lstat()
        if (
            stat.S_ISLNK(before_path.st_mode)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_nlink != 1
            or bool(
                getattr(before_path, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
            )
        ):
            raise ValueError(f"{label} is not a private regular file: {path}")
        if before_path.st_size > max_bytes:
            raise ValueError(f"{label} exceeds its {max_bytes}-byte limit: {path}")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _json_file_signature(
                opened
            ) != _json_file_signature(before_path):
                raise ValueError(f"{label} changed while opening: {path}")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, _JSON_READ_CHUNK_BYTES)
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise ValueError(
                        f"{label} exceeds its {max_bytes}-byte limit: {path}"
                    )
                if check_cancelled is not None:
                    check_cancelled()
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = path.lstat()
        if _json_file_signature(after_open) != _json_file_signature(
            opened
        ) or _json_file_signature(after_path) != _json_file_signature(opened):
            raise ValueError(f"{label} changed while reading: {path}")
        return payload
    except OSError as exc:
        raise ValueError(f"{label} is not readable: {path}") from exc


def _load_json(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    require_canonical: bool = False,
    reader: _ViewReader | None = None,
) -> Any:
    check_cancelled = (
        reader._check_cancelled if isinstance(reader, _PublicationViewReader) else None
    )
    if isinstance(reader, _PublicationViewReader):
        reader.validate_json(path, label=label, max_bytes=max_bytes)
    payload = (
        _read_bounded_json(path, label=label, max_bytes=max_bytes)
        if reader is None
        else reader.read_bytes(path, max_bytes=max_bytes)
    )
    if check_cancelled is not None:
        check_cancelled()
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
        )
    except ValueError as exc:
        raise ValueError(f"{label} is invalid JSON: {path}") from exc
    if require_canonical:
        canonical = (
            payload == _json_bytes(value)
            if check_cancelled is None
            else _canonical_json_payload_matches_interruptibly(
                value,
                payload,
                check_cancelled=check_cancelled,
            )
        )
        if not canonical:
            raise ValueError(f"{label} is not canonical JSON: {path}")
    return value


def _load_json_object(
    path: Path,
    *,
    label: str,
    max_bytes: int = _MAX_CONFIG_JSON_BYTES,
    require_canonical: bool = False,
    reader: _ViewReader | None = None,
) -> dict[str, Any]:
    value = _load_json(
        path,
        label=label,
        max_bytes=max_bytes,
        require_canonical=require_canonical,
        reader=reader,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _owned_view_root(root: Path, repo_path: Path) -> tuple[Path, Path]:
    candidate = root.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"portable query view root must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError(f"portable query view must be a directory: {resolved}")

    repository = repo_path.expanduser().resolve()
    if not repository.is_dir():
        raise ValueError(f"repository directory does not exist: {repository}")
    if (
        resolved == repository
        or resolved in repository.parents
        or repository in resolved.parents
    ):
        raise ValueError(
            "portable query view must not overlap the source repository: " f"{resolved}"
        )

    return resolved, repository


def _portable_source_path(
    value: object,
    repo_path: Path,
    *,
    source: str,
    authenticated_source_files: frozenset[str] | None = None,
) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) > _MAX_SOURCE_PATH_BYTES:
        raise ValueError(f"{source} exceeds {_MAX_SOURCE_PATH_BYTES} UTF-8 path bytes")
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{source} is not a valid UTF-8 path") from exc
    if len(encoded) > _MAX_SOURCE_PATH_BYTES:
        raise ValueError(f"{source} exceeds {_MAX_SOURCE_PATH_BYTES} UTF-8 path bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(
            f"{source} is not a portable repository-relative path: {raw!r}"
        )

    if authenticated_source_files is not None:
        raw_parts = raw.split("/")
        normalized = PurePosixPath(raw)
        if (
            raw.startswith("~")
            or raw.startswith("/")
            or "\\" in raw
            or _WINDOWS_DRIVE_RE.match(raw)
            or normalized.is_absolute()
            or any(part in {"", ".", "..", "~"} for part in raw_parts)
            or raw != normalized.as_posix()
            or len(normalized.parts) > _MAX_SOURCE_PATH_COMPONENTS
        ):
            raise ValueError(
                f"{source} is not a portable repository-relative path: {raw!r}"
            )
        normalized_text = normalized.as_posix()
        if normalized_text not in authenticated_source_files:
            raise ValueError(
                f"{source} is not in the authenticated repository source: {raw}"
            )
        return normalized_text

    path = Path(raw).expanduser()
    if path.is_absolute():
        try:
            path = path.relative_to(repo_path)
        except ValueError as exc:
            raise ValueError(f"{source} points outside the repository: {raw}") from exc
        raw = path.as_posix()
    else:
        native = path.as_posix()
        if native != raw:
            raw_parts = raw.replace("\\", "/").split("/")
            if any(part in {"", ".", ".."} for part in raw_parts):
                raise ValueError(f"{source} is not repository-relative: {raw}")
            raw = native
        elif "\\" in raw or raw.startswith("//") or _WINDOWS_DRIVE_RE.match(raw):
            raise ValueError(
                f"{source} is not a portable repository-relative path: {raw!r}"
            )

    raw_parts = raw.split("/")
    normalized = PurePosixPath(raw)
    if (
        normalized.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw != normalized.as_posix()
        or len(normalized.parts) > _MAX_SOURCE_PATH_COMPONENTS
    ):
        raise ValueError(f"{source} is not repository-relative: {raw}")
    normalized_text = normalized.as_posix()
    try:
        validate_repository_file(repo_path, normalized_text)
    except ValueError as exc:
        raise ValueError(
            f"{source} is not a stable contained source file: {raw}"
        ) from exc
    return normalized_text


def _normalize_pickle_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _OwnedViewReader,
) -> list[dict[str, Any]]:
    """Load documents from an explicitly trusted, machine-local pickle."""

    try:
        payload = reader.read_bytes(
            path,
            max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        )
        with io.BytesIO(payload) as handle:
            documents = compat_pickle.load(handle)
    except Exception as exc:
        raise ValueError(
            f"trusted-local vector documents pickle is invalid: {path.name}"
        ) from exc
    if not isinstance(documents, list):
        raise ValueError(f"portable vector documents must be a list: {path.name}")

    payload: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        page_content = getattr(document, "page_content", None)
        if not isinstance(page_content, str):
            raise ValueError(
                f"portable vector document {index} has invalid content: {path.name}"
            )
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            raise ValueError(
                f"portable vector document {index} has invalid metadata: {path.name}"
            )
        assert_no_secret_fields(
            metadata,
            source=f"portable vector document {index} metadata",
        )
        normalized_metadata = dict(metadata)
        normalized_metadata["file"] = _portable_source_path(
            metadata.get("file"),
            repo_path,
            source=f"vector document {index} file",
        )
        payload.append(
            {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
        )
    # Validate serializability before any file in the owned view is changed.
    _json_bytes(payload)
    return payload


def _owned_inventory_paths(
    root: Path,
    ownership: object,
    *,
    kind: str,
    check_cancelled: Callable[[], None] | None = None,
) -> set[Path]:
    inventory = _view_inventory(ownership)
    selected: set[Path] = set()
    for relative, observed_kind in _interitem_cancellation(
        inventory,
        check_cancelled,
    ):
        if observed_kind == kind:
            selected.add(root.joinpath(*PurePosixPath(relative).parts))
    return selected


def _authenticate_initial_file(
    reader: _ViewReader,
    root: Path,
    ownership: object,
    path: Path,
    *,
    max_bytes: int,
    cache_bytes: bool = False,
    keep_descriptor: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Bind one later read/parser to the bytes in the initial tree capture."""

    relative = path.relative_to(root).as_posix()
    records = _view_file_records(ownership, check_cancelled=check_cancelled)
    record = None
    for item in _interitem_cancellation(records, check_cancelled):
        if item.path == relative:
            record = item
            break
    if record is None:
        raise ValueError(f"portable vector artifact is missing: {relative}")
    reader.authenticate(
        path,
        {"size": record.size, "sha256": record.sha256},
        cache_bytes=cache_bytes,
        max_bytes=max_bytes,
        keep_descriptor=keep_descriptor,
    )


def _pickle_paths(
    root: Path,
    ownership: object,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> list[Path]:
    inventory = _owned_inventory_paths(
        root,
        ownership,
        kind="file",
        check_cancelled=check_cancelled,
    )
    selected = [
        path
        for path in _interitem_cancellation(inventory, check_cancelled)
        if path.suffix.casefold() in _PICKLE_SUFFIXES
    ]
    if check_cancelled is None:
        return sorted(selected)
    return list(
        _interruptible_sorted_security_items(
            selected,
            key=None,
            check_cancelled=check_cancelled,
        )
    )


def _reject_inert_pickles(
    root: Path,
    ownership: object,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    pickles = _pickle_paths(
        root,
        ownership,
        check_cancelled=check_cancelled,
    )
    if pickles:
        raise ValueError(
            "portable-inert query view must not contain pickle: "
            f"{pickles[0].relative_to(root)}"
        )


def _normalize_json_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _ViewReader | None = None,
    authenticated_source_files: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    documents = _load_json(
        path,
        label="portable vector documents",
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        reader=reader,
    )
    if not isinstance(documents, list):
        raise ValueError(f"portable vector documents must be a JSON list: {path}")

    payload: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict) or set(document) != {
            "page_content",
            "metadata",
        }:
            raise ValueError(
                f"portable vector document {index} has invalid shape: {path.name}"
            )
        page_content = document["page_content"]
        metadata = document["metadata"]
        if not isinstance(page_content, str) or not isinstance(metadata, dict):
            raise ValueError(
                "portable vector document "
                f"{index} has invalid content or metadata: {path.name}"
            )
        assert_no_secret_fields(
            metadata,
            source=f"portable vector document {index} metadata",
        )
        raw_file = metadata.get("file")
        if raw_file is not None and not isinstance(raw_file, str):
            raise ValueError(
                f"portable vector document {index} has invalid file: {path.name}"
            )
        normalized_metadata = dict(metadata)
        normalized_metadata["file"] = _portable_source_path(
            raw_file,
            repo_path,
            source=f"vector document {index} file",
            authenticated_source_files=authenticated_source_files,
        )
        payload.append(
            {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
        )
    return payload


def _iter_content_bound_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _PublicationViewReader,
    view_type: str,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
    consume: Callable[[dict[str, Any]], None],
) -> int:
    check_cancelled = (
        reader._check_cancelled if isinstance(reader, _PublicationViewReader) else None
    )
    semantic_check = check_cancelled
    relative = PurePosixPath(path.relative_to(reader.root).as_posix())
    document_count = 0
    with reader.open_file(
        relative,
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    ) as source:
        interruptible_reader: _InterruptibleReader | None = None
        if semantic_check is not None:
            interruptible_reader = _InterruptibleReader(source, semantic_check)
        documents = iter_bounded_json_array(
            (source if interruptible_reader is None else interruptible_reader),
            label=f"portable {view_type} documents",
        )
        iterator = enumerate(documents)
        while True:
            try:
                index, document = next(iterator)
            except StopIteration:
                break
            if not isinstance(document, dict) or not _mapping_has_exact_keys(
                document,
                {"page_content", "metadata"},
                check_cancelled=semantic_check,
            ):
                raise ValueError(
                    f"portable {view_type} document {index} has an invalid shape"
                )
            page_content = document["page_content"]
            metadata = document["metadata"]
            if not isinstance(page_content, str) or not isinstance(metadata, dict):
                raise ValueError(
                    f"portable {view_type} document {index} has invalid content or "
                    "metadata"
                )
            _assert_no_secret_fields_interruptibly(
                metadata,
                source=f"portable {view_type} document {index} metadata",
                check_cancelled=semantic_check,
            )
            raw_file = metadata.get("file")
            if raw_file is not None and not isinstance(raw_file, str):
                raise ValueError(
                    f"portable {view_type} document {index} has an invalid source path"
                )
            normalized_metadata = (
                dict(metadata)
                if semantic_check is None
                else dict(
                    _interitem_cancellation(
                        metadata.items(),
                        semantic_check,
                    )
                )
            )
            if view_type.startswith("vector") or raw_file is not None:
                normalized_metadata["file"] = _portable_source_path(
                    raw_file,
                    repo_path,
                    source=f"portable {view_type} document {index} file",
                    authenticated_source_files=authenticated_source_files,
                )
            normalized_document = {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
            _assert_authenticated_publishable_json_value(
                normalized_document,
                forbidden_paths=forbidden_paths,
                environ=environ,
                label=f"portable {view_type} document {index}",
                check_cancelled=semantic_check,
            )
            consume(normalized_document)
            document_count += 1
    return document_count


def _require_content_bound_canonical_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _PublicationViewReader,
    view_type: str,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
) -> int:
    check_cancelled = (
        reader._check_cancelled if isinstance(reader, _PublicationViewReader) else None
    )
    size = 0
    digest = hashlib.sha256()
    emitted = False

    def add_chunk(chunk: bytes) -> None:
        nonlocal size
        size += len(chunk)
        if size > MAX_PORTABLE_DOCUMENTS_JSON_BYTES:
            raise ValueError(f"portable {view_type} documents exceed their byte limit")
        digest.update(chunk)

    def consume(document: dict[str, Any]) -> None:
        nonlocal emitted
        add_chunk(b"[\n" if not emitted else b",\n")
        emitted = True
        add_chunk(b"  ")
        chunks = (
            canonical_json_value_chunks(document, level=1)
            if check_cancelled is None
            else _canonical_json_value_chunks_interruptibly(
                document,
                check_cancelled=check_cancelled,
                level=1,
            )
        )
        for chunk in chunks:
            add_chunk(chunk)

    count = _iter_content_bound_documents(
        path,
        repo_path,
        reader=reader,
        view_type=view_type,
        forbidden_paths=forbidden_paths,
        environ=environ,
        authenticated_source_files=authenticated_source_files,
        consume=consume,
    )
    add_chunk(b"\n]\n" if emitted else b"[]\n")
    record = reader.record(path)
    if size != record.size or digest.hexdigest() != record.sha256:
        raise ValueError(f"portable {view_type} documents are not canonical JSON")
    return count


def _normalize_bm25_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _OwnedViewReader,
) -> None:
    documents = _load_json(
        path,
        label="portable BM25 documents",
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        reader=reader,
    )
    if not isinstance(documents, list):
        raise ValueError(f"portable BM25 documents must be a JSON list: {path}")

    normalized: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict) or set(document) != {
            "page_content",
            "metadata",
        }:
            raise ValueError(
                f"portable BM25 document {index} has invalid shape: {path.name}"
            )
        page_content = document["page_content"]
        metadata = document["metadata"]
        if not isinstance(page_content, str) or not isinstance(metadata, dict):
            raise ValueError(
                f"portable BM25 document {index} has invalid content or metadata"
            )
        assert_no_secret_fields(
            metadata,
            source=f"portable BM25 document {index} metadata",
        )
        raw_file = metadata.get("file")
        if raw_file is not None and not isinstance(raw_file, str):
            raise ValueError(
                f"portable BM25 document {index} has an invalid source path"
            )
        normalized_metadata = dict(metadata)
        if raw_file is not None:
            normalized_metadata["file"] = _portable_source_path(
                raw_file,
                repo_path,
                source=f"BM25 document {index} file",
            )
        normalized.append(
            {"page_content": page_content, "metadata": normalized_metadata}
        )
    path.write_bytes(_json_bytes(normalized))


def _is_mutable_vector_state(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == VECTOR_VIEW_UPDATE_MARKER
        or name.endswith(".save-in-progress")
        or name.startswith(_MUTABLE_VECTOR_PREFIXES)
    )


def _validate_vector_model_policy(
    root: Path,
    *,
    ownership: object,
    model_suffix: str,
    native_authorized: bool,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Reject ambiguous multi-model trees and untrusted serialized objects."""

    allowed_model_artifacts = {
        root / f"config_{model_suffix}.json",
    }
    for level in _VECTOR_LEVELS:
        allowed_model_artifacts.update(
            {
                root / level / f"config_{model_suffix}.json",
                root / level / f"documents_{model_suffix}.json",
                root / level / f"documents_{model_suffix}.pkl",
                root / level / f"index_{model_suffix}.faiss",
                root / level / f"index_{model_suffix}.pkl",
            }
        )
    inventory_files = _owned_inventory_paths(
        root,
        ownership,
        kind="file",
        check_cancelled=check_cancelled,
    )
    unexpected_items = [
        path
        for path in _interitem_cancellation(inventory_files, check_cancelled)
        if path.name.casefold().startswith(("config_", "documents_", "index_"))
        and path not in allowed_model_artifacts
    ]
    unexpected_model_artifacts = (
        sorted(unexpected_items)
        if check_cancelled is None
        else list(
            _interruptible_sorted_security_items(
                unexpected_items,
                key=None,
                check_cancelled=check_cancelled,
            )
        )
    )
    if unexpected_model_artifacts:
        raise ValueError(
            "portable vector view contains an unknown or other-model artifact: "
            f"{unexpected_model_artifacts[0].relative_to(root)}"
        )

    pickles = _pickle_paths(
        root,
        ownership,
        check_cancelled=check_cancelled,
    )
    if not native_authorized:
        _reject_inert_pickles(
            root,
            ownership,
            check_cancelled=check_cancelled,
        )
        return

    allowed_pickles = {
        root / level / f"documents_{model_suffix}.pkl" for level in _VECTOR_LEVELS
    }
    allowed_pickles.update(
        root / level / f"index_{model_suffix}.pkl" for level in _VECTOR_LEVELS
    )
    allowed_pickles.update(
        root / name for name in _REMOVABLE_MUTABLE_VECTOR_FILES if name.endswith(".pkl")
    )
    unexpected = [path for path in pickles if path not in allowed_pickles]
    if unexpected:
        raise ValueError(
            "trusted-local vector view contains an unexpected pickle: "
            f"{unexpected[0].relative_to(root)}"
        )


def _validate_vector_semantics(
    config: Mapping[str, Any],
    view_config: Mapping[str, Any],
    *,
    portable_artifact_policy: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[str, int, str | None, str, str]:
    """Close the manifest-route to persisted-config compatibility contract."""

    route_environment = {} if portable_artifact_policy else None
    load_policy_resolver = (
        resolve_embedding_artifact_load_policy_from_options
        if portable_artifact_policy
        else resolve_embedding_load_policy_from_options
    )
    route = _resolve_embedding_artifact_route_interruptibly(
        view_config,
        environ=route_environment,
        check_cancelled=check_cancelled,
    )
    route_options = _route_compatibility_options_interruptibly(
        route,
        check_cancelled,
    )
    expected_revision = (
        (
            load_policy_resolver(
                route.model,
                route_options,
                check_cancelled=check_cancelled,
            )
            if portable_artifact_policy and check_cancelled is not None
            else load_policy_resolver(
                route.model,
                route_options,
            )
        ).revision
        if route.provider == "huggingface"
        else None
    )
    required_checks: tuple[tuple[str, object], ...] = (
        ("embedding_model", route.model),
        ("dimension", route.dimension),
        ("embedding_revision", expected_revision),
    )
    for key, expected in required_checks:
        actual = config.get(key)
        matches = (
            actual == expected
            if check_cancelled is None
            else _json_values_equal_interruptibly(
                actual,
                expected,
                check_cancelled=check_cancelled,
            )
        )
        if not matches:
            raise ValueError(
                f"portable vector persistence {key} does not match its view route"
            )
    if "embedding_dimension" in config and config["embedding_dimension"] != (
        route.dimension
    ):
        raise ValueError(
            "portable vector persistence embedding_dimension does not match its "
            "view route"
        )
    callback_errors: list[BaseException] = []

    def provider_poll() -> None:
        assert check_cancelled is not None
        try:
            check_cancelled()
        except BaseException as error:  # noqa: B036 - exact provenance
            callback_errors.append(error)
            raise

    try:
        provider_value = str(config.get("embedding_provider", ""))
        provider = (
            normalize_provider(provider_value)
            if check_cancelled is None
            else normalize_provider(
                provider_value,
                check_cancelled=provider_poll,
            )
        )
    except ValueError as exc:
        if any(exc is callback_error for callback_error in callback_errors):
            raise
        raise ValueError(
            "portable vector persistence has an invalid embedding provider"
        ) from exc
    if provider != route.provider:
        raise ValueError(
            "portable vector persistence embedding provider does not match "
            "its view route"
        )

    expected_metric = view_config.get("index_metric", "ip")
    if (
        type(expected_metric) is not str
        or len(expected_metric) > 2
        or expected_metric not in {"ip", "l2"}
    ):
        raise ValueError(
            f"portable vector view has unsupported index metric: {expected_metric!r}"
        )
    persisted_metric = config.get("index_metric")
    if not (
        persisted_metric == expected_metric
        if check_cancelled is None
        else _json_values_equal_interruptibly(
            persisted_metric,
            expected_metric,
            check_cancelled=check_cancelled,
        )
    ):
        raise ValueError(
            "portable vector persistence index metric does not match its view config"
        )

    expected_index_type = view_config.get("index_type", "flat")
    if (
        type(expected_index_type) is not str
        or len(expected_index_type) > 4
        or expected_index_type not in {"flat", "ivf"}
    ):
        raise ValueError(
            "portable vector view has unsupported index type: "
            f"{expected_index_type!r}"
        )
    persisted_index_type = config.get("index_type")
    if not (
        persisted_index_type == expected_index_type
        if check_cancelled is None
        else _json_values_equal_interruptibly(
            persisted_index_type,
            expected_index_type,
            check_cancelled=check_cancelled,
        )
    ):
        raise ValueError(
            "portable vector persistence index type does not match its view config"
        )

    persisted_identity = config.get("artifact")
    builder_schema = view_config.get("builder_schema")
    identity_required = (
        view_config.get("embedding_fingerprint") is not None
        or (
            isinstance(builder_schema, int)
            and not isinstance(builder_schema, bool)
            and builder_schema >= 3
        )
        or bool(route_options)
    )
    if persisted_identity is not None:
        if not isinstance(persisted_identity, Mapping):
            raise ValueError("portable vector persistence artifact identity is invalid")
        persisted_route = _resolve_embedding_artifact_route_interruptibly(
            persisted_identity,
            environ=route_environment,
            check_cancelled=check_cancelled,
        )
        if not _route_public_identities_match_interruptibly(
            persisted_route,
            route,
            check_cancelled,
        ):
            raise ValueError(
                "portable vector persistence route identity does not match its view "
                "route"
            )
        expected_fingerprint = view_config.get("embedding_fingerprint")
        persisted_fingerprint = persisted_identity.get("embedding_fingerprint")
        fingerprint_matches = (
            persisted_fingerprint == expected_fingerprint
            if check_cancelled is None
            else _json_values_equal_interruptibly(
                persisted_fingerprint,
                expected_fingerprint,
                check_cancelled=check_cancelled,
            )
        )
        if expected_fingerprint is not None and not fingerprint_matches:
            raise ValueError(
                "portable vector persistence embedding fingerprint does not match "
                "its view config"
            )
        persisted_revision = (
            (
                load_policy_resolver(
                    persisted_route.model,
                    persisted_route.interruptible_compatibility_options(
                        check_cancelled
                    ),
                    check_cancelled=check_cancelled,
                )
                if portable_artifact_policy and check_cancelled is not None
                else load_policy_resolver(
                    persisted_route.model,
                    persisted_route.compatibility_options,
                )
            ).revision
            if persisted_route.provider == "huggingface"
            else None
        )
        revision_matches = (
            persisted_revision == expected_revision
            if check_cancelled is None
            else _json_values_equal_interruptibly(
                persisted_revision,
                expected_revision,
                check_cancelled=check_cancelled,
            )
        )
        if not revision_matches:
            raise ValueError(
                "portable vector persistence artifact revision does not match its "
                "view config"
            )
        persisted_identity_metric = persisted_identity.get("index_metric")
        identity_metric_matches = (
            persisted_identity_metric == expected_metric
            if check_cancelled is None
            else _json_values_equal_interruptibly(
                persisted_identity_metric,
                expected_metric,
                check_cancelled=check_cancelled,
            )
        )
        if not identity_metric_matches:
            raise ValueError(
                "portable vector persistence identity metric does not match its "
                "view config"
            )
    elif identity_required:
        raise ValueError(
            "portable vector persistence is missing its embedding artifact identity"
        )
    elif "embedding_kwargs" in config:
        # Legacy configs sometimes expose the semantic options directly. When
        # present, absence is not treated as a wildcard.
        persisted_route = _resolve_embedding_artifact_route_interruptibly(
            config,
            environ=route_environment,
            check_cancelled=check_cancelled,
        )
        if not _route_public_identities_match_interruptibly(
            persisted_route,
            route,
            check_cancelled,
        ):
            raise ValueError(
                "portable vector persistence options do not match its view route"
            )

    persisted_builder_schema = (
        persisted_identity.get("builder_schema")
        if isinstance(persisted_identity, Mapping)
        else None
    )
    retained_schema_selected = (
        type(builder_schema) is int and builder_schema >= 7
    ) or (type(persisted_builder_schema) is int and persisted_builder_schema >= 7)
    if retained_schema_selected and not (
        type(builder_schema) is int
        and type(persisted_builder_schema) is int
        and persisted_builder_schema == builder_schema
    ):
        raise ValueError(
            "portable vector persistence builder schema does not match its "
            "view config"
        )
    schema_8_selected = type(builder_schema) is int and builder_schema == 8
    if schema_8_selected:
        if config.get("row_mapping") != VECTOR_ROW_MAPPING_CONTRACT:
            raise ValueError(
                "schema-8 portable vector persistence has an invalid row mapping"
            )
    elif config.get("row_mapping") is not None:
        raise ValueError(
            "legacy portable vector persistence has an unsupported row mapping"
        )

    if route.dimension is None:
        raise ValueError("portable vector route is missing its embedding dimension")
    return (
        _vector_model_suffix(
            route.model,
            check_cancelled=check_cancelled,
        ),
        route.dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
    )


def validate_portable_vector_persistence_semantics(
    config: Mapping[str, Any],
    view_config: Mapping[str, Any],
) -> tuple[str, int, str | None, str, str]:
    """Validate persisted vector identity using artifact-only route policy.

    This source-free helper never discovers credentials or interprets an
    embedding model as a local filesystem locator. Document/source membership
    and directory ownership remain the responsibility of the caller's
    retained-artifact validation layer.
    """

    try:
        config_value = snapshot_retained_import_response(
            config,
            label="portable vector persistence config",
        )
        view_config_value = snapshot_retained_import_response(
            view_config,
            label="portable vector view config",
        )
    except StorageIntegrityError as exc:
        raise ValueError(
            "portable vector persistence inputs must be bounded exact JSON objects"
        ) from exc
    if type(config_value) is not dict or type(view_config_value) is not dict:
        raise ValueError(
            "portable vector persistence inputs must be bounded exact JSON objects"
        )
    config_snapshot = _bounded_json_object_snapshot(
        config_value,
        label="portable vector persistence config",
    )
    view_config_snapshot = _bounded_json_object_snapshot(
        view_config_value,
        label="portable vector view config",
    )
    return _validate_vector_semantics(
        config_snapshot,
        view_config_snapshot,
        portable_artifact_policy=True,
    )


def _faiss_contract(
    path: Path,
    *,
    reader: _OwnedViewReader,
) -> tuple[int, int, str, str, bool]:
    """Read the persisted index contract, importing FAISS on demand."""

    try:
        faiss = importlib.import_module("faiss")
        index = reader.faiss_index(path, faiss)
        dimension = int(index.d)
        total = int(index.ntotal)
        is_trained = bool(index.is_trained)
        metric_type = int(index.metric_type)
        if metric_type == int(faiss.METRIC_INNER_PRODUCT):
            metric = "ip"
        elif metric_type == int(faiss.METRIC_L2):
            metric = "l2"
        else:
            metric = f"unsupported:{metric_type}"
        if isinstance(index, faiss.IndexIVF):
            index_type = "ivf"
        elif isinstance(index, faiss.IndexFlat):
            index_type = "flat"
        else:
            index_type = f"unsupported:{type(index).__name__}"
    except Exception as exc:
        raise ValueError(f"portable vector FAISS index is unreadable: {path}") from exc
    return dimension, total, metric, index_type, is_trained


def _validate_level_semantics(
    path: Path,
    *,
    present: bool,
    level: str,
    model: str,
    provider: str,
    revision: str | None,
    dimension: int,
    metric: object,
    index_type: str,
    count: int,
    reader: _ViewReader,
    canonicalize: bool = True,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    if not present:
        return
    if canonicalize:
        if reader is None:
            raise RuntimeError(
                "portable vector level canonicalization requires an owned reader"
            )
        reader.pin_replacement_target(path)
    config = _load_json_object(
        path,
        label=f"portable vector {level} config",
        require_canonical=not canonicalize,
        reader=reader,
    )
    _assert_no_secret_fields_interruptibly(
        config,
        source=f"portable vector {level} config",
        check_cancelled=check_cancelled,
    )
    expected_fields = {
        "embedding_model",
        "embedding_provider",
        "embedding_revision",
        "dimension",
        "index_type",
        "index_metric",
        "level",
        "num_documents",
    }
    if not _mapping_has_exact_keys(
        config,
        expected_fields,
        check_cancelled=check_cancelled,
    ):
        raise ValueError(
            f"portable vector {level} persistence config has an invalid shape"
        )
    checks: tuple[tuple[str, object], ...] = (
        ("embedding_model", model),
        ("embedding_revision", revision),
        ("dimension", dimension),
        ("index_metric", metric),
        ("index_type", index_type),
        ("level", level),
        ("num_documents", count),
    )
    for key, expected in checks:
        actual = config.get(key)
        matches = (
            actual == expected
            if check_cancelled is None
            else _json_values_equal_interruptibly(
                actual,
                expected,
                check_cancelled=check_cancelled,
            )
        )
        if not matches:
            raise ValueError(
                f"portable vector {level} persistence {key} does not match"
            )
    callback_errors: list[BaseException] = []

    def provider_poll() -> None:
        assert check_cancelled is not None
        try:
            check_cancelled()
        except BaseException as error:  # noqa: B036 - exact provenance
            callback_errors.append(error)
            raise

    try:
        provider_value = str(config.get("embedding_provider", ""))
        persisted_provider = (
            normalize_provider(provider_value)
            if check_cancelled is None
            else normalize_provider(
                provider_value,
                check_cancelled=provider_poll,
            )
        )
    except ValueError as exc:
        if any(exc is callback_error for callback_error in callback_errors):
            raise
        raise ValueError(
            f"portable vector {level} persistence provider is invalid"
        ) from exc
    if persisted_provider != provider:
        raise ValueError(f"portable vector {level} persistence provider does not match")
    if canonicalize:
        assert reader is not None
        reader.replace_bytes(path, _json_bytes(config))


def _validate_vector_layout(
    root: Path,
    repo_path: Path,
    *,
    ownership: object,
    model_suffix: str,
    config: Mapping[str, Any],
    expected_model: str,
    expected_provider: str,
    expected_revision: str | None,
    expected_dimension: int,
    expected_metric: str,
    expected_index_type: str,
    native_authorized: bool,
    reader: _ViewReader,
    canonicalize_level_configs: bool = True,
    authenticated_source_files: frozenset[str] | None = None,
    document_forbidden_paths: Iterable[Path] = (),
    document_environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[
    str,
    dict[Path, list[dict[str, Any]]],
    dict[str, int],
    set[Path],
]:
    expected_documents: set[Path] = set()
    selected: dict[Path, list[dict[str, Any]]] = {}
    formats: set[str] = set()
    counts_present = [f"{level}_documents" in config for level in _VECTOR_LEVELS]
    if any(counts_present) and not all(counts_present):
        raise ValueError("portable vector config has partial level counts")
    legacy_counts = not any(counts_present)
    if legacy_counts and (
        config.get("persistence_schema") is not None
        or config.get("level_artifacts") is not None
    ):
        raise ValueError(
            "portable vector config with persistence records requires level counts"
        )

    derived_counts: dict[str, int] = {}
    stale_paths: set[Path] = set()
    inventory_files = _owned_inventory_paths(
        root,
        ownership,
        kind="file",
        check_cancelled=check_cancelled,
    )

    for level in _VECTOR_LEVELS:
        count = config.get(f"{level}_documents") if not legacy_counts else None
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count < 0
        ):
            raise ValueError(
                f"portable vector config has invalid {level} count: {count!r}"
            )
        level_path = root / level
        pickle_path = level_path / f"documents_{model_suffix}.pkl"
        json_path = level_path / f"documents_{model_suffix}.json"
        present = [path for path in (pickle_path, json_path) if path in inventory_files]
        expected_documents.update(present)
        index_path = level_path / f"index_{model_suffix}.faiss"

        if count == 0:
            # A zero count is authoritative. Older writers left uncommitted
            # current-model level files behind; this caller-owned copy may prune
            # those exact names, but never unknown or other-model artifacts.
            stale_paths.update(
                path
                for path in (
                    pickle_path,
                    json_path,
                    index_path,
                    level_path / f"index_{model_suffix}.pkl",
                    level_path / f"config_{model_suffix}.json",
                )
                if path in inventory_files
            )
            derived_counts[level] = 0
            continue
        if len(present) > 1:
            raise ValueError(
                f"portable vector view mixes pickle and JSON documents in {level}"
            )
        if not present and (count is not None and count > 0):
            raise ValueError(
                f"portable vector view is missing documents for non-empty {level}"
            )
        if not present and count is None:
            if (
                index_path in inventory_files
                or (level_path / f"config_{model_suffix}.json") in inventory_files
            ):
                raise ValueError(f"portable legacy vector {level} level is incomplete")
            derived_counts[level] = 0
            continue
        if index_path not in inventory_files:
            raise ValueError(f"portable vector view is missing its index: {index_path}")

        path = present[0]
        document_format = path.suffix.removeprefix(".")
        if document_format == "pkl":
            if not native_authorized:
                raise MissingNativeIndexAuthorizationError(
                    "vector pickle deserialization requires external native "
                    "authorization"
                )
            _authenticate_initial_file(
                reader,
                root,
                ownership,
                path,
                max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
                cache_bytes=True,
                check_cancelled=check_cancelled,
            )
            payload = _normalize_pickle_documents(path, repo_path, reader=reader)
        else:
            if authenticated_source_files is not None:
                if not isinstance(reader, _PublicationViewReader):
                    raise RuntimeError(
                        "authenticated source validation requires a publication reader"
                    )
                actual_count = _require_content_bound_canonical_documents(
                    path,
                    repo_path,
                    reader=reader,
                    view_type=f"vector {level}",
                    forbidden_paths=document_forbidden_paths,
                    environ={} if document_environ is None else document_environ,
                    authenticated_source_files=authenticated_source_files,
                )
                payload = []
            else:
                payload = _normalize_json_documents(
                    path,
                    repo_path,
                    reader=reader,
                )
                actual_count = len(payload)
        if document_format == "pkl":
            actual_count = len(payload)
        if count is not None and actual_count != count:
            raise ValueError(
                f"portable vector {level} count differs from {path.name}: "
                f"expected {count}, found {actual_count}"
            )
        count = actual_count
        _authenticate_initial_file(
            reader,
            root,
            ownership,
            index_path,
            max_bytes=MAX_PORTABLE_FAISS_INDEX_BYTES,
            keep_descriptor=native_authorized,
            check_cancelled=check_cancelled,
        )
        derived_counts[level] = count
        if legacy_counts and count == 0:
            stale_paths.update(
                candidate
                for candidate in (
                    path,
                    index_path,
                    level_path / f"index_{model_suffix}.pkl",
                    level_path / f"config_{model_suffix}.json",
                )
                if candidate in inventory_files
            )
            continue
        if native_authorized:
            dimension, total, metric, index_type, is_trained = _faiss_contract(
                index_path,
                reader=reader,
            )
            if dimension != expected_dimension:
                raise ValueError(
                    f"portable vector FAISS dimension mismatch in {level}: "
                    f"expected {expected_dimension}, found {dimension}"
                )
            if total != count:
                raise ValueError(
                    f"portable vector FAISS count mismatch in {level}: "
                    f"expected {count}, found {total}"
                )
            if metric != expected_metric:
                raise ValueError(
                    f"portable vector FAISS metric mismatch in {level}: "
                    f"expected {expected_metric}, found {metric}"
                )
            if index_type != expected_index_type:
                raise ValueError(
                    f"portable vector FAISS index type mismatch in {level}: "
                    f"expected {expected_index_type}, found {index_type}"
                )
            if index_type == "ivf" and not is_trained:
                raise ValueError(
                    f"portable vector active IVF index is untrained in {level}"
                )
        level_config_path = level_path / f"config_{model_suffix}.json"
        _validate_level_semantics(
            level_config_path,
            present=level_config_path in inventory_files,
            level=level,
            model=expected_model,
            provider=expected_provider,
            revision=expected_revision,
            dimension=expected_dimension,
            metric=expected_metric,
            index_type=expected_index_type,
            count=count,
            canonicalize=canonicalize_level_configs,
            reader=reader,
            check_cancelled=check_cancelled,
        )
        formats.add(document_format)
        selected[path] = payload

    if not selected:
        raise ValueError("portable vector view has no non-empty document store")
    if len(formats) != 1:
        raise ValueError("portable vector view mixes pickle and JSON documents")

    candidates: set[Path] = set()
    for path in _interitem_cancellation(inventory_files, check_cancelled):
        if path.suffix.casefold() in {
            ".json",
            ".pkl",
            ".pickle",
        } and path.name.startswith("documents_"):
            candidates.add(path)
    if check_cancelled is None:
        unexpected_items = tuple(candidates - expected_documents)
    else:
        unexpected_list: list[Path] = []
        for path in _interitem_cancellation(candidates, check_cancelled):
            if path not in expected_documents:
                unexpected_list.append(path)
        unexpected_items = unexpected_list
    unexpected_documents = (
        sorted(unexpected_items)
        if check_cancelled is None
        else list(
            _interruptible_sorted_security_items(
                unexpected_items,
                key=None,
                check_cancelled=check_cancelled,
            )
        )
    )
    if unexpected_documents:
        raise ValueError(
            "portable vector view contains an unexpected document store: "
            f"{unexpected_documents[0].relative_to(root)}"
        )

    allowed_pickles = {path for path in selected if path.suffix.lower() == ".pkl"}
    allowed_pickles.update(
        path for path in stale_paths if path.suffix.casefold() in _PICKLE_SUFFIXES
    )
    for path in _interitem_cancellation(inventory_files, check_cancelled):
        if (
            path.parent.name in _VECTOR_LEVELS
            and path.name.startswith("index_")
            and path.suffix.casefold() == ".pkl"
        ):
            allowed_pickles.add(path)
    allowed_pickles.update(
        root / name
        for name in _REMOVABLE_MUTABLE_VECTOR_FILES
        if name.endswith(".pkl") and (root / name) in inventory_files
    )
    residual_pickle_items = [
        path
        for path in _interitem_cancellation(inventory_files, check_cancelled)
        if path.suffix.casefold() in _PICKLE_SUFFIXES and path not in allowed_pickles
    ]
    residual_pickles = (
        sorted(residual_pickle_items)
        if check_cancelled is None
        else list(
            _interruptible_sorted_security_items(
                residual_pickle_items,
                key=None,
                check_cancelled=check_cancelled,
            )
        )
    )
    if residual_pickles:
        raise ValueError(
            "portable vector view contains an unexpected pickle: "
            f"{residual_pickles[0].relative_to(root)}"
        )

    removable_mutable = {root / name for name in _REMOVABLE_MUTABLE_VECTOR_FILES}
    residual_mutable_items = [
        path
        for path in _interitem_cancellation(inventory_files, check_cancelled)
        if _is_mutable_vector_state(path) and path not in removable_mutable
    ]
    residual_mutable = (
        sorted(residual_mutable_items)
        if check_cancelled is None
        else list(
            _interruptible_sorted_security_items(
                residual_mutable_items,
                key=None,
                check_cancelled=check_cancelled,
            )
        )
    )
    if residual_mutable:
        raise ValueError(
            "portable vector view contains unexpected mutable state: "
            f"{residual_mutable[0].relative_to(root)}"
        )

    return next(iter(formats)), selected, derived_counts, stale_paths


def _refresh_vector_persistence_records(
    root: Path,
    *,
    model_suffix: str,
) -> None:
    config_path = root / f"config_{model_suffix}.json"
    try:
        ownership = capture_directory_ownership(root)
        reader = _OwnedViewReader(root, ownership)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "portable vector view could not be captured before refreshing records"
        ) from exc
    try:
        config = _load_json_object(
            config_path,
            label="portable vector config",
            reader=reader,
        )
        assert_no_secret_fields(config, source="portable vector config")
        level_artifacts: dict[str, dict[str, dict[str, Any]]] = {}
        for level in _VECTOR_LEVELS:
            count = config.get(f"{level}_documents")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(
                    f"portable vector config has invalid {level} count: {count!r}"
                )
            if count == 0:
                continue
            level_path = root / level
            documents_path = level_path / f"documents_{model_suffix}.json"
            index_path = level_path / f"index_{model_suffix}.faiss"
            index_record = reader.fingerprint(index_path)
            index_record["file"] = index_path.name
            documents_record = reader.fingerprint(
                documents_path,
                max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
            )
            documents_record["file"] = documents_path.name
            level_artifacts[level] = {
                "index": index_record,
                "documents": documents_record,
            }
        reader.verify_root()
        try:
            recorded_tree = capture_directory_ownership(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "portable vector view changed while refreshing records"
            ) from exc
        if recorded_tree != ownership:
            raise RuntimeError("portable vector view changed while refreshing records")
    finally:
        reader.close()
    config["persistence_schema"] = VECTOR_PERSISTENCE_SCHEMA
    config["level_artifacts"] = level_artifacts
    config_path.write_bytes(_json_bytes(config))


def _validate_view_document_count(
    view_config: Mapping[str, Any],
    counts: Mapping[str, int],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    if "document_count" not in view_config:
        return
    raw = view_config["document_count"]
    if not isinstance(raw, Mapping):
        raise ValueError("portable vector document_count must be a mapping")
    expected = {
        level: count for level in _VECTOR_LEVELS if (count := counts[level]) > 0
    }
    if not _mapping_has_exact_keys(
        raw,
        set(expected),
        check_cancelled=check_cancelled,
    ):
        raise ValueError(
            "portable vector document_count must contain exactly the non-empty "
            "levels"
        )
    for level, count in raw.items():
        if (
            not isinstance(level, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or count != expected[level]
        ):
            raise ValueError(
                "portable vector document_count does not match persisted levels"
            )


def _assert_exact_view_tree(
    *,
    ownership: object,
    allowed_files: set[PurePosixPath],
    required_files: set[PurePosixPath],
    label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    allowed_directories = {
        parent
        for path in allowed_files
        for parent in path.parents
        if parent != PurePosixPath(".")
    }
    observed_files: set[PurePosixPath] = set()
    inventory = _view_inventory(ownership)
    for raw_relative, kind in _interitem_cancellation(
        inventory,
        check_cancelled,
    ):
        relative = PurePosixPath(raw_relative)
        if kind == "directory":
            if relative not in allowed_directories:
                raise ValueError(
                    f"{label} contains an unexpected directory: {relative}"
                )
            continue
        if relative not in allowed_files:
            raise ValueError(f"{label} contains an unexpected file: {relative}")
        observed_files.add(relative)

    missing = sorted(required_files - observed_files, key=str)
    if missing:
        raise ValueError(f"{label} is missing a required file: {missing[0]}")


def _assert_normalized_vector_tree(
    *,
    ownership: object,
    model_suffix: str,
    counts: Mapping[str, int],
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    root_config = PurePosixPath(f"config_{model_suffix}.json")
    allowed_files = {root_config}
    required_files = {root_config}
    for level in _VECTOR_LEVELS:
        if counts[level] <= 0:
            continue
        level_path = PurePosixPath(level)
        documents = level_path / f"documents_{model_suffix}.json"
        index = level_path / f"index_{model_suffix}.faiss"
        allowed_files.update(
            {
                level_path / f"config_{model_suffix}.json",
                documents,
                index,
            }
        )
        required_files.update({documents, index})
    _assert_exact_view_tree(
        ownership=ownership,
        allowed_files=allowed_files,
        required_files=required_files,
        label="portable vector view",
        check_cancelled=check_cancelled,
    )


def _normalize_vector_view(
    root: Path,
    repo_path: Path,
    *,
    initial_tree: object,
    reader: _OwnedViewReader,
    view_config: Mapping[str, Any],
    native_authorized: bool,
) -> dict[str, Any]:
    assert_no_secret_fields(view_config, source="portable vector view config")
    initial_files = _owned_inventory_paths(root, initial_tree, kind="file")
    if root / VECTOR_VIEW_UPDATE_MARKER in initial_files:
        raise ValueError(
            "portable vector view has an incomplete update marker: "
            f"{VECTOR_VIEW_UPDATE_MARKER}"
        )
    interrupted = sorted(
        path
        for path in initial_files
        if path.parent == root
        and path.name.startswith(".config_")
        and path.name.endswith(".json.save-in-progress")
    )
    if interrupted:
        raise ValueError(
            "portable vector view contains an interrupted save marker: "
            f"{interrupted[0].name}"
        )

    route = resolve_embedding_artifact_route(view_config)
    model_suffix = route.model.replace("/", "__")
    _validate_vector_model_policy(
        root,
        ownership=initial_tree,
        model_suffix=model_suffix,
        native_authorized=native_authorized,
    )
    expected_config = view_config.get("persistence_config_fingerprint")
    config_path = root / f"config_{model_suffix}.json"
    if expected_config is not None:
        config = _authenticate_vector_generation(
            reader,
            root,
            model_suffix=model_suffix,
            expected_config=expected_config,
            require_canonical=False,
        )
    else:
        config = _load_json_object(
            config_path,
            label="portable vector config",
            reader=reader,
        )
        if config.get("persistence_schema") is not None:
            config = _authenticate_vector_generation(
                reader,
                root,
                model_suffix=model_suffix,
                expected_config=None,
                require_canonical=False,
            )
    assert_no_secret_fields(config, source="portable vector config")
    (
        semantic_suffix,
        expected_dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
    ) = _validate_vector_semantics(config, view_config)
    if semantic_suffix != model_suffix:
        raise ValueError("portable vector config embedding model does not match")

    document_format, documents, counts, stale_paths = _validate_vector_layout(
        root,
        repo_path,
        ownership=initial_tree,
        model_suffix=model_suffix,
        config=config,
        expected_model=route.model,
        expected_provider=route.provider,
        expected_revision=expected_revision,
        expected_dimension=expected_dimension,
        expected_metric=expected_metric,
        expected_index_type=expected_index_type,
        native_authorized=native_authorized,
        reader=reader,
    )
    _validate_view_document_count(view_config, counts)
    reader.verify_root()
    reader.close()

    for path, payload in documents.items():
        output = path.with_suffix(".json")
        output.write_bytes(_json_bytes(payload))
        if document_format == "pkl":
            path.unlink()

    for path in sorted(stale_paths):
        path.unlink(missing_ok=True)
    for level in _VECTOR_LEVELS:
        level_path = root / level
        try:
            level_path.rmdir()
        except OSError:
            pass

    config.update({f"{level}_documents": counts[level] for level in _VECTOR_LEVELS})
    config_path.write_bytes(_json_bytes(config))

    # Query serving does not need build-time incremental state. These exact
    # machine-local files are safe to discard from the owned copy; unfamiliar
    # mutable state was rejected before mutation.
    for name in _REMOVABLE_MUTABLE_VECTOR_FILES:
        (root / name).unlink(missing_ok=True)
    for legacy in sorted(
        path
        for path in initial_files
        if path.parent.parent == root
        and path.parent.name in _VECTOR_LEVELS
        and path.name.startswith("index_")
        and path.suffix.casefold() == ".pkl"
    ):
        legacy.unlink(missing_ok=True)

    _refresh_vector_persistence_records(root, model_suffix=model_suffix)
    try:
        final_tree = capture_directory_ownership(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "portable vector view could not be captured after normalization"
        ) from exc
    _assert_normalized_vector_tree(
        ownership=final_tree,
        model_suffix=model_suffix,
        counts=counts,
    )
    try:
        fingerprint_reader = _OwnedViewReader(root, final_tree)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "portable vector view could not be opened after normalization"
        ) from exc
    try:
        config_record = fingerprint_reader.fingerprint(
            config_path,
            max_bytes=_MAX_CONFIG_JSON_BYTES,
        )
        config_record["file"] = config_path.name
        fingerprint_reader.verify_root()
        try:
            fingerprint_tree = capture_directory_ownership(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "portable vector view changed while recording its fingerprint"
            ) from exc
        if fingerprint_tree != final_tree:
            raise RuntimeError(
                "portable vector view changed while recording its fingerprint"
            )
    finally:
        fingerprint_reader.close()
    return {
        "artifact_scope": "query-serving",
        "portable_document_format": "codenib.vector-documents.v1",
        "persistence_config_fingerprint": config_record,
    }


def _normalize_bm25_view(
    root: Path,
    repo_path: Path,
    *,
    initial_tree: object,
    reader: _OwnedViewReader,
) -> dict[str, Any]:
    _reject_inert_pickles(root, initial_tree)
    documents_path = root / "documents.json"
    _normalize_bm25_documents(documents_path, repo_path, reader=reader)
    metadata_path = root / "bm25_metadata.json"
    metadata = _load_json_object(
        metadata_path,
        label="portable BM25 metadata",
        reader=reader,
    )
    assert_no_secret_fields(metadata, source="portable BM25 metadata")
    if not set(metadata) <= {"project_root", "max_k", "language"}:
        raise ValueError("portable BM25 metadata has an invalid shape")
    max_k = metadata.get("max_k", 10)
    if isinstance(max_k, bool) or not isinstance(max_k, int) or max_k <= 0:
        raise ValueError("portable BM25 metadata max_k must be positive")
    language = metadata.get("language", "english")
    if (
        not isinstance(language, str)
        or not language.strip()
        or "\x00" in language
        or len(language) > 256
    ):
        raise ValueError("portable BM25 metadata language is invalid")
    if metadata.get("project_root") is not None and not isinstance(
        metadata.get("project_root"), str
    ):
        raise ValueError("portable BM25 metadata project_root is invalid")
    metadata.update({"project_root": "source", "max_k": max_k, "language": language})
    reader.verify_root()
    reader.close()

    metadata_path.write_bytes(_json_bytes(metadata))
    expected_files = {
        PurePosixPath("documents.json"),
        PurePosixPath("bm25_metadata.json"),
    }
    try:
        normalized_tree = capture_directory_ownership(root)
        fingerprint_reader = _OwnedViewReader(root, normalized_tree)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "portable BM25 view could not be captured after normalization"
        ) from exc
    try:
        _assert_exact_view_tree(
            ownership=normalized_tree,
            allowed_files=expected_files,
            required_files=expected_files,
            label="portable BM25 view",
        )
        fingerprints = {
            "documents.json": fingerprint_reader.fingerprint(
                documents_path,
                max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
            ),
            "bm25_metadata.json": fingerprint_reader.fingerprint(
                metadata_path,
                max_bytes=_MAX_CONFIG_JSON_BYTES,
            ),
        }
        fingerprint_reader.verify_root()
        try:
            final_tree = capture_directory_ownership(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "portable BM25 view changed while recording its fingerprints"
            ) from exc
        if final_tree != normalized_tree:
            raise RuntimeError(
                "portable BM25 view changed while recording its fingerprints"
            )
    finally:
        fingerprint_reader.close()
    return {"artifact_file_fingerprints": fingerprints}


def normalize_owned_query_view(
    root: Path,
    *,
    repo_path: Path,
    view_type: str,
    view_config: Mapping[str, Any],
    source_trust: SourceTrust = "portable-inert",
    native_index_authorization: NativeIndexAuthorization | None = None,
) -> dict[str, Any]:
    """Normalize one copied view and return its portable identity adjustments.

    ``root`` must be a caller-owned copy that can be safely rewritten.
    ``source_trust`` is descriptive compatibility metadata, not authority.
    Native FAISS or legacy pickle parsing requires a process-local authorization
    bound to the initial captured tree and this exact view config.
    """

    if not isinstance(source_trust, str) or source_trust not in {
        "portable-inert",
        "trusted-local",
    }:
        raise ValueError(f"invalid portable query view source trust: {source_trust!r}")
    if not isinstance(view_config, Mapping):
        raise ValueError(f"portable {view_type} normalization requires its view config")
    try:
        view_config_snapshot = json.loads(
            _json_bytes(dict(view_config)),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "portable query view config is not canonical JSON data"
        ) from exc
    if not isinstance(view_config_snapshot, dict):  # pragma: no cover - dict encoded
        raise ValueError("portable query view config must be a JSON object")
    if native_index_authorization is not None:
        if view_type != "vector":
            raise ValueError(
                "native index authorization is only valid for vector views"
            )
        require_native_index_authorization_preflight(
            native_index_authorization,
            view_type="vector",
        )
    normalized_root, repository = _owned_view_root(root, repo_path)
    try:
        initial_tree = capture_directory_ownership(normalized_root)
        reader = _OwnedViewReader(normalized_root, initial_tree)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("portable query view could not be captured safely") from exc
    native_authorized = False
    if native_index_authorization is not None:
        try:
            require_native_index_authorization(
                native_index_authorization,
                initial_tree,
                view_type="vector",
                semantic_contract=view_config_snapshot,
            )
        except BaseException as primary_error:
            try:
                reader.close()
            except BaseException as cleanup_error:  # noqa: B036 - preserve primary
                _annotate_secondary_error(
                    primary_error,
                    "portable query view reader cleanup also failed",
                    cleanup_error,
                )
            raise
        native_authorized = True
    try:
        if view_type == "vector":
            return _normalize_vector_view(
                normalized_root,
                repository,
                initial_tree=initial_tree,
                reader=reader,
                view_config=view_config_snapshot,
                native_authorized=native_authorized,
            )
        if view_type == "bm25":
            return _normalize_bm25_view(
                normalized_root,
                repository,
                initial_tree=initial_tree,
                reader=reader,
            )
        raise ValueError(
            f"view {view_type!r} is not yet supported by portable query views; "
            "select bm25 and/or vector"
        )
    finally:
        reader.close()


def _validate_normalized_document_sources(
    path: Path,
    repo_path: Path,
    *,
    view_type: str,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    reader: _ViewReader | None = None,
    authenticated_source_files: frozenset[str] | None = None,
) -> None:
    if authenticated_source_files is not None:
        if not isinstance(reader, _PublicationViewReader):
            raise RuntimeError(
                "authenticated source validation requires a publication reader"
            )
        _require_content_bound_canonical_documents(
            path,
            repo_path,
            reader=reader,
            view_type=view_type,
            forbidden_paths=forbidden_paths,
            environ=environ,
            authenticated_source_files=authenticated_source_files,
        )
        return
    documents = _load_json(
        path,
        label=f"portable {view_type} documents",
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        require_canonical=True,
        reader=reader,
    )
    if not isinstance(documents, list):
        raise ValueError(f"portable {view_type} documents must be a JSON list: {path}")
    for index, document in enumerate(documents):
        if not isinstance(document, dict) or set(document) != {
            "page_content",
            "metadata",
        }:
            raise ValueError(
                f"portable {view_type} document {index} has an invalid shape"
            )
        if not isinstance(document["page_content"], str) or not isinstance(
            document["metadata"], dict
        ):
            raise ValueError(
                f"portable {view_type} document {index} has invalid content or metadata"
            )
        metadata = document["metadata"]
        assert_no_secret_fields(
            metadata,
            source=f"portable {view_type} document {index} metadata",
        )
        assert_publishable_json_value(
            metadata,
            forbidden_paths=forbidden_paths,
            environ=environ,
            label=f"portable {view_type} document {index} metadata",
        )
        raw_file = metadata.get("file")
        if raw_file is not None and not isinstance(raw_file, str):
            raise ValueError(
                f"portable {view_type} document {index} has an invalid source path"
            )
        if raw_file is not None:
            normalized_file = _portable_source_path(
                raw_file,
                repo_path,
                source=f"portable {view_type} document {index} file",
                authenticated_source_files=authenticated_source_files,
            )
            if raw_file != normalized_file:
                raise ValueError(
                    f"portable {view_type} document {index} file is not normalized"
                )


def _authenticate_vector_generation(
    reader: _ViewReader,
    root: Path,
    *,
    model_suffix: str,
    expected_config: object | None,
    require_canonical: bool = True,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    config_path = root / f"config_{model_suffix}.json"
    if expected_config is not None:
        try:
            reader.authenticate(
                config_path,
                expected_config,
                cache_bytes=True,
                max_bytes=_MAX_CONFIG_JSON_BYTES,
            )
        except ValueError as exc:
            raise ValueError(
                "portable vector config does not match its manifest fingerprint"
            ) from exc
    config = _load_json_object(
        config_path,
        label="portable vector config",
        require_canonical=require_canonical,
        reader=reader,
    )
    if config.get("persistence_schema") != VECTOR_PERSISTENCE_SCHEMA:
        raise ValueError("portable vector generation has an invalid persistence schema")
    committed = config.get("level_artifacts")
    if not isinstance(committed, Mapping) or not _mapping_keys_are_subset(
        committed,
        set(_VECTOR_LEVELS),
        check_cancelled=check_cancelled,
    ):
        raise ValueError("portable vector generation has invalid committed levels")
    for level in _VECTOR_LEVELS:
        count = config.get(f"{level}_documents")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"portable vector generation has invalid {level} count")
        records = committed.get(level)
        if count == 0:
            if records is not None:
                raise ValueError(
                    f"portable vector generation commits empty {level} artifacts"
                )
            continue
        if not isinstance(records, Mapping) or not _mapping_has_exact_keys(
            records,
            {"index", "documents"},
            check_cancelled=check_cancelled,
        ):
            raise ValueError(
                f"portable vector generation has invalid {level} artifacts"
            )
        index_name = f"index_{model_suffix}.faiss"
        json_documents_name = f"documents_{model_suffix}.json"
        allowed_documents_names = {json_documents_name}
        if not require_canonical:
            allowed_documents_names.add(f"documents_{model_suffix}.pkl")
        index_record = records["index"]
        documents_record = records["documents"]
        if (
            not isinstance(index_record, Mapping)
            or index_record.get("file") != index_name
        ):
            raise ValueError(f"portable vector generation has invalid {level} index")
        if (
            not isinstance(documents_record, Mapping)
            or documents_record.get("file") not in allowed_documents_names
        ):
            raise ValueError(
                f"portable vector generation has invalid {level} documents"
            )
        try:
            reader.authenticate(
                root / level / str(documents_record["file"]),
                documents_record,
                cache_bytes=True,
                max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
            )
        except ValueError as exc:
            raise ValueError(
                f"committed documents vector artifact is invalid in {level}"
            ) from exc
        reader.authenticate(
            root / level / index_name,
            index_record,
            cache_bytes=False,
            max_bytes=MAX_PORTABLE_FAISS_INDEX_BYTES,
            keep_descriptor=True,
        )
    return config


def _validate_portable_bm25_view(
    root: Path,
    repository: Path,
    *,
    view_config: Mapping[str, Any],
    forbidden: tuple[Path, ...],
    environment: Mapping[str, str],
    initial_tree: object,
    reader: _ViewReader,
    authenticated_source_files: frozenset[str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    _assert_no_secret_fields_interruptibly(
        view_config,
        source="portable BM25 view config",
        check_cancelled=check_cancelled,
    )
    fingerprints = view_config.get("artifact_file_fingerprints")
    if not isinstance(fingerprints, Mapping) or not _mapping_has_exact_keys(
        fingerprints,
        {"documents.json", "bm25_metadata.json"},
        check_cancelled=check_cancelled,
    ):
        raise ValueError(
            "portable BM25 validation requires complete artifact file fingerprints"
        )
    expected_files = {
        PurePosixPath("documents.json"),
        PurePosixPath("bm25_metadata.json"),
    }
    _assert_exact_view_tree(
        ownership=initial_tree,
        allowed_files=expected_files,
        required_files=expected_files,
        label="portable BM25 view",
        check_cancelled=check_cancelled,
    )
    reader.authenticate(
        root / "documents.json",
        fingerprints["documents.json"],
        cache_bytes=True,
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    )
    reader.authenticate(
        root / "bm25_metadata.json",
        fingerprints["bm25_metadata.json"],
        cache_bytes=True,
        max_bytes=_MAX_CONFIG_JSON_BYTES,
    )
    metadata = _load_json_object(
        root / "bm25_metadata.json",
        label="portable BM25 metadata",
        require_canonical=True,
        reader=reader,
    )
    _assert_no_secret_fields_interruptibly(
        metadata,
        source="portable BM25 metadata",
        check_cancelled=check_cancelled,
    )
    if authenticated_source_files is None:
        assert_publishable_json_value(
            metadata,
            forbidden_paths=forbidden,
            environ=environment,
            label="portable BM25 metadata",
            check_cancelled=check_cancelled,
        )
    else:
        _assert_authenticated_publishable_json_value(
            metadata,
            forbidden_paths=forbidden,
            environ=environment,
            label="portable BM25 metadata",
            check_cancelled=check_cancelled,
        )
    if not _mapping_has_exact_keys(
        metadata,
        {"project_root", "max_k", "language"},
        check_cancelled=check_cancelled,
    ):
        raise ValueError("portable BM25 metadata has an invalid normalized shape")
    if metadata.get("project_root") != "source":
        raise ValueError("portable BM25 metadata project_root is not normalized")
    if (
        isinstance(metadata.get("max_k"), bool)
        or not isinstance(metadata.get("max_k"), int)
        or metadata["max_k"] <= 0
        or not isinstance(metadata.get("language"), str)
        or len(metadata["language"]) > 256
        or not metadata["language"].strip()
        or "\x00" in metadata["language"]
    ):
        raise ValueError("portable BM25 metadata is invalid")
    configured_max_k = view_config.get("max_k")
    if configured_max_k is not None and (
        isinstance(configured_max_k, bool)
        or not isinstance(configured_max_k, int)
        or configured_max_k <= 0
        or metadata["max_k"] != configured_max_k
    ):
        raise ValueError("portable BM25 metadata max_k does not match its view config")
    _validate_normalized_document_sources(
        root / "documents.json",
        repository,
        view_type="BM25",
        forbidden_paths=forbidden,
        environ=environment,
        reader=reader,
        authenticated_source_files=authenticated_source_files,
    )


def _validate_portable_vector_view(
    root: Path,
    repository: Path,
    *,
    view_config: Mapping[str, Any],
    forbidden: tuple[Path, ...],
    environment: Mapping[str, str],
    initial_tree: object,
    reader: _ViewReader,
    authenticated_source_files: frozenset[str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    _assert_no_secret_fields_interruptibly(
        view_config,
        source="portable vector view config",
        check_cancelled=check_cancelled,
    )
    portable_artifact_policy = authenticated_source_files is not None
    route = _resolve_embedding_artifact_route_interruptibly(
        view_config,
        environ={} if portable_artifact_policy else None,
        check_cancelled=check_cancelled,
    )
    expected_suffix = _vector_model_suffix(
        route.model,
        check_cancelled=check_cancelled,
    )
    inventory_files = _owned_inventory_paths(
        root,
        initial_tree,
        kind="file",
        check_cancelled=check_cancelled,
    )
    root_config_items = [
        path
        for path in _interitem_cancellation(inventory_files, check_cancelled)
        if path.parent == root
        and path.name.startswith("config_")
        and path.name.endswith(".json")
    ]
    root_configs = (
        sorted(root_config_items)
        if check_cancelled is None
        else list(
            _interruptible_sorted_security_items(
                root_config_items,
                key=None,
                check_cancelled=check_cancelled,
            )
        )
    )
    if len(root_configs) != 1:
        raise ValueError("portable vector view must contain one root config")
    config_path = root_configs[0]
    model_suffix = config_path.name[len("config_") : -len(".json")]
    if not model_suffix or model_suffix != expected_suffix:
        raise ValueError("portable vector root config does not match its view model")
    _validate_vector_model_policy(
        root,
        ownership=initial_tree,
        model_suffix=model_suffix,
        native_authorized=False,
        check_cancelled=check_cancelled,
    )
    expected_config = view_config.get("persistence_config_fingerprint")
    if expected_config is None:
        raise ValueError("portable vector validation requires its config fingerprint")
    config = _authenticate_vector_generation(
        reader,
        root,
        model_suffix=model_suffix,
        expected_config=expected_config,
        check_cancelled=check_cancelled,
    )
    _assert_no_secret_fields_interruptibly(
        config,
        source="portable vector config",
        check_cancelled=check_cancelled,
    )
    if authenticated_source_files is None:
        assert_publishable_json_value(
            config,
            forbidden_paths=forbidden,
            environ=environment,
            label="portable vector config",
            check_cancelled=check_cancelled,
        )
    else:
        _assert_authenticated_publishable_json_value(
            config,
            forbidden_paths=forbidden,
            environ=environment,
            label="portable vector config",
            check_cancelled=check_cancelled,
        )
    (
        semantic_suffix,
        expected_dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
    ) = _validate_vector_semantics(
        config,
        view_config,
        portable_artifact_policy=portable_artifact_policy,
        check_cancelled=check_cancelled,
    )
    if semantic_suffix != model_suffix:
        raise ValueError("portable vector config embedding model does not match")
    document_format, _documents, counts, stale_paths = _validate_vector_layout(
        root,
        repository,
        ownership=initial_tree,
        model_suffix=model_suffix,
        config=config,
        expected_model=route.model,
        expected_provider=route.provider,
        expected_revision=expected_revision,
        expected_dimension=expected_dimension,
        expected_metric=expected_metric,
        expected_index_type=expected_index_type,
        native_authorized=False,
        canonicalize_level_configs=False,
        reader=reader,
        authenticated_source_files=authenticated_source_files,
        document_forbidden_paths=forbidden,
        document_environ=environment,
        check_cancelled=check_cancelled,
    )
    if document_format != "json" or stale_paths:
        raise ValueError("portable vector view is not in its normalized final form")
    _validate_view_document_count(
        view_config,
        counts,
        check_cancelled=check_cancelled,
    )
    _assert_normalized_vector_tree(
        ownership=initial_tree,
        model_suffix=model_suffix,
        counts=counts,
        check_cancelled=check_cancelled,
    )
    for level in _VECTOR_LEVELS:
        if counts[level] <= 0:
            continue
        level_root = root / level
        level_config = level_root / f"config_{model_suffix}.json"
        if level_config in inventory_files:
            level_value = _load_json_object(
                level_config,
                label=f"portable vector {level} config",
                require_canonical=True,
                reader=reader,
            )
            if authenticated_source_files is None:
                assert_publishable_json_value(
                    level_value,
                    forbidden_paths=forbidden,
                    environ=environment,
                    label=f"portable vector {level} config",
                    check_cancelled=check_cancelled,
                )
            else:
                _assert_authenticated_publishable_json_value(
                    level_value,
                    forbidden_paths=forbidden,
                    environ=environment,
                    label=f"portable vector {level} config",
                    check_cancelled=check_cancelled,
                )
        if authenticated_source_files is None:
            _validate_normalized_document_sources(
                level_root / f"documents_{model_suffix}.json",
                repository,
                view_type=f"vector {level}",
                forbidden_paths=forbidden,
                environ=environment,
                reader=reader,
            )


def _exact_repository_identity_value(
    value: object,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> object:
    if type(value) is tuple:
        if check_cancelled is None:
            return tuple(_exact_repository_identity_value(item) for item in value)
        detached: list[object] = []
        item_count = len(value)
        for index in range(item_count):
            item = value[index]
            if not (
                type(item) is tuple
                or item is None
                or type(item) in {bool, int, str, bytes}
            ):
                raise TypeError(
                    "portable repository identity contains a non-exact value"
                )
            if type(item) is tuple:
                check_cancelled()
                item = _exact_repository_identity_value(
                    item,
                    check_cancelled=check_cancelled,
                )
            detached.append(item)
            if index + 1 < item_count:
                check_cancelled()
        return tuple(detached)
    if value is None or type(value) in {bool, int, str, bytes}:
        return value
    raise TypeError("portable repository identity contains a non-exact value")


def _repository_identity_values_equal_interruptibly(
    left: object,
    right: object,
    *,
    check_cancelled: Callable[[], None],
) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is tuple:
        assert type(right) is tuple
        if len(left) != len(right):
            return False
        item_count = len(left)
        for index in range(item_count):
            left_item = left[index]
            right_item = right[index]
            if type(left_item) is not type(right_item):
                return False
            if type(left_item) is tuple:
                check_cancelled()
                matches = _repository_identity_values_equal_interruptibly(
                    left_item,
                    right_item,
                    check_cancelled=check_cancelled,
                )
            else:
                matches = _repository_identity_values_equal_interruptibly(
                    left_item,
                    right_item,
                    check_cancelled=check_cancelled,
                )
            if not matches:
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) in {str, bytes}:
        if left is right:
            return True
        if len(left) != len(right):  # type: ignore[arg-type]
            return False
        value_length = len(left)  # type: ignore[arg-type]
        for offset in range(0, value_length, _SEMANTIC_SCAN_CHARS):
            end = min(value_length, offset + _SEMANTIC_SCAN_CHARS)
            if left[offset:end] != right[offset:end]:  # type: ignore[index]
                return False
            if end < value_length:
                check_cancelled()
        return True
    return bool(left == right)


def _detach_repository_source_selection(
    selection: RepositorySourceSelection | None,
    *,
    check_cancelled: Callable[[], None] | None,
) -> RepositorySourceSelection | None:
    if selection is None:
        return None
    values = selection.exclude_subtrees
    if type(values) is not tuple:
        raise TypeError(
            "portable repository source selection fields must use exact types"
        )
    if check_cancelled is None:
        if any(type(path) is not str for path in values):
            raise TypeError(
                "portable repository source selection fields must use exact types"
            )
        return RepositorySourceSelection(values)

    if len(values) > 4096:
        raise ValueError("portable repository source selection has too many exclusions")
    detached: list[str] = []
    selected: set[str] = set()
    total_bytes = 0
    previous: str | None = None
    value_count = len(values)
    for index in range(value_count):
        value = values[index]
        if type(value) is not str:
            raise TypeError(
                "portable repository source selection fields must use exact types"
            )
        if len(value) > 4096:
            raise ValueError("portable repository source selection is not canonical")
        path = PurePosixPath(value)
        encoded = value.encode("utf-8", errors="strict")
        if (
            not value
            or "\x00" in value
            or "\\" in value
            or path.is_absolute()
            or value in {".", ".."}
            or ".." in path.parts
            or path.as_posix() != value
            or len(encoded) > 4096
            or len(path.parts) > 256
            or (previous is not None and value <= previous)
            or any(
                "/".join(path.parts[:component_count]) in selected
                for component_count in range(1, len(path.parts))
            )
        ):
            raise ValueError("portable repository source selection is not canonical")
        total_bytes += len(encoded)
        if total_bytes > 1024 * 1024:
            raise ValueError("portable repository source selection is too large")
        detached.append(value)
        selected.add(value)
        previous = value
        if index + 1 < value_count:
            check_cancelled()

    snapshot = RepositorySourceSelection()
    object.__setattr__(snapshot, "exclude_subtrees", tuple(detached))
    object.__setattr__(snapshot, "_exclude_index", frozenset(selected))
    return snapshot


def _detach_repository_identity_snapshot(
    repository_identity: RepositorySourceIdentitySnapshot,
    *,
    check_cancelled: Callable[[], None] | None = None,
    retained_identity: RepositorySourceIdentitySnapshot | None = None,
) -> RepositorySourceIdentitySnapshot:
    """Validate and detach every caller-controlled identity field."""

    if type(repository_identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("portable view repository identity has an invalid type")
    if (
        type(repository_identity.root) is not type(Path())
        or type(repository_identity.fingerprint) is not str
        or type(repository_identity.file_count) is not int
        or type(repository_identity.file_records) is not tuple
        or (
            repository_identity.source_selection is not None
            and type(repository_identity.source_selection)
            is not RepositorySourceSelection
        )
    ):
        raise TypeError("portable repository identity fields must use exact types")
    if (
        repository_identity.file_count < 0
        or not is_secure_source_fingerprint_v2(repository_identity.fingerprint)
        or repository_identity.file_count != len(repository_identity.file_records)
    ):
        raise ValueError("portable repository identity is invalid")
    if retained_identity is not None and (
        repository_identity.root != retained_identity.root
        or repository_identity.fingerprint != retained_identity.fingerprint
        or repository_identity.file_count != retained_identity.file_count
        or len(repository_identity.file_records) != len(retained_identity.file_records)
    ):
        raise ValueError(
            "portable repository identity differs from its retained binding"
        )

    selection = _detach_repository_source_selection(
        repository_identity.source_selection,
        check_cancelled=check_cancelled,
    )
    records: list[RepositorySourceFileRecord] = []
    paths: set[str] = set()
    record_count = len(repository_identity.file_records)
    for index in range(record_count):
        record = repository_identity.file_records[index]
        if type(record) is not RepositorySourceFileRecord:
            raise TypeError(
                "portable repository records must use the exact record type"
            )
        if (
            type(record.path) is not str
            or type(record.size) is not int
            or type(record.sha256) is not str
            or type(record.lexical_identity) is not tuple
            or (record.link_target is not None and type(record.link_target) is not str)
        ):
            raise TypeError("portable repository record fields must use exact types")
        if (
            not record.path
            or len(record.path) > _MAX_SOURCE_PATH_BYTES
            or (
                record.link_target is not None
                and len(record.link_target) > _MAX_SOURCE_PATH_BYTES
            )
            or record.size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", record.sha256, re.ASCII)
        ):
            raise ValueError("portable repository record identity is invalid")
        if retained_identity is not None:
            retained_record = retained_identity.file_records[index]
            if (
                record.path != retained_record.path
                or record.size != retained_record.size
                or record.sha256 != retained_record.sha256
                or record.link_target != retained_record.link_target
            ):
                raise ValueError(
                    "portable repository identity differs from its retained binding"
                )
        if record.path in paths:
            raise RuntimeError("authenticated repository source repeats a file record")
        identity = _exact_repository_identity_value(
            record.lexical_identity,
            check_cancelled=check_cancelled,
        )
        detached = RepositorySourceFileRecord(
            path=record.path,
            size=record.size,
            sha256=record.sha256,
            lexical_identity=identity,  # type: ignore[arg-type]
            link_target=record.link_target,
        )
        paths.add(detached.path)
        records.append(detached)
        if check_cancelled is not None and index + 1 < record_count:
            check_cancelled()

    return RepositorySourceIdentitySnapshot(
        root=type(repository_identity.root)(os.fspath(repository_identity.root)),
        fingerprint=repository_identity.fingerprint,
        file_count=repository_identity.file_count,
        file_records=tuple(records),
        source_selection=selection,
    )


def _require_repository_identity_matches(
    repository_identity: RepositorySourceIdentitySnapshot,
    retained_identity: RepositorySourceIdentitySnapshot,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    if (
        repository_identity.root != retained_identity.root
        or repository_identity.fingerprint != retained_identity.fingerprint
        or repository_identity.file_count != retained_identity.file_count
        or len(repository_identity.file_records) != len(retained_identity.file_records)
    ):
        raise ValueError(
            "portable repository identity differs from its retained binding"
        )
    selection = repository_identity.source_selection
    retained_selection = retained_identity.source_selection
    if selection is None or retained_selection is None:
        if selection is not retained_selection:
            raise ValueError(
                "portable repository identity differs from its retained binding"
            )
    else:
        selected = selection.exclude_subtrees
        retained = retained_selection.exclude_subtrees
        if len(selected) != len(retained):
            raise ValueError(
                "portable repository identity differs from its retained binding"
            )
        selection_count = len(selected)
        for index in range(selection_count):
            if selected[index] != retained[index]:
                raise ValueError(
                    "portable repository identity differs from its retained binding"
                )
            if check_cancelled is not None and index + 1 < selection_count:
                check_cancelled()
    record_count = len(repository_identity.file_records)
    for index in range(record_count):
        current_record = repository_identity.file_records[index]
        retained_record = retained_identity.file_records[index]
        if check_cancelled is None:
            matches = current_record == retained_record
        else:
            matches = (
                current_record.path == retained_record.path
                and current_record.size == retained_record.size
                and current_record.sha256 == retained_record.sha256
                and current_record.link_target == retained_record.link_target
                and _repository_identity_values_equal_interruptibly(
                    current_record.lexical_identity,
                    retained_record.lexical_identity,
                    check_cancelled=check_cancelled,
                )
            )
        if not matches:
            raise ValueError(
                "portable repository identity differs from its retained binding"
            )
        if check_cancelled is not None and index + 1 < record_count:
            check_cancelled()


def _attest_repository_identity_snapshot(
    repository_identity: RepositorySourceIdentitySnapshot,
    retained_identity: RepositorySourceIdentitySnapshot,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> RepositorySourceIdentitySnapshot:
    """Detach and match a supplied identity against retained binding authority."""

    detached = _detach_repository_identity_snapshot(
        repository_identity,
        check_cancelled=check_cancelled,
        retained_identity=retained_identity,
    )
    _require_repository_identity_matches(
        detached,
        retained_identity,
        check_cancelled=check_cancelled,
    )
    return detached


def _authenticated_repository_source_files(
    repository_identity: RepositorySourceIdentitySnapshot,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> frozenset[str]:
    records = repository_identity.file_records
    record_count = len(records)
    paths: set[str] = set()
    for index in range(record_count):
        record = records[index]
        if type(record) is not RepositorySourceFileRecord or (
            type(record.path) is not str
            or type(record.size) is not int
            or type(record.sha256) is not str
            or type(record.lexical_identity) is not tuple
            or (record.link_target is not None and type(record.link_target) is not str)
        ):
            raise TypeError("authenticated repository source record fields are invalid")
        if (
            not record.path
            or len(record.path) > _MAX_SOURCE_PATH_BYTES
            or (
                record.link_target is not None
                and len(record.link_target) > _MAX_SOURCE_PATH_BYTES
            )
            or record.size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", record.sha256, re.ASCII)
        ):
            raise ValueError("authenticated repository source record is invalid")
        path = record.path
        if path in paths:
            raise RuntimeError("authenticated repository source repeats a file record")
        _exact_repository_identity_value(
            record.lexical_identity,
            check_cancelled=check_cancelled,
        )
        paths.add(path)
        if check_cancelled is not None and index + 1 < record_count:
            check_cancelled()
    return frozenset(paths)


def _validate_content_bound_portable_query_view_reader_with_identity_impl(
    publication: PublicationDirectoryReader,
    *,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_type: str,
    view_config: Mapping[str, Any] | None = None,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    _framework_sandwiched: bool = False,
) -> None:
    """Validate one portable view through retained view and source authorities.

    Document source paths are matched only against the repository binding's
    frozen records. Vector native indexes remain opaque authenticated bytes.
    """

    if type(publication) is not PublicationDirectoryReader:
        raise TypeError("portable view requires a publication directory reader")
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("portable view repository source has an invalid type")
    if type(repository_identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("portable view repository identity has an invalid type")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("portable view cancellation check must be callable")
    if view_type not in {"bm25", "vector"}:
        raise ValueError(f"unsupported portable query view: {view_type!r}")
    if not isinstance(view_config, Mapping):
        raise ValueError(f"portable {view_type} validation requires its view config")

    if not repository_source.usable:
        raise RuntimeError("portable view repository source is not usable")
    if check_cancelled is not None:
        retained_identity = repository_source.authenticated_identity_snapshot(
            check_cancelled=check_cancelled,
        )
        repository_identity = _attest_repository_identity_snapshot(
            repository_identity,
            retained_identity,
            check_cancelled=check_cancelled,
        )

    if check_cancelled is None:
        config_snapshot = _bounded_json_object_snapshot(
            view_config,
            label=f"portable {view_type} view config",
        )
    else:
        config_snapshot = _bounded_json_object_snapshot(
            view_config,
            label=f"portable {view_type} view config",
            check_cancelled=check_cancelled,
        )
    if view_type == "vector":
        route = _resolve_embedding_artifact_route_interruptibly(
            config_snapshot,
            environ={},
            check_cancelled=check_cancelled,
        )
        if route.provider == "huggingface":
            route_options = _route_compatibility_options_interruptibly(
                route,
                check_cancelled,
            )
            if check_cancelled is None:
                resolve_embedding_artifact_load_policy_from_options(
                    route.model,
                    route_options,
                )
            else:
                resolve_embedding_artifact_load_policy_from_options(
                    route.model,
                    route_options,
                    check_cancelled=check_cancelled,
                )
    if check_cancelled is None:
        environment = _environment_snapshot(environ)
        forbidden = _forbidden_paths_snapshot(forbidden_paths)
    else:
        environment = _environment_snapshot(
            environ,
            check_cancelled=check_cancelled,
        )
        forbidden = _forbidden_paths_snapshot(
            forbidden_paths,
            check_cancelled=check_cancelled,
        )
    authenticated_source_files = (
        _authenticated_repository_source_files(repository_identity)
        if check_cancelled is None
        else _authenticated_repository_source_files(
            repository_identity,
            check_cancelled=check_cancelled,
        )
    )

    if not isinstance(_framework_sandwiched, bool):
        raise TypeError("portable framework sandwich selector must be a boolean")
    if check_cancelled is not None:
        check_cancelled()
    if _framework_sandwiched:
        reader = _PublicationViewReader(
            publication,
            None,
            check_cancelled=check_cancelled,
        )
        initial_tree: object = reader
    else:
        initial_tree = publication.capture_ownership(
            check_cancelled=check_cancelled,
        )
        reader = _PublicationViewReader(
            publication,
            initial_tree,
            check_cancelled=check_cancelled,
        )
    if check_cancelled is not None:
        check_cancelled()

    def validate_semantics() -> None:
        if check_cancelled is not None:
            check_cancelled()
        source_session = (
            repository_source.read_session()
            if check_cancelled is None
            else repository_source.read_session(check_cancelled=check_cancelled)
        )
        with source_session:
            policy_paths = (repository_identity.root, *forbidden)
            _assert_authenticated_publishable_json_value(
                config_snapshot,
                forbidden_paths=policy_paths,
                environ=environment,
                label=f"portable {view_type} view config",
                check_cancelled=check_cancelled,
            )
            if view_type == "bm25":
                _validate_portable_bm25_view(
                    reader.root,
                    repository_identity.root,
                    view_config=config_snapshot,
                    forbidden=policy_paths,
                    environment=environment,
                    initial_tree=initial_tree,
                    reader=reader,
                    authenticated_source_files=authenticated_source_files,
                    check_cancelled=check_cancelled,
                )
            else:
                _validate_portable_vector_view(
                    reader.root,
                    repository_identity.root,
                    view_config=config_snapshot,
                    forbidden=policy_paths,
                    environment=environment,
                    initial_tree=initial_tree,
                    reader=reader,
                    authenticated_source_files=authenticated_source_files,
                    check_cancelled=check_cancelled,
                )

    final_checks = [
        (
            "portable view reader cleanup also failed",
            reader.close,
        )
    ]
    if not _framework_sandwiched:
        final_checks.insert(
            0,
            (
                "portable view final ownership validation also failed",
                reader.verify_root,
            ),
        )
    _run_callback_with_post_validations(
        validate_semantics,
        tuple(final_checks),
    )


def _validate_content_bound_portable_query_view_reader_with_identity(
    publication: PublicationDirectoryReader,
    *,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_type: str,
    view_config: Mapping[str, Any] | None = None,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    _framework_sandwiched: bool = False,
) -> None:
    if check_cancelled is None or not callable(check_cancelled):
        _validate_content_bound_portable_query_view_reader_with_identity_impl(
            publication,
            repository_source=repository_source,
            repository_identity=repository_identity,
            view_type=view_type,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
            check_cancelled=check_cancelled,
            _framework_sandwiched=_framework_sandwiched,
        )
        return

    iteration_error: StopIteration | StopAsyncIteration | None = None
    iteration_carrier: _CallbackIterationStop | None = None

    def preserve_iteration_stop() -> None:
        nonlocal iteration_error, iteration_carrier
        try:
            check_cancelled()
        except (StopIteration, StopAsyncIteration) as error:
            if error is not iteration_error:
                iteration_error = error
                iteration_carrier = _CallbackIterationStop(error)
            assert iteration_carrier is not None
            raise iteration_carrier from None

    try:
        _validate_content_bound_portable_query_view_reader_with_identity_impl(
            publication,
            repository_source=repository_source,
            repository_identity=repository_identity,
            view_type=view_type,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
            check_cancelled=preserve_iteration_stop,
            _framework_sandwiched=_framework_sandwiched,
        )
    except _CallbackIterationStop as failure:
        if failure is not iteration_carrier:
            raise
        _transfer_callback_exception_settlement(failure, failure.error)
        raise failure.error from None


def validate_content_bound_portable_query_view_reader(
    publication: PublicationDirectoryReader,
    *,
    repository_source: RepositorySourceBinding,
    view_type: str,
    view_config: Mapping[str, Any] | None = None,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> None:
    """Validate one portable view through retained view and source authorities."""

    if type(publication) is not PublicationDirectoryReader:
        raise TypeError("portable view requires a publication directory reader")
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("portable view repository source has an invalid type")
    if view_type not in {"bm25", "vector"}:
        raise ValueError(f"unsupported portable query view: {view_type!r}")
    if not isinstance(view_config, Mapping):
        raise ValueError(f"portable {view_type} validation requires its view config")
    repository_identity = repository_source.authenticated_identity_snapshot()
    if type(repository_identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("portable view repository identity has an invalid type")
    _validate_content_bound_portable_query_view_reader_with_identity(
        publication,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_type=view_type,
        view_config=view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )


def validate_portable_query_view(
    root: Path,
    *,
    repo_path: Path,
    view_type: str,
    view_config: Mapping[str, Any] | None = None,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> None:
    """Revalidate a normalized query view without mutating its bytes."""

    normalized_root, repository = _owned_view_root(root, repo_path)
    if view_type not in {"bm25", "vector"}:
        raise ValueError(f"unsupported portable query view: {view_type!r}")
    if not isinstance(view_config, Mapping):
        raise ValueError(f"portable {view_type} validation requires its view config")
    try:
        initial_tree = capture_directory_ownership(normalized_root)
        reader = _OwnedViewReader(normalized_root, initial_tree)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"portable {view_type} view could not be captured safely"
        ) from exc
    environment = os.environ if environ is None else environ
    forbidden = (repository, *tuple(forbidden_paths))
    try:
        if view_type == "bm25":
            _validate_portable_bm25_view(
                normalized_root,
                repository,
                view_config=view_config,
                forbidden=forbidden,
                environment=environment,
                initial_tree=initial_tree,
                reader=reader,
            )
        else:
            _validate_portable_vector_view(
                normalized_root,
                repository,
                view_config=view_config,
                forbidden=forbidden,
                environment=environment,
                initial_tree=initial_tree,
                reader=reader,
            )
        reader.verify_root()
        try:
            final_tree = capture_directory_ownership(normalized_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"portable {view_type} view changed during semantic validation"
            ) from exc
        if final_tree != initial_tree:
            raise RuntimeError(
                f"portable {view_type} view changed during semantic validation"
            )
    finally:
        reader.close()


__all__ = [
    "MAX_PORTABLE_DOCUMENTS_JSON_BYTES",
    "MAX_PORTABLE_FAISS_INDEX_BYTES",
    "SourceTrust",
    "normalize_owned_query_view",
    "validate_content_bound_portable_query_view_reader",
    "validate_portable_query_view",
    "validate_portable_vector_persistence_semantics",
]
