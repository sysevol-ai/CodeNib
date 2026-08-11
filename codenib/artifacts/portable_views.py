# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Normalize owned query views into portable, query-only artifacts."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal, Mapping

from .. import compat_pickle
from .._atomic_directory import (
    _annotate_secondary_error,
    capture_directory_ownership,
    directory_ownership_file_records,
    directory_ownership_inventory,
    directory_ownership_root_identity,
)
from .._contained_source import validate_repository_file
from .._secret_fields import assert_no_secret_fields
from ..index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_VIEW_UPDATE_MARKER,
)
from ..index.embedding.model_policy import resolve_embedding_load_policy_from_options
from ..native_index_authorization import (
    MissingNativeIndexAuthorizationError,
    NativeIndexAuthorization,
    require_native_index_authorization,
    require_native_index_authorization_preflight,
)
from ..provider_routes import normalize_provider, resolve_embedding_artifact_route
from .security import assert_publishable_json_value

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
_MAX_DOCUMENTS_JSON_BYTES = 256 * 1024 * 1024
_MAX_FAISS_INDEX_BYTES = 8 * 1024 * 1024 * 1024
_JSON_READ_CHUNK_BYTES = 1024 * 1024
SourceTrust = Literal["portable-inert", "trusted-local"]


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

    def _open_file(self, relative: PurePosixPath) -> tuple[int, os.stat_result]:
        directory_descriptor = os.dup(self._descriptor)
        source_descriptor = -1
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
        except OSError as exc:
            raise ValueError(
                f"portable view file is not safely readable: {relative}"
            ) from exc
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            os.close(directory_descriptor)

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


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


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


def _read_bounded_json(path: Path, *, label: str, max_bytes: int) -> bytearray:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError(f"{label} byte limit must be positive")
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
    reader: _OwnedViewReader | None = None,
) -> Any:
    payload = (
        _read_bounded_json(path, label=label, max_bytes=max_bytes)
        if reader is None
        else reader.read_bytes(path, max_bytes=max_bytes)
    )
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
        )
    except ValueError as exc:
        raise ValueError(f"{label} is invalid JSON: {path}") from exc
    if require_canonical and payload != _json_bytes(value):
        raise ValueError(f"{label} is not canonical JSON: {path}")
    return value


def _load_json_object(
    path: Path,
    *,
    label: str,
    max_bytes: int = _MAX_CONFIG_JSON_BYTES,
    require_canonical: bool = False,
    reader: _OwnedViewReader | None = None,
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


def _portable_source_path(value: object, repo_path: Path, *, source: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(
            f"{source} is not a portable repository-relative path: {raw!r}"
        )

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
    ):
        raise ValueError(f"{source} is not repository-relative: {raw}")
    try:
        validate_repository_file(repo_path, normalized.as_posix())
    except ValueError as exc:
        raise ValueError(
            f"{source} is not a stable contained source file: {raw}"
        ) from exc
    return normalized.as_posix()


def _normalize_pickle_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _OwnedViewReader,
) -> list[dict[str, Any]]:
    """Load documents from an explicitly trusted, machine-local pickle."""

    try:
        payload = reader.read_bytes(path, max_bytes=_MAX_DOCUMENTS_JSON_BYTES)
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


def _owned_inventory_paths(root: Path, ownership: object, *, kind: str) -> set[Path]:
    return {
        root.joinpath(*PurePosixPath(relative).parts)
        for relative, observed_kind in directory_ownership_inventory(ownership)
        if observed_kind == kind
    }


def _authenticate_initial_file(
    reader: _OwnedViewReader,
    root: Path,
    ownership: object,
    path: Path,
    *,
    max_bytes: int,
    cache_bytes: bool = False,
    keep_descriptor: bool = False,
) -> None:
    """Bind one later read/parser to the bytes in the initial tree capture."""

    relative = path.relative_to(root).as_posix()
    record = next(
        (
            item
            for item in directory_ownership_file_records(ownership)
            if item.path == relative
        ),
        None,
    )
    if record is None:
        raise ValueError(f"portable vector artifact is missing: {relative}")
    reader.authenticate(
        path,
        {"size": record.size, "sha256": record.sha256},
        cache_bytes=cache_bytes,
        max_bytes=max_bytes,
        keep_descriptor=keep_descriptor,
    )


def _pickle_paths(root: Path, ownership: object) -> list[Path]:
    return sorted(
        path
        for path in _owned_inventory_paths(root, ownership, kind="file")
        if path.suffix.casefold() in _PICKLE_SUFFIXES
    )


def _reject_inert_pickles(root: Path, ownership: object) -> None:
    pickles = _pickle_paths(root, ownership)
    if pickles:
        raise ValueError(
            "portable-inert query view must not contain pickle: "
            f"{pickles[0].relative_to(root)}"
        )


def _normalize_json_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _OwnedViewReader | None = None,
) -> list[dict[str, Any]]:
    documents = _load_json(
        path,
        label="portable vector documents",
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
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
        )
        payload.append(
            {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
        )
    return payload


def _normalize_bm25_documents(
    path: Path,
    repo_path: Path,
    *,
    reader: _OwnedViewReader,
) -> None:
    documents = _load_json(
        path,
        label="portable BM25 documents",
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
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
    inventory_files = _owned_inventory_paths(root, ownership, kind="file")
    unexpected_model_artifacts = sorted(
        path
        for path in inventory_files
        if path.name.casefold().startswith(("config_", "documents_", "index_"))
        and path not in allowed_model_artifacts
    )
    if unexpected_model_artifacts:
        raise ValueError(
            "portable vector view contains an unknown or other-model artifact: "
            f"{unexpected_model_artifacts[0].relative_to(root)}"
        )

    pickles = _pickle_paths(root, ownership)
    if not native_authorized:
        _reject_inert_pickles(root, ownership)
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
) -> tuple[str, int, str | None, str, str]:
    """Close the manifest-route to persisted-config compatibility contract."""

    route = resolve_embedding_artifact_route(view_config)
    expected_revision = (
        resolve_embedding_load_policy_from_options(
            route.model,
            route.compatibility_options,
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
        if config.get(key) != expected:
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
    try:
        provider = normalize_provider(str(config.get("embedding_provider", "")))
    except ValueError as exc:
        raise ValueError(
            "portable vector persistence has an invalid embedding provider"
        ) from exc
    if provider != route.provider:
        raise ValueError(
            "portable vector persistence embedding provider does not match "
            "its view route"
        )

    expected_metric = view_config.get("index_metric", "ip")
    if expected_metric not in {"ip", "l2"}:
        raise ValueError(
            f"portable vector view has unsupported index metric: {expected_metric!r}"
        )
    persisted_metric = config.get("index_metric")
    if persisted_metric != expected_metric:
        raise ValueError(
            "portable vector persistence index metric does not match its view config"
        )

    expected_index_type = view_config.get("index_type", "flat")
    if expected_index_type not in {"flat", "ivf"}:
        raise ValueError(
            "portable vector view has unsupported index type: "
            f"{expected_index_type!r}"
        )
    if config.get("index_type") != expected_index_type:
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
        or bool(route.compatibility_options)
    )
    if persisted_identity is not None:
        if not isinstance(persisted_identity, Mapping):
            raise ValueError("portable vector persistence artifact identity is invalid")
        persisted_route = resolve_embedding_artifact_route(persisted_identity)
        if persisted_route.public_identity() != route.public_identity():
            raise ValueError(
                "portable vector persistence route identity does not match its view "
                "route"
            )
        expected_fingerprint = view_config.get("embedding_fingerprint")
        if (
            expected_fingerprint is not None
            and persisted_identity.get("embedding_fingerprint") != expected_fingerprint
        ):
            raise ValueError(
                "portable vector persistence embedding fingerprint does not match "
                "its view config"
            )
        persisted_revision = (
            resolve_embedding_load_policy_from_options(
                persisted_route.model,
                persisted_route.compatibility_options,
            ).revision
            if persisted_route.provider == "huggingface"
            else None
        )
        if persisted_revision != expected_revision:
            raise ValueError(
                "portable vector persistence artifact revision does not match its "
                "view config"
            )
        persisted_identity_metric = persisted_identity.get("index_metric")
        if persisted_identity_metric != expected_metric:
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
        persisted_route = resolve_embedding_artifact_route(config)
        if persisted_route.public_identity() != route.public_identity():
            raise ValueError(
                "portable vector persistence options do not match its view route"
            )

    if route.dimension is None:
        raise ValueError("portable vector route is missing its embedding dimension")
    return (
        route.model.replace("/", "__"),
        route.dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
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
    level: str,
    model: str,
    provider: str,
    revision: str | None,
    dimension: int,
    metric: object,
    index_type: str,
    count: int,
    present: bool,
    reader: _OwnedViewReader,
    canonicalize: bool = True,
) -> None:
    if not present:
        return
    config = _load_json_object(
        path,
        label=f"portable vector {level} config",
        require_canonical=not canonicalize,
        reader=reader,
    )
    assert_no_secret_fields(
        config,
        source=f"portable vector {level} config",
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
    if set(config) != expected_fields:
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
        if config.get(key) != expected:
            raise ValueError(
                f"portable vector {level} persistence {key} does not match"
            )
    try:
        persisted_provider = normalize_provider(
            str(config.get("embedding_provider", ""))
        )
    except ValueError as exc:
        raise ValueError(
            f"portable vector {level} persistence provider is invalid"
        ) from exc
    if persisted_provider != provider:
        raise ValueError(f"portable vector {level} persistence provider does not match")
    if canonicalize:
        path.write_bytes(_json_bytes(config))


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
    reader: _OwnedViewReader,
    canonicalize_level_configs: bool = True,
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
    inventory_files = _owned_inventory_paths(root, ownership, kind="file")

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
                max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
                cache_bytes=True,
            )
            payload = _normalize_pickle_documents(path, repo_path, reader=reader)
        else:
            payload = _normalize_json_documents(path, repo_path, reader=reader)
        if count is not None and len(payload) != count:
            raise ValueError(
                f"portable vector {level} count differs from {path.name}: "
                f"expected {count}, found {len(payload)}"
            )
        count = len(payload)
        _authenticate_initial_file(
            reader,
            root,
            ownership,
            index_path,
            max_bytes=_MAX_FAISS_INDEX_BYTES,
            keep_descriptor=native_authorized,
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
            level=level,
            model=expected_model,
            provider=expected_provider,
            revision=expected_revision,
            dimension=expected_dimension,
            metric=expected_metric,
            index_type=expected_index_type,
            count=count,
            present=level_config_path in inventory_files,
            canonicalize=canonicalize_level_configs,
            reader=reader,
        )
        formats.add(document_format)
        selected[path] = payload

    if not selected:
        raise ValueError("portable vector view has no non-empty document store")
    if len(formats) != 1:
        raise ValueError("portable vector view mixes pickle and JSON documents")

    candidates = {
        path
        for path in inventory_files
        if path.suffix.casefold() in {".json", ".pkl", ".pickle"}
        and path.name.startswith("documents_")
    }
    unexpected_documents = sorted(candidates - expected_documents)
    if unexpected_documents:
        raise ValueError(
            "portable vector view contains an unexpected document store: "
            f"{unexpected_documents[0].relative_to(root)}"
        )

    allowed_pickles = {path for path in selected if path.suffix.lower() == ".pkl"}
    allowed_pickles.update(
        path for path in stale_paths if path.suffix.casefold() in _PICKLE_SUFFIXES
    )
    allowed_pickles.update(
        path
        for path in inventory_files
        if path.parent.name in _VECTOR_LEVELS
        and path.name.startswith("index_")
        and path.suffix.casefold() == ".pkl"
    )
    allowed_pickles.update(
        root / name
        for name in _REMOVABLE_MUTABLE_VECTOR_FILES
        if name.endswith(".pkl") and (root / name) in inventory_files
    )
    residual_pickles = sorted(
        path
        for path in inventory_files
        if path.suffix.casefold() in _PICKLE_SUFFIXES and path not in allowed_pickles
    )
    if residual_pickles:
        raise ValueError(
            "portable vector view contains an unexpected pickle: "
            f"{residual_pickles[0].relative_to(root)}"
        )

    removable_mutable = {root / name for name in _REMOVABLE_MUTABLE_VECTOR_FILES}
    residual_mutable = sorted(
        path
        for path in inventory_files
        if _is_mutable_vector_state(path) and path not in removable_mutable
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
                max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
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
) -> None:
    if "document_count" not in view_config:
        return
    raw = view_config["document_count"]
    if not isinstance(raw, Mapping):
        raise ValueError("portable vector document_count must be a mapping")
    expected = {
        level: count for level in _VECTOR_LEVELS if (count := counts[level]) > 0
    }
    if set(raw) != set(expected):
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
) -> None:
    allowed_directories = {
        parent
        for path in allowed_files
        for parent in path.parents
        if parent != PurePosixPath(".")
    }
    observed_files: set[PurePosixPath] = set()
    for raw_relative, kind in directory_ownership_inventory(ownership):
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
                max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
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
    reader: _OwnedViewReader | None = None,
) -> None:
    documents = _load_json(
        path,
        label=f"portable {view_type} documents",
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
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
            )
            if raw_file != normalized_file:
                raise ValueError(
                    f"portable {view_type} document {index} file is not normalized"
                )


def _authenticate_vector_generation(
    reader: _OwnedViewReader,
    root: Path,
    *,
    model_suffix: str,
    expected_config: object | None,
    require_canonical: bool = True,
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
    if not isinstance(committed, Mapping) or not set(committed) <= set(_VECTOR_LEVELS):
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
        if not isinstance(records, Mapping) or set(records) != {
            "index",
            "documents",
        }:
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
                max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
            )
        except ValueError as exc:
            raise ValueError(
                f"committed documents vector artifact is invalid in {level}"
            ) from exc
        reader.authenticate(
            root / level / index_name,
            index_record,
            cache_bytes=False,
            max_bytes=_MAX_FAISS_INDEX_BYTES,
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
    reader: _OwnedViewReader,
) -> None:
    assert_no_secret_fields(view_config, source="portable BM25 view config")
    fingerprints = view_config.get("artifact_file_fingerprints")
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != {
        "documents.json",
        "bm25_metadata.json",
    }:
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
    )
    reader.authenticate(
        root / "documents.json",
        fingerprints["documents.json"],
        cache_bytes=True,
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
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
    assert_no_secret_fields(metadata, source="portable BM25 metadata")
    assert_publishable_json_value(
        metadata,
        forbidden_paths=forbidden,
        environ=environment,
        label="portable BM25 metadata",
    )
    if set(metadata) != {"project_root", "max_k", "language"}:
        raise ValueError("portable BM25 metadata has an invalid normalized shape")
    if metadata.get("project_root") != "source":
        raise ValueError("portable BM25 metadata project_root is not normalized")
    if (
        isinstance(metadata.get("max_k"), bool)
        or not isinstance(metadata.get("max_k"), int)
        or metadata["max_k"] <= 0
        or not isinstance(metadata.get("language"), str)
        or not metadata["language"].strip()
        or "\x00" in metadata["language"]
        or len(metadata["language"]) > 256
    ):
        raise ValueError("portable BM25 metadata is invalid")
    _validate_normalized_document_sources(
        root / "documents.json",
        repository,
        view_type="BM25",
        forbidden_paths=forbidden,
        environ=environment,
        reader=reader,
    )


def _validate_portable_vector_view(
    root: Path,
    repository: Path,
    *,
    view_config: Mapping[str, Any],
    forbidden: tuple[Path, ...],
    environment: Mapping[str, str],
    initial_tree: object,
    reader: _OwnedViewReader,
) -> None:
    assert_no_secret_fields(view_config, source="portable vector view config")
    route = resolve_embedding_artifact_route(view_config)
    expected_suffix = route.model.replace("/", "__")
    inventory_files = _owned_inventory_paths(root, initial_tree, kind="file")
    root_configs = sorted(
        path
        for path in inventory_files
        if path.parent == root
        and path.name.startswith("config_")
        and path.name.endswith(".json")
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
    )
    expected_config = view_config.get("persistence_config_fingerprint")
    if expected_config is None:
        raise ValueError("portable vector validation requires its config fingerprint")
    config = _authenticate_vector_generation(
        reader,
        root,
        model_suffix=model_suffix,
        expected_config=expected_config,
    )
    assert_no_secret_fields(config, source="portable vector config")
    assert_publishable_json_value(
        config,
        forbidden_paths=forbidden,
        environ=environment,
        label="portable vector config",
    )
    (
        semantic_suffix,
        expected_dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
    ) = _validate_vector_semantics(config, view_config)
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
    )
    if document_format != "json" or stale_paths:
        raise ValueError("portable vector view is not in its normalized final form")
    _validate_view_document_count(view_config, counts)
    _assert_normalized_vector_tree(
        ownership=initial_tree,
        model_suffix=model_suffix,
        counts=counts,
    )
    for level in _VECTOR_LEVELS:
        if counts[level] <= 0:
            continue
        level_root = root / level
        level_config = level_root / f"config_{model_suffix}.json"
        if level_config in inventory_files:
            assert_publishable_json_value(
                _load_json_object(
                    level_config,
                    label=f"portable vector {level} config",
                    require_canonical=True,
                    reader=reader,
                ),
                forbidden_paths=forbidden,
                environ=environment,
                label=f"portable vector {level} config",
            )
        _validate_normalized_document_sources(
            level_root / f"documents_{model_suffix}.json",
            repository,
            view_type=f"vector {level}",
            forbidden_paths=forbidden,
            environ=environment,
            reader=reader,
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
    "SourceTrust",
    "normalize_owned_query_view",
    "validate_portable_query_view",
]
