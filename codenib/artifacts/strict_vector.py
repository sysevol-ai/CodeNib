# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strict, missing-only recapture for schema-8 compiler vector caches."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .._atomic_directory import (
    PublicationAuthenticatedFile,
    PublicationDirectoryReader,
    TreeFileRecord,
    _annotate_secondary_error,
    capture_directory_ownership,
    directory_ownership_digest,
    directory_ownership_file_records,
    directory_ownership_inventory,
    lexical_directory_path,
    reopen_authenticated_directory,
)
from .._bounded_json import (
    canonical_json_array_chunks,
    canonical_json_value_chunks,
    iter_bounded_json_array,
    validate_bounded_json_stream,
    validate_json_complexity,
)
from .._captured_directory import (
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
    require_owned_workspace_publication_support,
)
from .._secret_fields import assert_no_secret_fields
from .._workspace_provider import (
    StrictWorkspaceProvider,
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_strict_workspace,
)
from ..index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_ROW_MAPPING_CONTRACT,
    validate_schema_8_vector_document_row,
)
from ..provider_routes import resolve_embedding_artifact_route
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
)
from .portable_views import (
    MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    MAX_PORTABLE_FAISS_INDEX_BYTES,
    _validate_content_bound_portable_query_view_reader_with_identity,
    validate_portable_vector_persistence_semantics,
)
from .security import assert_publishable_json_value

_MAX_CONFIG_JSON_BYTES = 16 * 1024 * 1024
_JSON_READ_CHUNK_BYTES = 1024 * 1024
_MAX_SOURCE_PATH_BYTES = 4_096
_MAX_SOURCE_PATH_COMPONENTS = 256
_STRICT_VECTOR_BUILDER_SCHEMA = 8
_STRICT_VECTOR_PLAN_DOMAIN = b"codenib-portable-vector-strict-workspace-v1"
_VECTOR_LEVELS = ("l0", "l2")
_RAW_MUTABLE_ROOT_FILES = frozenset(
    {
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    }
)
_REQUIRED_IDENTITY_FIELDS = frozenset(
    {
        "builder_schema",
        "embedding_model",
        "embedding_provider",
        "embedding_dimension",
        "dimension",
        "embedding_endpoint",
        "embedding_kwargs",
        "embedding_route",
        "embedding_fingerprint",
        "index_metric",
        "languages",
        "levels",
        "max_lines_per_chunk",
        "chunking_failure_policy",
        "repository_filter_policy",
        "source_selection_digest",
    }
)
_OPTIONAL_IDENTITY_FIELDS = frozenset(
    {"additional_ignore_dirs", "embedding_document_max_chars"}
)
_ROOT_CONFIG_FIELDS = frozenset(
    {
        "embedding_model",
        "embedding_provider",
        "embedding_revision",
        "dimension",
        "index_type",
        "index_metric",
        "l0_documents",
        "l2_documents",
        "persistence_schema",
        "row_mapping",
        "level_artifacts",
        "artifact",
    }
)
_LEVEL_CONFIG_FIELDS = frozenset(
    {
        "embedding_model",
        "embedding_provider",
        "embedding_revision",
        "dimension",
        "index_type",
        "index_metric",
        "level",
        "num_documents",
    }
)


class _InterruptibleReader:
    __slots__ = ("_source", "_check_cancelled")

    def __init__(self, source: Any, check_cancelled: Callable[[], None]) -> None:
        self._source = source
        self._check_cancelled = check_cancelled

    def read(self, size: int = -1) -> bytes:
        self._check_cancelled()
        block = self._source.read(size)
        self._check_cancelled()
        return block


def _interruptible_chunks(
    chunks: Iterable[bytes],
    check_cancelled: Callable[[], None] | None,
) -> Iterator[bytes]:
    for chunk in chunks:
        if check_cancelled is not None:
            check_cancelled()
        yield chunk


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _bounded_canonical_json_bytes(
    value: Any,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    payload = bytearray()
    for chunk in canonical_json_value_chunks(value):
        payload.extend(chunk)
        if len(payload) + 1 > max_bytes:
            raise ValueError(f"{label} exceeds its {max_bytes}-byte limit")
    payload.extend(b"\n")
    return bytes(payload)


def _json_object_snapshot(
    value: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} requires a mapping")
    detached = dict(value)
    validate_json_complexity(detached, label=label)
    try:
        payload = _bounded_canonical_json_bytes(
            detached,
            max_bytes=_MAX_CONFIG_JSON_BYTES,
            label=label,
        )
        snapshot = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical JSON data") from exc
    if not isinstance(snapshot, dict):  # pragma: no cover - detached is a dict
        raise AssertionError(f"{label} snapshot is not an object")
    return snapshot


def _environment_snapshot(environ: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        raise TypeError("strict vector environment must be a mapping")
    snapshot = dict(source)
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in snapshot.items()
    ):
        raise TypeError("strict vector environment must contain text pairs")
    return snapshot


def _preflight_recapture_authorities(
    *,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
) -> None:
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict vector repository source has an invalid type")
    if type(output_receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict vector output receipt owner has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict vector repository source is not usable")
    if output_receipt_owner.state != "empty":
        raise RuntimeError("strict vector output receipt owner must be empty")

    require_owned_workspace_publication_support()
    for member in ("require_support", "run_workspace"):
        try:
            raw = inspect.getattr_static(workspace_provider, member)
        except AttributeError as exc:
            raise TypeError(
                "strict workspace provider has an invalid contract"
            ) from exc
        if isinstance(raw, (classmethod, staticmethod)):
            raw = raw.__func__
        if not callable(raw):
            raise TypeError("strict workspace provider has an invalid contract")


def _authenticated_repository_identity(
    repository_source: RepositorySourceBinding,
) -> RepositorySourceIdentitySnapshot:
    identity = repository_source.authenticated_identity_snapshot()
    if type(identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("strict vector repository identity has an invalid type")
    return identity


def _path_relation(path: Path, boundary: Path) -> str:
    if path == boundary:
        return "same"
    if boundary in path.parents:
        return "descendant"
    if path in boundary.parents:
        return "ancestor"
    return "disjoint"


def _resolved_recapture_path(path: Path, *, strict: bool, label: str) -> Path:
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"strict vector {label} cannot be authenticated") from exc


def _require_missing_destination(destination: Path) -> None:
    try:
        destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(
            "strict vector recapture destination cannot be authenticated"
        ) from exc
    raise FileExistsError("strict vector recapture destination must be missing")


def _recapture_locations(
    source: Path,
    destination: Path,
    *,
    repository_identity: RepositorySourceIdentitySnapshot,
) -> tuple[Path, Path]:
    """Authenticate denial-only topology for one ordinary cache recapture."""

    lexical_source = lexical_directory_path(source)
    lexical_destination = lexical_directory_path(destination)
    repository = lexical_directory_path(repository_identity.root)
    source_relation = _path_relation(lexical_source, repository)
    physical_source = _resolved_recapture_path(
        lexical_source,
        strict=True,
        label="recapture source location",
    )
    physical_repository = _resolved_recapture_path(
        repository,
        strict=True,
        label="repository location",
    )
    if source_relation != _path_relation(physical_source, physical_repository):
        raise ValueError(
            "strict vector recapture source has inconsistent lexical and physical "
            "repository topology"
        )
    if source_relation in {"same", "ancestor"}:
        raise ValueError(
            "strict vector recapture source must not contain the source repository"
        )
    if source_relation == "descendant":
        relative = lexical_source.relative_to(repository).as_posix()
        prefix = relative + "/"
        if any(
            record.path == relative or record.path.startswith(prefix)
            for record in repository_identity.file_records
        ):
            raise ValueError(
                "strict vector recapture source inside the repository must be "
                "excluded from its authenticated source identity"
            )

    physical_destination = _resolved_recapture_path(
        lexical_destination,
        strict=False,
        label="recapture destination location",
    )
    if any(
        _path_relation(candidate, boundary) != "disjoint"
        for candidate, boundary in (
            (lexical_destination, lexical_source),
            (lexical_destination, repository),
            (physical_destination, physical_source),
            (physical_destination, physical_repository),
        )
    ):
        raise ValueError(
            "strict vector recapture destination must be disjoint from source and "
            "repository"
        )
    _require_missing_destination(lexical_destination)
    return lexical_source, lexical_destination


def _assert_authenticated_publishable_json_value(
    value: Any,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
    label: str,
) -> None:
    assert_publishable_json_value(
        value,
        forbidden_paths=(),
        environ=environ,
        label=label,
    )
    forbidden: set[str] = set()
    for path in forbidden_paths:
        expanded = path.expanduser()
        raw = os.fsdecode(os.fspath(expanded))
        lexical = os.path.abspath(raw)
        if os.path.isabs(raw):
            forbidden.add(raw)
        forbidden.update((lexical, Path(lexical).as_posix()))
    patterns = tuple(sorted(pattern for pattern in forbidden if pattern))
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if any(pattern in key for pattern in patterns):
                    raise ValueError(f"{label} contains an absolute build-machine path")
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str) and any(
            pattern in current for pattern in patterns
        ):
            raise ValueError(f"{label} contains an absolute build-machine path")


def _source_path(
    value: object,
    *,
    source: str,
    authenticated_source_files: frozenset[str],
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source} must be a non-empty repository-relative path")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{source} is not a valid UTF-8 path") from exc
    if len(encoded) > _MAX_SOURCE_PATH_BYTES:
        raise ValueError(f"{source} exceeds {_MAX_SOURCE_PATH_BYTES} UTF-8 path bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{source} is not a portable repository-relative path")
    raw_parts = value.split("/")
    normalized = PurePosixPath(value)
    if (
        value.startswith(("/", "~"))
        or "\\" in value
        or normalized.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or value != normalized.as_posix()
        or len(normalized.parts) > _MAX_SOURCE_PATH_COMPONENTS
    ):
        raise ValueError(f"{source} is not a portable repository-relative path")
    normalized_text = normalized.as_posix()
    if normalized_text not in authenticated_source_files:
        raise ValueError(f"{source} is not in the authenticated repository source")
    return normalized_text


def _raw_entry_policy(model_suffix: str):
    root_config = f"config_{model_suffix}.json"
    level_files = {
        f"config_{model_suffix}.json": _MAX_CONFIG_JSON_BYTES,
        f"documents_{model_suffix}.json": MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        f"index_{model_suffix}.faiss": MAX_PORTABLE_FAISS_INDEX_BYTES,
    }
    root_files = {
        root_config: _MAX_CONFIG_JSON_BYTES,
        "chunk_store.json": MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        "chunk_store.pkl": MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        "embeddings_cache.json": MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        "embeddings_cache.npz": MAX_PORTABLE_FAISS_INDEX_BYTES,
        "embeddings_cache.pkl": MAX_PORTABLE_FAISS_INDEX_BYTES,
        "incremental_state.json": _MAX_CONFIG_JSON_BYTES,
    }

    def policy(relative: str, kind: str, mode: int, size: int) -> None:
        del mode
        path = PurePosixPath(relative)
        limit: int | None = None
        if kind == "directory":
            if len(path.parts) == 1 and path.name in _VECTOR_LEVELS:
                return
        elif kind == "file" and len(path.parts) == 1:
            limit = root_files.get(path.name)
        elif (
            kind == "file" and len(path.parts) == 2 and path.parts[0] in _VECTOR_LEVELS
        ):
            limit = level_files.get(path.name)
        if limit is None:
            raise ValueError(f"schema-8 vector cache has an invalid entry: {relative}")
        if size < 0 or size > limit:
            raise ValueError(
                f"schema-8 vector cache entry exceeds its {limit}-byte limit: "
                f"{relative}"
            )

    return policy


class _PublicationVectorReader:
    """Vector facade over one callback-scoped publication reader."""

    def __init__(
        self,
        publication: PublicationDirectoryReader,
        ownership: object,
    ) -> None:
        self.root = Path("/__codenib_publication_vector__")
        self.ownership = ownership
        self._publication = publication
        self._records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)  # type: ignore[arg-type]
        }

    def record(self, relative: str | PurePosixPath) -> TreeFileRecord:
        key = relative.as_posix() if isinstance(relative, PurePosixPath) else relative
        try:
            return self._records[key]
        except KeyError as exc:
            raise ValueError(
                f"strict vector cache has no captured file: {key}"
            ) from exc

    @contextmanager
    def open_file(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> Iterator[PublicationAuthenticatedFile]:
        key = relative.as_posix() if isinstance(relative, PurePosixPath) else relative
        expected = self.record(key)
        with self._publication.open_authenticated_file(
            PurePosixPath(key),
            max_bytes=max_bytes,
        ) as source:
            yield source
        if source.record != expected:
            raise RuntimeError(f"strict vector file changed while reading: {key}")

    def read_bytes(self, relative: str, *, max_bytes: int) -> bytes:
        payload = bytearray()
        with self.open_file(relative, max_bytes=max_bytes) as source:
            for chunk in source.iter_bytes(chunk_size=_JSON_READ_CHUNK_BYTES):
                payload.extend(chunk)
        return bytes(payload)

    def verify_root(self, *, entry_policy: Any) -> None:
        observed = self._publication.capture_ownership(entry_policy=entry_policy)
        if observed != self.ownership:
            raise RuntimeError("strict vector cache changed during validation")


def _load_json_object(
    reader: _PublicationVectorReader,
    relative: str,
    *,
    label: str,
) -> dict[str, Any]:
    with reader.open_file(relative, max_bytes=_MAX_CONFIG_JSON_BYTES) as source:
        validate_bounded_json_stream(
            source,
            label=label,
            max_bytes=_MAX_CONFIG_JSON_BYTES,
        )
    payload = reader.read_bytes(relative, max_bytes=_MAX_CONFIG_JSON_BYTES)
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
        )
    except (RecursionError, ValueError) as exc:
        raise ValueError(f"{label} is invalid UTF-8 JSON") from exc
    validate_json_complexity(value, label=label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _record_chunks(
    relative: str,
    chunks: Iterable[bytes],
    *,
    max_bytes: int,
) -> TreeFileRecord:
    iterator = iter(chunks)
    digest = hashlib.sha256()
    size = 0
    primary_error: BaseException | None = None
    try:
        for chunk in iterator:
            if type(chunk) is not bytes:
                raise TypeError("strict vector output chunks must be exact bytes")
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(
                    f"strict vector output exceeds its {max_bytes}-byte limit: "
                    f"{relative}"
                )
            digest.update(chunk)
    except BaseException as exc:  # noqa: B036 - preserve generation fault
        primary_error = exc
    try:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    except BaseException as close_error:  # noqa: B036 - cleanup is secondary
        if primary_error is None:
            raise
        _annotate_secondary_error(
            primary_error,
            "strict vector output iterator close also failed",
            close_error,
        )
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    return TreeFileRecord(
        path=relative,
        mode=0o600,
        size=size,
        sha256=digest.hexdigest(),
    )


def _normalized_documents(
    reader: _PublicationVectorReader,
    relative: str,
    *,
    level: str,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
    counter: list[int] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterable[dict[str, Any]]:
    if check_cancelled is not None:
        check_cancelled()
    with reader.open_file(
        relative,
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    ) as source:
        documents = iter_bounded_json_array(
            (
                source
                if check_cancelled is None
                else _InterruptibleReader(source, check_cancelled)
            ),
            label=f"schema-8 vector {level} documents",
        )
        for index, document in enumerate(documents):
            if check_cancelled is not None:
                check_cancelled()
            page_content, metadata = validate_schema_8_vector_document_row(
                document,
                row_index=index,
                level=level,
            )
            assert_no_secret_fields(
                metadata,
                source=f"schema-8 vector {level} document {index} metadata",
            )
            normalized_metadata = dict(metadata)
            normalized_metadata["file"] = _source_path(
                metadata["file"],
                source=f"schema-8 vector {level} document {index} file",
                authenticated_source_files=authenticated_source_files,
            )
            normalized = {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
            _assert_authenticated_publishable_json_value(
                normalized,
                forbidden_paths=forbidden_paths,
                environ=environ,
                label=f"schema-8 vector {level} document {index}",
            )
            if counter is not None:
                counter[0] += 1
            yield normalized
    if check_cancelled is not None:
        check_cancelled()


def _document_record(
    reader: _PublicationVectorReader,
    relative: str,
    *,
    level: str,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[TreeFileRecord, int]:
    counter = [0]
    record = _record_chunks(
        relative,
        canonical_json_array_chunks(
            _normalized_documents(
                reader,
                relative,
                level=level,
                forbidden_paths=forbidden_paths,
                environ=environ,
                authenticated_source_files=authenticated_source_files,
                counter=counter,
                check_cancelled=check_cancelled,
            )
        ),
        max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    )
    if record != TreeFileRecord(
        path=relative,
        mode=0o600,
        size=reader.record(relative).size,
        sha256=reader.record(relative).sha256,
    ):
        raise ValueError(
            f"schema-8 vector {level} documents are not canonical producer bytes"
        )
    return record, counter[0]


def _fingerprint_record(
    value: object,
    *,
    expected_name: str,
    label: str,
) -> tuple[int, str]:
    if not isinstance(value, Mapping) or set(value) != {"file", "size", "sha256"}:
        raise ValueError(f"{label} has an invalid fingerprint shape")
    if value.get("file") != expected_name:
        raise ValueError(f"{label} has an invalid filename")
    size = value.get("size")
    digest = value.get("sha256")
    if type(size) is not int or size < 0:
        raise ValueError(f"{label} has an invalid size")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{label} has an invalid digest")
    return size, digest


def _require_record_fingerprint(
    record: TreeFileRecord,
    value: object,
    *,
    expected_name: str,
    expected_record_path: str | None = None,
    label: str,
) -> None:
    size, digest = _fingerprint_record(
        value,
        expected_name=expected_name,
        label=label,
    )
    record_path = (
        expected_name if expected_record_path is None else expected_record_path
    )
    if record.path != record_path or record.size != size or record.sha256 != digest:
        raise ValueError(f"{label} does not match its captured file")


def _repository_files(
    repository_identity: RepositorySourceIdentitySnapshot,
) -> frozenset[str]:
    records = repository_identity.file_records
    paths = frozenset(record.path for record in records)
    if len(paths) != len(records):
        raise RuntimeError("authenticated repository source repeats a file")
    return paths


@dataclass(frozen=True, slots=True)
class _VectorPolicy:
    config_digest: str
    view_config: dict[str, Any]
    forbidden_paths: tuple[Path, ...]
    environment: dict[str, str]
    authenticated_source_files: frozenset[str]
    model_suffix: str


def _policy(
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config: Mapping[str, Any],
    *,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> _VectorPolicy:
    if (
        type(view_config.get("builder_schema")) is not int
        or view_config.get("builder_schema") != _STRICT_VECTOR_BUILDER_SCHEMA
    ):
        raise ValueError(
            "strict vector cache ingress requires builder schema 8; rebuild older "
            "compiler caches"
        )
    if not _REQUIRED_IDENTITY_FIELDS <= set(view_config):
        raise ValueError("schema-8 vector view config is missing producer identity")
    source_selection = repository_identity.source_selection
    if type(source_selection) is not RepositorySourceSelection:
        raise ValueError(
            "schema-8 vector ingress requires an authenticated source selection"
        )
    selection_digest = view_config.get("source_selection_digest")
    if type(selection_digest) is not str or selection_digest != source_selection.digest:
        raise ValueError(
            "schema-8 vector source selection differs from the authenticated "
            "repository source"
        )
    identity_fields = _REQUIRED_IDENTITY_FIELDS | (
        set(view_config) & _OPTIONAL_IDENTITY_FIELDS
    )
    if any(field not in view_config for field in identity_fields):  # pragma: no cover
        raise AssertionError("strict vector identity field snapshot is incomplete")
    forbidden = (repository_identity.root, *forbidden_paths)
    assert_no_secret_fields(view_config, source="schema-8 vector view config")
    _assert_authenticated_publishable_json_value(
        view_config,
        forbidden_paths=forbidden,
        environ=environ,
        label="schema-8 vector view config",
    )
    route = resolve_embedding_artifact_route(view_config, environ={})
    model_suffix = route.model.replace("/", "__")
    if not model_suffix or "/" in model_suffix or "\\" in model_suffix:
        raise ValueError("schema-8 vector model suffix is not portable")
    config_bytes = _bounded_canonical_json_bytes(
        dict(view_config),
        max_bytes=_MAX_CONFIG_JSON_BYTES,
        label="schema-8 vector view config",
    )
    return _VectorPolicy(
        config_digest=hashlib.sha256(config_bytes).hexdigest(),
        view_config=dict(view_config),
        forbidden_paths=forbidden,
        environment=dict(environ),
        authenticated_source_files=_repository_files(repository_identity),
        model_suffix=model_suffix,
    )


@dataclass(frozen=True, slots=True)
class _DerivedVector:
    output_records: tuple[TreeFileRecord, ...]
    root_config_payload: bytes
    level_config_payloads: tuple[tuple[str, bytes], ...]
    counts: tuple[tuple[str, int], ...]
    model_suffix: str

    @property
    def count_map(self) -> dict[str, int]:
        return dict(self.counts)

    @property
    def level_config_map(self) -> dict[str, bytes]:
        return dict(self.level_config_payloads)


def _validate_identity(
    root_config: Mapping[str, Any],
    view_config: Mapping[str, Any],
) -> None:
    artifact = root_config.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("schema-8 vector root config has no artifact identity")
    expected_fields = _REQUIRED_IDENTITY_FIELDS | (
        set(view_config) & _OPTIONAL_IDENTITY_FIELDS
    )
    if set(artifact) != expected_fields:
        raise ValueError("schema-8 vector root artifact identity has an invalid shape")
    expected = {field: view_config[field] for field in expected_fields}
    if dict(artifact) != expected:
        raise ValueError("schema-8 vector root artifact identity differs from manifest")
    if (
        type(artifact.get("builder_schema")) is not int
        or artifact.get("builder_schema") != _STRICT_VECTOR_BUILDER_SCHEMA
    ):
        raise ValueError("schema-8 vector root artifact identity is not schema 8")


def _validate_document_count(
    view_config: Mapping[str, Any],
    counts: Mapping[str, int],
) -> None:
    raw = view_config.get("document_count")
    if not isinstance(raw, Mapping):
        raise ValueError("schema-8 vector view config requires document_count")
    expected = {level: count for level, count in counts.items() if count > 0}
    if dict(raw) != expected:
        raise ValueError("schema-8 vector document_count differs from persistence")


def _derived_vector(
    reader: _PublicationVectorReader,
    *,
    policy: _VectorPolicy,
    check_cancelled: Callable[[], None] | None = None,
) -> _DerivedVector:
    if check_cancelled is not None:
        check_cancelled()
    root_name = f"config_{policy.model_suffix}.json"
    root_config = _load_json_object(
        reader,
        root_name,
        label="schema-8 vector root config",
    )
    if set(root_config) != _ROOT_CONFIG_FIELDS:
        raise ValueError("schema-8 vector root config has an invalid shape")
    assert_no_secret_fields(root_config, source="schema-8 vector root config")
    _assert_authenticated_publishable_json_value(
        root_config,
        forbidden_paths=policy.forbidden_paths,
        environ=policy.environment,
        label="schema-8 vector root config",
    )
    if root_config.get("persistence_schema") != VECTOR_PERSISTENCE_SCHEMA:
        raise ValueError(
            "schema-8 vector root config has an invalid persistence schema"
        )
    if root_config.get("row_mapping") != VECTOR_ROW_MAPPING_CONTRACT:
        raise ValueError("schema-8 vector root config has an invalid row mapping")
    _validate_identity(root_config, policy.view_config)
    validate_portable_vector_persistence_semantics(
        root_config,
        policy.view_config,
    )
    expected_root_fingerprint = policy.view_config.get("persistence_config_fingerprint")
    _require_record_fingerprint(
        reader.record(root_name),
        expected_root_fingerprint,
        expected_name=root_name,
        label="schema-8 vector root config fingerprint",
    )

    levels = policy.view_config.get("levels")
    if (
        not isinstance(levels, list)
        or not levels
        or any(level not in _VECTOR_LEVELS for level in levels)
        or len(set(levels)) != len(levels)
    ):
        raise ValueError("schema-8 vector build levels are invalid")
    committed = root_config.get("level_artifacts")
    if not isinstance(committed, Mapping) or not set(committed) <= set(_VECTOR_LEVELS):
        raise ValueError("schema-8 vector committed levels are invalid")

    output_records: list[TreeFileRecord] = []
    level_config_payloads: list[tuple[str, bytes]] = []
    counts: dict[str, int] = {}
    normalized_committed: dict[str, dict[str, dict[str, Any]]] = {}
    expected_query_files = {root_name}
    expected_query_directories: set[str] = set()
    for level in _VECTOR_LEVELS:
        if check_cancelled is not None:
            check_cancelled()
        raw_count = root_config.get(f"{level}_documents")
        if type(raw_count) is not int or raw_count < 0:
            raise ValueError(f"schema-8 vector {level} count is invalid")
        counts[level] = raw_count
        artifacts = committed.get(level)
        if raw_count == 0:
            if artifacts is not None:
                raise ValueError(f"schema-8 vector commits artifacts for empty {level}")
            continue
        if level not in levels:
            raise ValueError(f"schema-8 vector commits unconfigured level {level}")
        if not isinstance(artifacts, Mapping) or set(artifacts) != {
            "index",
            "documents",
        }:
            raise ValueError(f"schema-8 vector {level} artifacts are invalid")

        level_root = f"{level}/"
        config_name = level_root + f"config_{policy.model_suffix}.json"
        documents_name = f"documents_{policy.model_suffix}.json"
        documents_relative = level_root + documents_name
        index_name = f"index_{policy.model_suffix}.faiss"
        index_relative = level_root + index_name
        expected_query_directories.add(level)
        expected_query_files.update({config_name, documents_relative, index_relative})

        _require_record_fingerprint(
            reader.record(documents_relative),
            artifacts["documents"],
            expected_name=documents_name,
            expected_record_path=documents_relative,
            label=f"schema-8 vector {level} documents fingerprint",
        )
        _require_record_fingerprint(
            reader.record(index_relative),
            artifacts["index"],
            expected_name=index_name,
            expected_record_path=index_relative,
            label=f"schema-8 vector {level} index fingerprint",
        )
        document_record, observed_count = _document_record(
            reader,
            documents_relative,
            level=level,
            forbidden_paths=policy.forbidden_paths,
            environ=policy.environment,
            authenticated_source_files=policy.authenticated_source_files,
            check_cancelled=check_cancelled,
        )
        if check_cancelled is not None:
            check_cancelled()
        if observed_count != raw_count:
            raise ValueError(
                f"schema-8 vector {level} document count differs from root config"
            )

        level_config = _load_json_object(
            reader,
            config_name,
            label=f"schema-8 vector {level} config",
        )
        if set(level_config) != _LEVEL_CONFIG_FIELDS:
            raise ValueError(f"schema-8 vector {level} config has an invalid shape")
        checks = {
            "embedding_model": root_config["embedding_model"],
            "embedding_provider": root_config["embedding_provider"],
            "embedding_revision": root_config["embedding_revision"],
            "dimension": root_config["dimension"],
            "index_type": root_config["index_type"],
            "index_metric": root_config["index_metric"],
            "level": level,
            "num_documents": raw_count,
        }
        if level_config != checks:
            raise ValueError(f"schema-8 vector {level} config differs from root")
        _assert_authenticated_publishable_json_value(
            level_config,
            forbidden_paths=policy.forbidden_paths,
            environ=policy.environment,
            label=f"schema-8 vector {level} config",
        )
        level_payload = _bounded_canonical_json_bytes(
            level_config,
            max_bytes=_MAX_CONFIG_JSON_BYTES,
            label=f"schema-8 vector {level} config",
        )
        level_config_payloads.append((config_name, level_payload))
        output_records.extend(
            (
                TreeFileRecord(
                    path=config_name,
                    mode=0o600,
                    size=len(level_payload),
                    sha256=hashlib.sha256(level_payload).hexdigest(),
                ),
                document_record,
                TreeFileRecord(
                    path=index_relative,
                    mode=0o600,
                    size=reader.record(index_relative).size,
                    sha256=reader.record(index_relative).sha256,
                ),
            )
        )
        normalized_committed[level] = {
            "documents": {
                "file": documents_name,
                "size": document_record.size,
                "sha256": document_record.sha256,
            },
            "index": {
                "file": index_name,
                "size": reader.record(index_relative).size,
                "sha256": reader.record(index_relative).sha256,
            },
        }

    if not any(counts.values()):
        raise ValueError("schema-8 vector generation has no non-empty level")
    _validate_document_count(policy.view_config, counts)
    root_config["level_artifacts"] = normalized_committed
    root_payload = _bounded_canonical_json_bytes(
        root_config,
        max_bytes=_MAX_CONFIG_JSON_BYTES,
        label="schema-8 vector root config",
    )
    output_records.append(
        TreeFileRecord(
            path=root_name,
            mode=0o600,
            size=len(root_payload),
            sha256=hashlib.sha256(root_payload).hexdigest(),
        )
    )

    inventory = directory_ownership_inventory(reader.ownership)  # type: ignore[arg-type]
    inventory_paths = {(relative, kind) for relative, kind in inventory}
    required_raw_files = expected_query_files | _RAW_MUTABLE_ROOT_FILES
    if any(
        (relative, "file") not in inventory_paths for relative in required_raw_files
    ):
        raise ValueError("schema-8 vector cache is missing current producer state")
    observed_directories = {
        relative for relative, kind in inventory if kind == "directory"
    }
    if observed_directories != expected_query_directories:
        raise ValueError("schema-8 vector cache level directories are not exact")

    if check_cancelled is not None:
        check_cancelled()

    return _DerivedVector(
        output_records=tuple(sorted(output_records, key=lambda item: item.path)),
        root_config_payload=root_payload,
        level_config_payloads=tuple(sorted(level_config_payloads)),
        counts=tuple((level, counts[level]) for level in _VECTOR_LEVELS),
        model_suffix=policy.model_suffix,
    )


def _frame(digest: Any, label: bytes, payload: bytes) -> None:
    digest.update(len(label).to_bytes(2, "big"))
    digest.update(label)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _record_frame(digest: Any, scope: bytes, record: TreeFileRecord) -> None:
    _frame(digest, scope + b"-path", record.path.encode("utf-8"))
    _frame(digest, scope + b"-mode", record.mode.to_bytes(4, "big"))
    _frame(digest, scope + b"-size", record.size.to_bytes(8, "big"))
    _frame(digest, scope + b"-sha256", bytes.fromhex(record.sha256))


def _subject_digest(
    *,
    source_digest: str,
    source_records: tuple[TreeFileRecord, ...],
    output_records: tuple[TreeFileRecord, ...],
    view_config_digest: str,
    repository_fingerprint: str,
) -> str:
    digest = hashlib.sha256()
    _frame(digest, b"domain", _STRICT_VECTOR_PLAN_DOMAIN)
    _frame(digest, b"source-tree-sha256", bytes.fromhex(source_digest))
    _frame(digest, b"view-config-sha256", bytes.fromhex(view_config_digest))
    _frame(
        digest,
        b"repository-fingerprint",
        repository_fingerprint.encode("utf-8"),
    )
    for record in source_records:
        _record_frame(digest, b"source-record", record)
    for record in output_records:
        _record_frame(digest, b"output-record", record)
    return digest.hexdigest()


def _workspace_plan(
    *,
    source_digest: str,
    source_records: tuple[TreeFileRecord, ...],
    output_records: tuple[TreeFileRecord, ...],
    view_config_digest: str,
    repository_fingerprint: str,
) -> WorkspacePlan:
    directories = {
        parent
        for record in output_records
        for parent in PurePosixPath(record.path).parents
        if parent != PurePosixPath(".")
    }
    return WorkspacePlan(
        subject_digest=_subject_digest(
            source_digest=source_digest,
            source_records=source_records,
            output_records=output_records,
            view_config_digest=view_config_digest,
            repository_fingerprint=repository_fingerprint,
        ),
        directories=tuple(
            WorkspaceDirectory(path, mode=0o700)
            for path in sorted(directories, key=lambda item: item.as_posix())
        ),
        files=tuple(
            WorkspaceFile(
                PurePosixPath(record.path),
                mode=record.mode,
                max_bytes=record.size,
            )
            for record in output_records
        ),
        root_mode=0o700,
    )


@dataclass(frozen=True, slots=True)
class PlannedVectorView:
    """Immutable exact output plan for one schema-8 vector generation."""

    plan: WorkspacePlan
    source_ownership: object
    source_digest: str
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]
    view_config_digest: str
    repository_fingerprint: str
    model_suffix: str
    counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        counts = tuple(self.counts)
        if type(self.plan) is not WorkspacePlan:
            raise TypeError("strict vector plan must contain an exact WorkspacePlan")
        if any(type(record) is not TreeFileRecord for record in source_records):
            raise TypeError("strict vector source records have an invalid type")
        if any(type(record) is not TreeFileRecord for record in output_records):
            raise TypeError("strict vector output records have an invalid type")
        if source_records != tuple(sorted(source_records, key=lambda item: item.path)):
            raise ValueError("strict vector source records must be canonical")
        if output_records != tuple(sorted(output_records, key=lambda item: item.path)):
            raise ValueError("strict vector output records must be canonical")
        if len({record.path for record in output_records}) != len(output_records):
            raise ValueError("strict vector output records repeat a path")
        if any(record.mode != 0o600 for record in output_records):
            raise ValueError("strict vector output files must use mode 0600")
        if (
            not isinstance(self.model_suffix, str)
            or not self.model_suffix
            or "/" in self.model_suffix
            or "\\" in self.model_suffix
        ):
            raise ValueError("strict vector model suffix is invalid")
        if tuple(level for level, _count in counts) != _VECTOR_LEVELS or any(
            type(count) is not int or count < 0 for _level, count in counts
        ):
            raise ValueError("strict vector counts are invalid")
        expected_paths = {f"config_{self.model_suffix}.json"}
        for level, count in counts:
            if count <= 0:
                continue
            expected_paths.update(
                {
                    f"{level}/config_{self.model_suffix}.json",
                    f"{level}/documents_{self.model_suffix}.json",
                    f"{level}/index_{self.model_suffix}.faiss",
                }
            )
        if {record.path for record in output_records} != expected_paths:
            raise ValueError("strict vector output records are not exact")
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)
        object.__setattr__(self, "counts", counts)

    @property
    def adjustments(self) -> dict[str, Any]:
        root_name = f"config_{self.model_suffix}.json"
        root_record = next(
            record for record in self.output_records if record.path == root_name
        )
        return {
            "artifact_scope": "query-serving",
            "portable_document_format": "codenib.vector-documents.v1",
            "persistence_config_fingerprint": {
                "file": root_name,
                "size": root_record.size,
                "sha256": root_record.sha256,
            },
        }


def _captured_source_view(
    expected_ownership: object,
    publication: PublicationDirectoryReader,
    *,
    label: str,
) -> tuple[object, tuple[TreeFileRecord, ...], _PublicationVectorReader]:
    # Every caller enters through ``reopen_authenticated_directory``, whose
    # pre/post whole-tree matches already bind this callback to the expected
    # ownership.  Per-file reads compare against the same retained records.
    ownership = expected_ownership
    records = tuple(directory_ownership_file_records(ownership))  # type: ignore[arg-type]
    if not records:
        raise ValueError(f"{label} has no files")
    return ownership, records, _PublicationVectorReader(publication, ownership)


def _planned_view_from_publication(
    publication: PublicationDirectoryReader,
    expected_ownership: object,
    *,
    repository_identity: RepositorySourceIdentitySnapshot,
    policy: _VectorPolicy,
    source_label: str,
    check_cancelled: Callable[[], None] | None = None,
) -> PlannedVectorView:
    if check_cancelled is not None:
        check_cancelled()
    ownership, source_records, reader = _captured_source_view(
        expected_ownership,
        publication,
        label=source_label,
    )
    derived = _derived_vector(
        reader,
        policy=policy,
        check_cancelled=check_cancelled,
    )
    if check_cancelled is not None:
        check_cancelled()
    source_digest = directory_ownership_digest(ownership)  # type: ignore[arg-type]
    plan = _workspace_plan(
        source_digest=source_digest,
        source_records=source_records,
        output_records=derived.output_records,
        view_config_digest=policy.config_digest,
        repository_fingerprint=repository_identity.fingerprint,
    )
    return PlannedVectorView(
        plan=plan,
        source_ownership=ownership,
        source_digest=source_digest,
        source_records=source_records,
        output_records=derived.output_records,
        view_config_digest=policy.config_digest,
        repository_fingerprint=repository_identity.fingerprint,
        model_suffix=derived.model_suffix,
        counts=derived.counts,
    )


def _plan_recaptured_vector_view_with_identity(
    lexical_source: Path,
    *,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    check_cancelled: Callable[[], None] | None = None,
) -> PlannedVectorView:
    if check_cancelled is not None:
        check_cancelled()
    policy = _policy(
        repository_identity,
        view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    entry_policy = _raw_entry_policy(policy.model_suffix)
    source_ownership = capture_directory_ownership(
        lexical_source,
        entry_policy=entry_policy,
    )
    if check_cancelled is not None:
        check_cancelled()

    def consume_source(publication: PublicationDirectoryReader) -> PlannedVectorView:
        return _planned_view_from_publication(
            publication,
            source_ownership,
            repository_identity=repository_identity,
            policy=policy,
            source_label="strict vector recapture source",
            check_cancelled=check_cancelled,
        )

    with repository_source.read_session():
        return reopen_authenticated_directory(
            lexical_source,
            source_ownership,  # type: ignore[arg-type]
            consume_source,
        )


def _plan_recaptured_vector_view(
    source: Path,
    destination: Path,
    *,
    repository_source: RepositorySourceBinding,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> PlannedVectorView:
    """Privately pre-plan a schema-8 vector cache without workspace mutation."""

    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict vector repository source has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict vector repository source is not usable")
    if check_cancelled is not None:
        check_cancelled()
    repository_identity = _authenticated_repository_identity(repository_source)
    lexical_source, _lexical_destination = _recapture_locations(
        source,
        destination,
        repository_identity=repository_identity,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="schema-8 vector view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    if any(type(path) is not type(Path()) for path in forbidden_tail):
        raise TypeError("strict vector forbidden paths must be exact Path values")
    return _plan_recaptured_vector_view_with_identity(
        lexical_source,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
        check_cancelled=check_cancelled,
    )


def _require_plan(
    planned: PlannedVectorView,
    *,
    source_ownership: object,
    repository_identity: RepositorySourceIdentitySnapshot,
    policy: _VectorPolicy,
) -> None:
    if type(planned) is not PlannedVectorView:
        raise TypeError("strict vector execution requires a PlannedVectorView")
    if (
        planned.source_ownership != source_ownership
        or planned.source_records
        != tuple(directory_ownership_file_records(source_ownership))  # type: ignore[arg-type]
        or planned.source_digest
        != directory_ownership_digest(source_ownership)  # type: ignore[arg-type]
    ):
        raise ValueError("strict vector plan belongs to another source generation")
    if planned.repository_fingerprint != repository_identity.fingerprint:
        raise ValueError("strict vector plan belongs to another repository source")
    if planned.view_config_digest != policy.config_digest:
        raise ValueError("strict vector plan belongs to another view config")
    expected = _workspace_plan(
        source_digest=planned.source_digest,
        source_records=planned.source_records,
        output_records=planned.output_records,
        view_config_digest=planned.view_config_digest,
        repository_fingerprint=planned.repository_fingerprint,
    )
    if planned.plan != expected:
        raise ValueError("strict vector workspace plan is not canonical")


def _replay_vector_source(
    session: StrictWorkspaceSession,
    publication: PublicationDirectoryReader,
    *,
    planned: PlannedVectorView,
    policy: _VectorPolicy,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    if check_cancelled is not None:
        check_cancelled()
    ownership, source_records, reader = _captured_source_view(
        planned.source_ownership,
        publication,
        label="strict vector recapture source",
    )
    if (
        ownership != planned.source_ownership
        or source_records != planned.source_records
    ):
        raise ValueError("strict vector recapture source changed after planning")

    derived = _derived_vector(
        reader,
        policy=policy,
        check_cancelled=check_cancelled,
    )
    if (
        derived.output_records != planned.output_records
        or derived.model_suffix != planned.model_suffix
        or derived.counts != planned.counts
    ):
        raise RuntimeError("strict vector replay derivation differs from its plan")

    written: list[TreeFileRecord] = []
    root_name = f"config_{planned.model_suffix}.json"
    if check_cancelled is not None:
        check_cancelled()
    written.append(session.write_file(root_name, (derived.root_config_payload,)))
    for relative, payload in derived.level_config_payloads:
        if check_cancelled is not None:
            check_cancelled()
        written.append(session.write_file(relative, (payload,)))

    for level, count in planned.counts:
        if check_cancelled is not None:
            check_cancelled()
        if count <= 0:
            continue
        documents_relative = f"{level}/documents_{planned.model_suffix}.json"
        # `_derived_vector` has just authenticated, semantically validated,
        # canonicalized, and content-bound these exact source bytes.  Replay
        # the retained descriptor once instead of parsing a 256 MiB document
        # array a second time.
        with reader.open_file(
            documents_relative,
            max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        ) as source:
            written.append(
                session.write_file(
                    documents_relative,
                    _interruptible_chunks(
                        source.iter_bytes(chunk_size=_JSON_READ_CHUNK_BYTES),
                        check_cancelled,
                    ),
                )
            )
        index_relative = f"{level}/index_{planned.model_suffix}.faiss"
        with reader.open_file(
            index_relative,
            max_bytes=MAX_PORTABLE_FAISS_INDEX_BYTES,
        ) as source:
            written.append(
                session.write_file(
                    index_relative,
                    _interruptible_chunks(
                        source.iter_bytes(chunk_size=_JSON_READ_CHUNK_BYTES),
                        check_cancelled,
                    ),
                )
            )

    if check_cancelled is not None:
        check_cancelled()
    if tuple(sorted(written, key=lambda item: item.path)) != planned.output_records:
        raise RuntimeError("strict vector replay differs from its exact plan")


def _candidate_records(
    candidate: PublicationDirectoryReader,
    *,
    planned: PlannedVectorView,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    entry_policy: Any,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[TreeFileRecord, ...]:
    if check_cancelled is not None:
        check_cancelled()
    observed = candidate.file_records()
    observed_inventory = candidate.inventory()
    records_by_path = {record.path: record for record in observed}
    for relative, kind in observed_inventory:
        if kind == "file":
            record = records_by_path[relative]
            entry_policy(relative, kind, record.mode, record.size)
        else:
            entry_policy(relative, kind, 0, 0)
    expected_directories = {
        parent.as_posix()
        for record in planned.output_records
        for parent in PurePosixPath(record.path).parents
        if parent != PurePosixPath(".")
    }
    expected_inventory = {
        *((record.path, "file") for record in planned.output_records),
        *((relative, "directory") for relative in expected_directories),
    }
    if set(observed_inventory) != expected_inventory or observed != (
        planned.output_records
    ):
        raise RuntimeError("strict vector candidate differs from its exact plan")

    candidate_config = dict(view_config)
    candidate_config.update(planned.adjustments)
    _validate_content_bound_portable_query_view_reader_with_identity(
        candidate,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_type="vector",
        view_config=candidate_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
        _framework_sandwiched=True,
    )
    if check_cancelled is not None:
        check_cancelled()
    return observed


def _publish_recaptured_vector_view_with_identity(
    lexical_source: Path,
    lexical_destination: Path,
    *,
    planned: PlannedVectorView,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if check_cancelled is not None:
        check_cancelled()
    policy = _policy(
        repository_identity,
        view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    entry_policy = _raw_entry_policy(policy.model_suffix)
    if type(planned) is not PlannedVectorView:
        raise TypeError("strict vector execution requires a PlannedVectorView")
    _require_plan(
        planned,
        source_ownership=planned.source_ownership,
        repository_identity=repository_identity,
        policy=policy,
    )
    observed_source_ownership = capture_directory_ownership(
        lexical_source,
        entry_policy=entry_policy,
    )
    if check_cancelled is not None:
        check_cancelled()
    _require_plan(
        planned,
        source_ownership=observed_source_ownership,
        repository_identity=repository_identity,
        policy=policy,
    )
    request = StrictWorkspaceRequest(
        purpose="portable-vector-recapture",
        destination=lexical_destination,
        plan=planned.plan,
        destination_binding=None,
    )

    def operation(session: StrictWorkspaceSession) -> None:
        if check_cancelled is not None:
            check_cancelled()
        if session.request != request:
            raise RuntimeError("strict vector provider changed its request")

        def replay_source(publication: PublicationDirectoryReader) -> None:
            _replay_vector_source(
                session,
                publication,
                planned=planned,
                policy=policy,
                check_cancelled=check_cancelled,
            )

        def validate_candidate(candidate: PublicationDirectoryReader) -> None:
            with repository_source.read_session():
                if (
                    _candidate_records(
                        candidate,
                        planned=planned,
                        repository_source=repository_source,
                        repository_identity=repository_identity,
                        entry_policy=entry_policy,
                        view_config=policy.view_config,
                        forbidden_paths=forbidden_paths,
                        environ=policy.environment,
                        check_cancelled=check_cancelled,
                    )
                    != planned.output_records
                ):
                    raise RuntimeError("strict vector candidate is not canonical")

        with repository_source.read_session():
            reopen_authenticated_directory(
                lexical_source,
                planned.source_ownership,  # type: ignore[arg-type]
                replay_source,
            )
        if check_cancelled is not None:
            check_cancelled()
        session.publish_validated(validate_candidate)
        if check_cancelled is not None:
            check_cancelled()

    # Recheck mutable authority state immediately before a workspace may be
    # provisioned.  Planning never grants a provider permission to replace an
    # existing destination.
    _preflight_recapture_authorities(
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    if check_cancelled is not None:
        check_cancelled()
    _require_missing_destination(lexical_destination)
    run_strict_workspace(
        workspace_provider,
        request,
        receipt_owner=output_receipt_owner,
        operation=operation,
    )
    if check_cancelled is not None:
        check_cancelled()
    if not output_receipt_owner.active:
        raise RuntimeError("strict vector publication returned without a receipt")
    receipt = output_receipt_owner.receipt
    if receipt.path != lexical_destination or receipt.plan != planned.plan:
        raise RuntimeError("strict vector publication returned the wrong receipt")
    return planned.adjustments


def _publish_recaptured_vector_view(
    source: Path,
    destination: Path,
    *,
    planned: PlannedVectorView,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Privately publish one exact pre-planned schema-8 vector tree."""

    _preflight_recapture_authorities(
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    if check_cancelled is not None:
        check_cancelled()
    repository_identity = _authenticated_repository_identity(repository_source)
    lexical_source, lexical_destination = _recapture_locations(
        source,
        destination,
        repository_identity=repository_identity,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="schema-8 vector view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    if any(type(path) is not type(Path()) for path in forbidden_tail):
        raise TypeError("strict vector forbidden paths must be exact Path values")
    return _publish_recaptured_vector_view_with_identity(
        lexical_source,
        lexical_destination,
        planned=planned,
        repository_source=repository_source,
        repository_identity=repository_identity,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
        check_cancelled=check_cancelled,
    )


def recapture_vector_view_strict(
    source: Path,
    destination: Path,
    *,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Recapture one schema-8 compiler vector tree into a missing workspace."""

    _preflight_recapture_authorities(
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    repository_identity = _authenticated_repository_identity(repository_source)
    lexical_source, lexical_destination = _recapture_locations(
        source,
        destination,
        repository_identity=repository_identity,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="schema-8 vector view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    if any(type(path) is not type(Path()) for path in forbidden_tail):
        raise TypeError("strict vector forbidden paths must be exact Path values")
    planned = _plan_recaptured_vector_view_with_identity(
        lexical_source,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )
    _preflight_recapture_authorities(
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    lexical_source, lexical_destination = _recapture_locations(
        lexical_source,
        lexical_destination,
        repository_identity=repository_identity,
    )
    return _publish_recaptured_vector_view_with_identity(
        lexical_source,
        lexical_destination,
        planned=planned,
        repository_source=repository_source,
        repository_identity=repository_identity,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )


__all__ = [
    "PlannedVectorView",
    "recapture_vector_view_strict",
]
