# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free integrity records for persisted vector levels."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..._atomic_directory import (
    TreeFileRecord,
    _annotate_secondary_error,
    capture_directory_ownership,
    directory_ownership_file_records,
    directory_ownership_inventory,
    lexical_directory_path,
)
from ..._bounded_json import iter_bounded_json_array
from ..._captured_directory import AuthenticatedFile, CapturedDirectoryReader
from ...native_index_authorization import (
    NativeIndexAuthorization,
    require_native_index_authorization,
    require_native_index_authorization_preflight,
)

VECTOR_PERSISTENCE_SCHEMA = 1
VECTOR_VIEW_UPDATE_MARKER = ".vector-view.update-in-progress"
_DIGEST_BYTES = 1024 * 1024
_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 256 * 1024 * 1024
_MAX_FAISS_BYTES = 8 * 1024 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_UPDATE_MARKER_PAYLOAD = b'{"schema":"codenib.vector-view-update.v1"}\n'
_VECTOR_LEVELS = frozenset({"l0", "l2"})
_MUTABLE_ROOT_FILES = frozenset(
    {
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    }
)


def _vector_entry_policy(relative: str, kind: str, mode: int, size: int) -> None:
    del mode
    path = PurePosixPath(relative)
    if kind == "directory":
        if len(path.parts) != 1 or path.name not in _VECTOR_LEVELS:
            raise ValueError(f"vector view has an invalid directory: {relative}")
        return
    if len(path.parts) == 1:
        name = path.name
        if name.startswith("config_") and name.endswith(".json"):
            limit = _MAX_CONFIG_BYTES
        elif name == "config.json":
            limit = _MAX_CONFIG_BYTES
        elif name == VECTOR_VIEW_UPDATE_MARKER or (
            name.startswith(".config_") and name.endswith(".save-in-progress")
        ):
            limit = _MAX_CONFIG_BYTES
        elif name in _MUTABLE_ROOT_FILES:
            limit = _MAX_DOCUMENT_BYTES
        else:
            raise ValueError(f"vector view has an invalid file: {relative}")
    elif len(path.parts) == 2 and path.parts[0] in _VECTOR_LEVELS:
        name = path.name
        suffix = path.suffix.casefold()
        if name.startswith("config_") and suffix == ".json":
            limit = _MAX_CONFIG_BYTES
        elif name.startswith("documents_") and suffix in {
            ".json",
            ".pkl",
            ".pickle",
        }:
            limit = _MAX_DOCUMENT_BYTES
        elif name.startswith("index_") and suffix == ".faiss":
            limit = _MAX_FAISS_BYTES
        elif name.startswith("index_") and suffix in {".pkl", ".pickle"}:
            limit = _MAX_DOCUMENT_BYTES
        else:
            raise ValueError(f"vector view has an invalid file: {relative}")
    else:
        raise ValueError(f"vector view has an invalid entry: {relative}")
    if size < 0 or size > limit:
        raise ValueError(f"vector artifact exceeds its {limit}-byte limit: {relative}")


class _WrappedValueReader:
    """Expose one bytes payload as a synthetic one-item JSON array."""

    def __init__(self, payload: bytes) -> None:
        self._parts = iter((b"[", payload, b"]"))
        self._pending = b""

    def read(self, size: int = -1) -> bytes:
        if not self._pending:
            self._pending = next(self._parts, b"")
        if size is None or size < 0 or len(self._pending) <= size:
            block = self._pending
            self._pending = b""
            return block
        block = self._pending[:size]
        self._pending = self._pending[size:]
        return block


@dataclass(slots=True)
class AuthenticatedVectorView:
    """One captured vector tree whose consumers never reopen trusted paths."""

    root: Path
    ownership: object
    reader: CapturedDirectoryReader
    records: dict[str, TreeFileRecord]
    inventory: tuple[tuple[str, str], ...]

    @classmethod
    def capture(cls, root: str | Path) -> AuthenticatedVectorView:
        lexical = lexical_directory_path(Path(root))
        ownership = capture_directory_ownership(
            lexical,
            entry_policy=_vector_entry_policy,
        )
        try:
            reader = CapturedDirectoryReader(lexical, ownership)
        except BaseException:
            raise
        return cls(
            root=lexical,
            ownership=ownership,
            reader=reader,
            records={
                record.path: record
                for record in directory_ownership_file_records(ownership)
            },
            inventory=directory_ownership_inventory(ownership),
        )

    def close(self) -> None:
        self.reader.close()

    def __enter__(self) -> AuthenticatedVectorView:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def has_file(self, relative: str | PurePosixPath) -> bool:
        value = relative.as_posix() if isinstance(relative, PurePosixPath) else relative
        return value in self.records

    def record(self, relative: str | PurePosixPath) -> TreeFileRecord:
        value = relative.as_posix() if isinstance(relative, PurePosixPath) else relative
        try:
            return self.records[value]
        except KeyError as exc:
            raise ValueError(f"vector artifact file is missing: {value}") from exc

    def open_file(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> AuthenticatedFile:
        return self.reader.open_file(relative, max_bytes=max_bytes)

    def authenticated_descriptor(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ):
        """Return a context manager yielding an immutable authenticated fd."""

        return self.reader.authenticated_descriptor(relative, max_bytes=max_bytes)

    def authenticated_snapshot(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ):
        """Return a context manager yielding immutable authenticated bytes."""

        return self.reader.authenticated_snapshot(relative, max_bytes=max_bytes)

    def load_json_object(
        self,
        relative: str | PurePosixPath,
        *,
        label: str,
        max_bytes: int = _MAX_CONFIG_BYTES,
    ) -> dict[str, Any]:
        payload = self.reader.read_bytes(relative, max_bytes=max_bytes)
        values = iter_bounded_json_array(
            _WrappedValueReader(payload),
            label=label,
            max_items=1,
            max_element_bytes=max_bytes,
        )
        try:
            value = next(values)
        except StopIteration as exc:
            raise ValueError(f"{label} is empty") from exc
        try:
            next(values)
        except StopIteration:
            pass
        else:  # pragma: no cover - the synthetic wrapper has exactly one item
            raise ValueError(f"{label} contains multiple JSON values")
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a JSON object")
        return value

    def require_record(
        self,
        relative: str | PurePosixPath,
        expected: object,
    ) -> TreeFileRecord:
        record = self.record(relative)
        if not isinstance(expected, Mapping) or set(expected) != {
            "file",
            "size",
            "sha256",
        }:
            raise ValueError("invalid vector artifact fingerprint")
        if dict(expected) != {
            "file": PurePosixPath(record.path).name,
            "size": record.size,
            "sha256": record.sha256,
        }:
            raise ValueError(
                f"vector artifact does not match its fingerprint: {record.path}"
            )
        return record

    def verify_final(self) -> None:
        self.reader.verify_root()
        observed = capture_directory_ownership(
            self.root,
            entry_policy=_vector_entry_policy,
        )
        if observed != self.ownership:
            raise ValueError("vector view changed during authenticated loading")


def capture_authenticated_vector_view(root: str | Path) -> AuthenticatedVectorView:
    """Capture a bounded no-follow vector tree before native authorization."""

    try:
        return AuthenticatedVectorView.capture(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"vector view could not be captured safely: {root}") from exc


def require_authorized_vector_view(
    root: str | Path,
    authorization: NativeIndexAuthorization | None,
    semantic_contract: Mapping[str, Any],
) -> None:
    """Require exact authority for a vector tree without loading its model.

    This gate is intended for callers whose vector-store constructor may load
    an embedding model or initialize a remote client. It binds the supplied
    capability to one freshly captured tree and semantic contract, verifies
    that the tree remained stable, and closes the captured view before the
    caller performs any expensive initialization.
    """

    require_native_index_authorization_preflight(
        authorization,
        view_type="vector",
    )
    view = capture_authenticated_vector_view(root)
    try:
        require_native_index_authorization(
            authorization,
            view.ownership,
            view_type="vector",
            semantic_contract=semantic_contract,
        )
        view.verify_final()
    except BaseException as primary_error:  # noqa: B036 - preserve first failure
        try:
            view.close()
        except BaseException as cleanup_error:  # noqa: B036 - preserve first failure
            _annotate_secondary_error(
                primary_error,
                "authenticated vector view cleanup also failed",
                cleanup_error,
            )
        raise
    view.close()


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


def _open_stable_regular(path: Path) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or bool(
                getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            )
        ):
            raise ValueError(f"invalid vector artifact file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
            before
        ):
            raise ValueError(f"vector artifact changed while opening: {path}")
        return descriptor, opened
    except ValueError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ValueError(f"invalid vector artifact file: {path}") from exc


def _read_exact(descriptor: int, size: int, path: Path) -> bytearray:
    payload = bytearray()
    remaining = size
    while remaining:
        block = os.read(descriptor, min(remaining, _DIGEST_BYTES))
        if not block:
            raise ValueError(f"vector artifact was truncated: {path}")
        payload.extend(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise ValueError(f"vector artifact grew while reading: {path}")
    return payload


def _verify_open_file(
    descriptor: int,
    opened: os.stat_result,
    path: Path,
) -> None:
    try:
        after_open = os.fstat(descriptor)
        after_path = path.lstat()
    except OSError as exc:
        raise ValueError(f"vector artifact changed while reading: {path}") from exc
    if _file_identity(after_open) != _file_identity(opened) or _file_identity(
        after_path
    ) != _file_identity(opened):
        raise ValueError(f"vector artifact changed while reading: {path}")


def _file_record(path: Path) -> dict[str, Any]:
    descriptor, opened = _open_stable_regular(path)
    try:
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, _DIGEST_BYTES))
            if not block:
                raise ValueError(f"vector artifact was truncated: {path}")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ValueError(f"vector artifact grew while hashing: {path}")
        _verify_open_file(descriptor, opened, path)
        return {
            "file": path.name,
            "size": opened.st_size,
            "sha256": digest.hexdigest(),
        }
    except OSError as exc:
        raise ValueError(f"vector artifact could not be read: {path}") from exc
    finally:
        os.close(descriptor)


def _load_config(path: Path) -> dict[str, Any]:
    descriptor, opened = _open_stable_regular(path)
    try:
        if opened.st_size > _MAX_CONFIG_BYTES:
            raise ValueError(f"vector generation config exceeds its byte limit: {path}")
        payload = _read_exact(descriptor, opened.st_size, path)
        _verify_open_file(descriptor, opened, path)
        try:
            config = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"vector generation config is invalid JSON: {path}"
            ) from exc
    except OSError as exc:
        raise ValueError(f"vector generation config could not be read: {path}") from exc
    finally:
        os.close(descriptor)
    if not isinstance(config, dict):
        raise ValueError(f"vector generation config must be an object: {path}")
    return config


def vector_level_artifact_records(
    level_path: Path,
    model_suffix: str,
    *,
    documents_file: str,
) -> dict[str, dict[str, Any]]:
    """Fingerprint the index/document pair committed for one vector level."""

    allowed_documents = {
        f"documents_{model_suffix}.pkl",
        f"documents_{model_suffix}.json",
    }
    if documents_file not in allowed_documents:
        raise ValueError(
            f"unsupported vector document artifact for {level_path}: {documents_file}"
        )
    return {
        "index": _file_record(level_path / f"index_{model_suffix}.faiss"),
        "documents": _file_record(level_path / documents_file),
    }


def vector_config_artifact_record(
    root: str | Path,
    model_suffix: str,
) -> dict[str, Any]:
    """Fingerprint the top-level record that commits all vector levels."""

    return _file_record(Path(root) / f"config_{model_suffix}.json")


def require_complete_vector_view(root: str | Path) -> None:
    """Reject a vector view whose multi-artifact update did not finish."""

    marker = Path(root) / VECTOR_VIEW_UPDATE_MARKER
    if marker.exists() or marker.is_symlink():
        raise ValueError(f"vector view has an incomplete update marker: {marker}")


def begin_vector_view_update(root: str | Path) -> Path:
    """Publish a crash marker before replacing any vector-view component."""

    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / VECTOR_VIEW_UPDATE_MARKER
    if marker.is_symlink():
        raise ValueError(f"refusing symlinked vector update marker: {marker}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(marker, flags, 0o600)
    try:
        remaining = memoryview(_UPDATE_MARKER_PAYLOAD)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("could not persist vector view update marker")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return marker


def finish_vector_view_update(root: str | Path) -> None:
    """Remove the marker only after every vector-view component is written."""

    marker = Path(root) / VECTOR_VIEW_UPDATE_MARKER
    if not marker.exists() and not marker.is_symlink():
        raise ValueError(f"vector view update marker disappeared: {marker}")
    marker.unlink()


def validate_vector_config_artifact(
    root: str | Path,
    model_suffix: str,
    expected: object,
) -> Path:
    """Validate the vector generation selected by a manifest entry."""

    expected_name = f"config_{model_suffix}.json"
    if not isinstance(expected, Mapping) or set(expected) != {
        "file",
        "size",
        "sha256",
    }:
        raise ValueError("invalid vector config fingerprint in manifest")
    if expected.get("file") != expected_name:
        raise ValueError("invalid vector config filename in manifest")
    path = Path(root) / expected_name
    if _file_record(path) != dict(expected):
        raise ValueError("vector config does not match its manifest fingerprint")
    return path


def validate_vector_level_artifacts(
    level_path: Path,
    model_suffix: str,
    expected: object,
) -> tuple[Path, Path]:
    """Validate one committed pair and return its index/document paths."""

    if not isinstance(expected, Mapping) or set(expected) != {"index", "documents"}:
        raise ValueError(f"invalid committed vector artifacts for {level_path}")

    allowed_names = {
        "index": {f"index_{model_suffix}.faiss"},
        "documents": {
            f"documents_{model_suffix}.pkl",
            f"documents_{model_suffix}.json",
        },
    }
    paths: dict[str, Path] = {}
    for kind in ("index", "documents"):
        record = expected[kind]
        if not isinstance(record, Mapping):
            raise ValueError(f"invalid {kind} vector artifact record for {level_path}")
        filename = record.get("file")
        size = record.get("size")
        digest = record.get("sha256")
        if filename not in allowed_names[kind]:
            raise ValueError(
                f"invalid {kind} vector artifact filename for {level_path}: "
                f"{filename!r}"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"invalid {kind} vector artifact size for {level_path}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"invalid {kind} vector artifact digest for {level_path}")

        path = level_path / filename
        observed = _file_record(path)
        if observed != {"file": filename, "size": size, "sha256": digest}:
            raise ValueError(f"committed {kind} vector artifact does not match {path}")
        paths[kind] = path

    return paths["index"], paths["documents"]


def validate_vector_generation_artifacts(
    root: str | Path,
    model_suffix: str,
) -> dict[str, Any]:
    """Validate every file committed by one modern top-level vector config."""

    config_path = Path(root) / f"config_{model_suffix}.json"
    config = _load_config(config_path)

    persistence_schema = config.get("persistence_schema")
    committed_levels = config.get("level_artifacts")
    if persistence_schema is None and committed_levels is None:
        return config
    if persistence_schema != VECTOR_PERSISTENCE_SCHEMA:
        raise ValueError(
            "vector generation has unsupported persistence schema: "
            f"{persistence_schema!r}"
        )
    if not isinstance(committed_levels, Mapping) or not set(committed_levels) <= {
        "l0",
        "l2",
    }:
        raise ValueError("vector generation has invalid committed levels")

    for level in ("l0", "l2"):
        count = config.get(f"{level}_documents")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(
                f"vector generation has invalid {level} document count: {count!r}"
            )
        artifacts = committed_levels.get(level)
        if count == 0:
            if artifacts is not None:
                raise ValueError(
                    f"vector generation commits artifacts for empty {level} level"
                )
            continue
        if artifacts is None:
            raise ValueError(
                f"vector generation is missing committed artifacts for {level}"
            )
        validate_vector_level_artifacts(
            Path(root) / level,
            model_suffix,
            artifacts,
        )
    return config


__all__ = [
    "AuthenticatedVectorView",
    "VECTOR_PERSISTENCE_SCHEMA",
    "VECTOR_VIEW_UPDATE_MARKER",
    "begin_vector_view_update",
    "capture_authenticated_vector_view",
    "finish_vector_view_update",
    "require_authorized_vector_view",
    "require_complete_vector_view",
    "validate_vector_config_artifact",
    "validate_vector_generation_artifacts",
    "validate_vector_level_artifacts",
    "vector_config_artifact_record",
    "vector_level_artifact_records",
]
