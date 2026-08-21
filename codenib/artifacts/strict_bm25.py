# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strict, authority-bound publication for portable BM25 generations."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Iterable, Iterator, Mapping
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
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
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
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
)
from .security import assert_publishable_json_value

_MAX_CONFIG_JSON_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS_JSON_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_PATH_BYTES = 4_096
_MAX_SOURCE_PATH_COMPONENTS = 256
_JSON_READ_CHUNK_BYTES = 1024 * 1024
_WINDOWS_DRIVE_PREFIX = tuple(
    f"{letter}:" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_CURRENT_BM25_BUILDER_SCHEMA = 8
_STRICT_BM25_PLAN_DOMAIN = b"codenib-portable-bm25-strict-workspace-v1"
_STRICT_BM25_FILES = frozenset({"documents.json", "bm25_metadata.json"})


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
        raise TypeError("strict BM25 environment must be a mapping")
    snapshot = dict(source)
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in snapshot.items()
    ):
        raise TypeError("strict BM25 environment must contain text pairs")
    return snapshot


def _preflight_publication_authorities(
    *,
    source_generation: PublishedWorkspaceReceiptOwner,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
) -> None:
    """Reject unusable authority before config, source, or provider mutation."""

    if type(source_generation) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict BM25 source must be a workspace receipt owner")
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict BM25 repository source has an invalid type")
    if type(output_receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict BM25 output receipt owner has an invalid type")
    if source_generation is output_receipt_owner:
        raise ValueError("strict BM25 source and output receipt owners must differ")
    if not source_generation.active:
        raise RuntimeError("strict BM25 source generation must be active")
    if not repository_source.usable:
        raise RuntimeError("strict BM25 repository source is not usable")
    if output_receipt_owner.state != "empty":
        raise RuntimeError("strict BM25 output receipt owner must be empty")

    # Match run_strict_workspace: the platform gate precedes even provider
    # descriptor lookup. Provider support itself is invoked exactly once by
    # run_strict_workspace after caller inputs and the exact plan are frozen.
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


def _preflight_recapture_publication_authorities(
    *,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
) -> None:
    """Reject unusable recapture authority before source or provider mutation."""

    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict BM25 repository source has an invalid type")
    if type(output_receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict BM25 output receipt owner has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict BM25 repository source is not usable")
    if output_receipt_owner.state != "empty":
        raise RuntimeError("strict BM25 output receipt owner must be empty")

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
        raise TypeError("strict BM25 repository identity has an invalid type")
    return identity


def _require_disjoint_view_path(
    path: Path,
    repository_root: Path,
) -> Path:
    """Return one lexical view path that cannot contain or enter the repo."""

    candidate = lexical_directory_path(path)
    repository = lexical_directory_path(repository_root)

    def overlaps(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    if overlaps(candidate, repository):
        raise ValueError(
            "strict BM25 view must not overlap the source repository: " f"{candidate}"
        )
    try:
        physical_candidate = candidate.resolve(strict=False)
        physical_repository = repository.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("strict BM25 view location cannot be authenticated") from exc
    if overlaps(physical_candidate, physical_repository):
        raise ValueError(
            "strict BM25 view must not overlap the source repository: " f"{candidate}"
        )
    return candidate


def _preflight_view_location(
    destination: Path,
    *,
    source_generation: PublishedWorkspaceReceiptOwner,
    repository_identity: RepositorySourceIdentitySnapshot,
) -> tuple[PublishedWorkspaceReceipt, Path]:
    """Bind replacement to its source path before source bytes are consumed."""

    source_receipt = source_generation.receipt
    source_path = _require_disjoint_view_path(
        source_receipt.path,
        repository_identity.root,
    )
    lexical_destination = _require_disjoint_view_path(
        destination,
        repository_identity.root,
    )
    if lexical_destination != source_path:
        raise ValueError(
            "strict BM25 normalization must replace its exact source generation"
        )
    return source_receipt, lexical_destination


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
        raise ValueError(f"strict BM25 {label} cannot be authenticated") from exc


def _require_missing_recapture_destination(destination: Path) -> None:
    try:
        destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(
            "strict BM25 recapture destination cannot be authenticated"
        ) from exc
    raise FileExistsError("strict BM25 recapture destination must be missing")


def _recapture_locations(
    source: Path,
    destination: Path,
    *,
    repository_identity: RepositorySourceIdentitySnapshot,
) -> tuple[Path, Path]:
    """Authenticate denial-only topology for an ordinary cache recapture."""

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
    physical_source_relation = _path_relation(
        physical_source,
        physical_repository,
    )
    if source_relation != physical_source_relation:
        raise ValueError(
            "strict BM25 recapture source has inconsistent lexical and physical "
            "repository topology"
        )
    if source_relation in {"same", "ancestor"}:
        raise ValueError(
            "strict BM25 recapture source must not contain the source repository"
        )
    if source_relation == "descendant":
        relative = lexical_source.relative_to(repository).as_posix()
        prefix = relative + "/"
        if any(
            record.path == relative or record.path.startswith(prefix)
            for record in repository_identity.file_records
        ):
            raise ValueError(
                "strict BM25 recapture source inside the repository must be "
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
            "strict BM25 recapture destination must be disjoint from source and "
            "repository"
        )
    _require_missing_recapture_destination(lexical_destination)
    return lexical_source, lexical_destination


def _entry_policy(relative: str, kind: str, mode: int, size: int) -> None:
    del mode
    if kind != "file" or relative not in _STRICT_BM25_FILES:
        raise ValueError(f"portable BM25 view has an invalid entry: {relative}")
    limit = (
        _MAX_DOCUMENTS_JSON_BYTES
        if relative == "documents.json"
        else _MAX_CONFIG_JSON_BYTES
    )
    if size < 0 or size > limit:
        raise ValueError(
            f"portable BM25 entry exceeds its {limit}-byte limit: {relative}"
        )


class _PublicationBm25Reader:
    """BM25 facade over one callback-scoped publication reader."""

    def __init__(
        self,
        publication: PublicationDirectoryReader,
        ownership: object,
    ) -> None:
        self.root = Path("/__codenib_publication_bm25__")
        self.ownership = ownership
        self._publication = publication
        self._records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)  # type: ignore[arg-type]
        }

    def _relative(self, path: Path | PurePosixPath | str) -> PurePosixPath:
        if isinstance(path, Path):
            return PurePosixPath(path.relative_to(self.root).as_posix())
        return PurePosixPath(path)

    def record(self, path: Path | PurePosixPath | str) -> TreeFileRecord:
        relative = self._relative(path)
        try:
            return self._records[relative.as_posix()]
        except KeyError as exc:
            raise ValueError(
                f"strict BM25 view has no captured file: {relative}"
            ) from exc

    @contextmanager
    def open_file(
        self,
        path: Path | PurePosixPath | str,
        *,
        max_bytes: int,
    ) -> Iterator[PublicationAuthenticatedFile]:
        relative = self._relative(path)
        expected = self.record(relative)
        with self._publication.open_authenticated_file(
            relative,
            max_bytes=max_bytes,
        ) as source:
            yield source
        if source.record != expected:
            raise RuntimeError(f"strict BM25 file changed while reading: {relative}")

    def read_bytes(
        self,
        path: Path | PurePosixPath | str,
        *,
        max_bytes: int,
    ) -> bytes:
        payload = bytearray()
        with self.open_file(path, max_bytes=max_bytes) as source:
            for chunk in source.iter_bytes(chunk_size=_JSON_READ_CHUNK_BYTES):
                payload.extend(chunk)
        return bytes(payload)

    def verify_root(self) -> None:
        observed = self._publication.capture_ownership(entry_policy=_entry_policy)
        if observed != self.ownership:
            raise RuntimeError("strict BM25 view changed during validation")


def _load_json_object(
    reader: _PublicationBm25Reader,
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
    repository_root: Path,
    *,
    source: str,
    authenticated_source_files: frozenset[str],
) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{source} must be a text repository-relative path")
    raw = value
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{source} is not a valid UTF-8 path") from exc
    if len(encoded) > _MAX_SOURCE_PATH_BYTES:
        raise ValueError(f"{source} exceeds {_MAX_SOURCE_PATH_BYTES} UTF-8 path bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(f"{source} is not a portable repository-relative path")

    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.relative_to(repository_root)
        except ValueError as exc:
            raise ValueError(f"{source} points outside the repository") from exc
        raw = path.as_posix()
    else:
        native = path.as_posix()
        if native != raw:
            raw_parts = raw.replace("\\", "/").split("/")
            if any(part in {"", ".", ".."} for part in raw_parts):
                raise ValueError(f"{source} is not repository-relative")
            raw = native
        elif (
            raw.startswith("//") or raw.startswith(_WINDOWS_DRIVE_PREFIX) or "\\" in raw
        ):
            raise ValueError(f"{source} is not a portable repository-relative path")

    raw_parts = raw.split("/")
    normalized = PurePosixPath(raw)
    if (
        normalized.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw != normalized.as_posix()
        or len(normalized.parts) > _MAX_SOURCE_PATH_COMPONENTS
    ):
        raise ValueError(f"{source} is not repository-relative")
    normalized_text = normalized.as_posix()
    if normalized_text not in authenticated_source_files:
        raise ValueError(f"{source} is not in the authenticated repository source")
    return normalized_text


def _normalized_documents(
    reader: _PublicationBm25Reader,
    repository_root: Path,
    *,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
) -> Iterable[dict[str, Any]]:
    with reader.open_file(
        "documents.json",
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
    ) as source:
        documents = iter_bounded_json_array(
            source,
            label="portable BM25 documents",
        )
        for index, document in enumerate(documents):
            if not isinstance(document, dict) or set(document) != {
                "page_content",
                "metadata",
            }:
                raise ValueError(f"portable BM25 document {index} has an invalid shape")
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
                normalized_metadata["file"] = _source_path(
                    raw_file,
                    repository_root,
                    source=f"portable BM25 document {index} file",
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
                label=f"portable BM25 document {index}",
            )
            yield normalized


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
            if not isinstance(chunk, bytes):
                raise TypeError("strict BM25 output chunks must be bytes")
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(
                    f"strict BM25 output exceeds its {max_bytes}-byte limit: {relative}"
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
            "strict BM25 output iterator close also failed",
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


def _payloads(
    reader: _PublicationBm25Reader,
    repository_root: Path,
    *,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
) -> tuple[bytes, Iterable[bytes]]:
    # Apply the whole-file lexical budgets, including string and atom limits,
    # before the element iterator allocates any decoded document object.
    with reader.open_file(
        "documents.json",
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
    ) as source:
        validate_bounded_json_stream(
            source,
            label="portable BM25 documents",
            max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
        )
    metadata = _load_json_object(
        reader,
        "bm25_metadata.json",
        label="portable BM25 metadata",
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
    normalized_metadata = {
        "project_root": "source",
        "max_k": max_k,
        "language": language,
    }
    _assert_authenticated_publishable_json_value(
        normalized_metadata,
        forbidden_paths=forbidden_paths,
        environ=environ,
        label="portable BM25 metadata",
    )
    metadata_bytes = _bounded_canonical_json_bytes(
        normalized_metadata,
        max_bytes=_MAX_CONFIG_JSON_BYTES,
        label="portable BM25 metadata",
    )
    documents = canonical_json_array_chunks(
        _normalized_documents(
            reader,
            repository_root,
            forbidden_paths=forbidden_paths,
            environ=environ,
            authenticated_source_files=authenticated_source_files,
        )
    )
    return metadata_bytes, documents


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
    _frame(digest, b"domain", _STRICT_BM25_PLAN_DOMAIN)
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
    return WorkspacePlan(
        subject_digest=_subject_digest(
            source_digest=source_digest,
            source_records=source_records,
            output_records=output_records,
            view_config_digest=view_config_digest,
            repository_fingerprint=repository_fingerprint,
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
class PlannedBm25View:
    """Immutable exact output plan for one authenticated BM25 generation."""

    plan: WorkspacePlan
    source_ownership: object
    source_digest: str
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]
    view_config_digest: str
    repository_fingerprint: str

    def __post_init__(self) -> None:
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        if type(self.plan) is not WorkspacePlan:
            raise TypeError("strict BM25 plan must contain an exact WorkspacePlan")
        if any(type(record) is not TreeFileRecord for record in source_records):
            raise TypeError("strict BM25 source records have an invalid type")
        if any(type(record) is not TreeFileRecord for record in output_records):
            raise TypeError("strict BM25 output records have an invalid type")
        if source_records != tuple(sorted(source_records, key=lambda item: item.path)):
            raise ValueError("strict BM25 source records must be canonical")
        if output_records != tuple(sorted(output_records, key=lambda item: item.path)):
            raise ValueError("strict BM25 output records must be canonical")
        if {record.path for record in output_records} != _STRICT_BM25_FILES or any(
            record.mode != 0o600 for record in output_records
        ):
            raise ValueError("strict BM25 output records are not exact")
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)

    @property
    def adjustments(self) -> dict[str, Any]:
        return {
            "artifact_file_fingerprints": {
                record.path: {
                    "size": record.size,
                    "sha256": record.sha256,
                }
                for record in self.output_records
            }
        }


def _captured_source_view(
    expected_ownership: object,
    publication: PublicationDirectoryReader,
    *,
    label: str,
) -> tuple[object, tuple[TreeFileRecord, ...], _PublicationBm25Reader]:
    ownership = publication.capture_ownership(entry_policy=_entry_policy)
    if ownership != expected_ownership:
        raise RuntimeError(f"{label} changed")
    records = tuple(directory_ownership_file_records(ownership))  # type: ignore[arg-type]
    if {record.path for record in records} != _STRICT_BM25_FILES:
        raise ValueError(f"{label} is incomplete")
    return ownership, records, _PublicationBm25Reader(publication, ownership)


def _source_view(
    receipt: PublishedWorkspaceReceipt,
    publication: PublicationDirectoryReader,
) -> tuple[object, tuple[TreeFileRecord, ...], _PublicationBm25Reader]:
    return _captured_source_view(
        receipt.ownership,
        publication,
        label="strict BM25 source generation",
    )


def _repository_files(
    repository_identity: RepositorySourceIdentitySnapshot,
) -> frozenset[str]:
    records = repository_identity.file_records
    paths = frozenset(record.path for record in records)
    if len(paths) != len(records):
        raise RuntimeError("authenticated repository source repeats a file")
    return paths


def _policy(
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config: Mapping[str, Any],
    *,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> tuple[str, tuple[Path, ...], frozenset[str]]:
    has_selection_digest = "source_selection_digest" in view_config
    if (
        type(view_config.get("builder_schema")) is int
        and view_config.get("builder_schema") == _CURRENT_BM25_BUILDER_SCHEMA
        and not has_selection_digest
    ):
        raise ValueError("current BM25 view config requires a source selection digest")
    if has_selection_digest:
        source_selection = repository_identity.source_selection
        selection_digest = view_config.get("source_selection_digest")
        if (
            type(source_selection) is not RepositorySourceSelection
            or type(selection_digest) is not str
            or selection_digest != source_selection.digest
        ):
            raise ValueError(
                "BM25 view source selection differs from the authenticated "
                "repository source"
            )
    forbidden = (repository_identity.root, *forbidden_paths)
    assert_no_secret_fields(view_config, source="portable BM25 view config")
    _assert_authenticated_publishable_json_value(
        view_config,
        forbidden_paths=forbidden,
        environ=environ,
        label="portable BM25 view config",
    )
    config_bytes = _bounded_canonical_json_bytes(
        dict(view_config),
        max_bytes=_MAX_CONFIG_JSON_BYTES,
        label="portable BM25 view config",
    )
    return (
        hashlib.sha256(config_bytes).hexdigest(),
        forbidden,
        _repository_files(repository_identity),
    )


def _planned_view_from_publication(
    publication: PublicationDirectoryReader,
    expected_ownership: object,
    *,
    repository_identity: RepositorySourceIdentitySnapshot,
    config_digest: str,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
    source_label: str,
) -> PlannedBm25View:
    ownership, source_records, reader = _captured_source_view(
        expected_ownership,
        publication,
        label=source_label,
    )
    metadata_bytes, document_chunks = _payloads(
        reader,
        repository_identity.root,
        forbidden_paths=forbidden_paths,
        environ=environ,
        authenticated_source_files=authenticated_source_files,
    )
    documents_record = _record_chunks(
        "documents.json",
        document_chunks,
        max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
    )
    metadata_record = TreeFileRecord(
        path="bm25_metadata.json",
        mode=0o600,
        size=len(metadata_bytes),
        sha256=hashlib.sha256(metadata_bytes).hexdigest(),
    )
    reader.verify_root()
    output_records = tuple(
        sorted((documents_record, metadata_record), key=lambda item: item.path)
    )
    source_digest = directory_ownership_digest(ownership)  # type: ignore[arg-type]
    plan = _workspace_plan(
        source_digest=source_digest,
        source_records=source_records,
        output_records=output_records,
        view_config_digest=config_digest,
        repository_fingerprint=repository_identity.fingerprint,
    )
    return PlannedBm25View(
        plan=plan,
        source_ownership=ownership,
        source_digest=source_digest,
        source_records=source_records,
        output_records=output_records,
        view_config_digest=config_digest,
        repository_fingerprint=repository_identity.fingerprint,
    )


def _plan_bm25_view_with_identity(
    source_generation: PublishedWorkspaceReceiptOwner,
    *,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> PlannedBm25View:
    config_digest, forbidden, authenticated_files = _policy(
        repository_identity,
        view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )

    def consume_source(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> PlannedBm25View:
        return _planned_view_from_publication(
            publication,
            receipt.ownership,
            repository_identity=repository_identity,
            config_digest=config_digest,
            forbidden_paths=forbidden,
            environ=environ,
            authenticated_source_files=authenticated_files,
            source_label="strict BM25 source generation",
        )

    with repository_source.read_session():
        return source_generation.consume(consume_source)


def plan_bm25_view_strict(
    source_generation: PublishedWorkspaceReceiptOwner,
    *,
    repository_source: RepositorySourceBinding,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> PlannedBm25View:
    """Plan exact canonical BM25 bytes from one retained source generation."""

    if type(source_generation) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict BM25 source must be a workspace receipt owner")
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict BM25 repository source has an invalid type")
    if not source_generation.active:
        raise RuntimeError("strict BM25 source generation must be active")
    if not repository_source.usable:
        raise RuntimeError("strict BM25 repository source is not usable")
    repository_identity = _authenticated_repository_identity(repository_source)
    _require_disjoint_view_path(
        source_generation.receipt.path,
        repository_identity.root,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="portable BM25 view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    return _plan_bm25_view_with_identity(
        source_generation,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )


def _require_plan_for_ownership(
    planned: PlannedBm25View,
    *,
    source_ownership: object,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config_digest: str,
) -> None:
    if type(planned) is not PlannedBm25View:
        raise TypeError("strict BM25 execution requires a PlannedBm25View")
    if (
        planned.source_ownership != source_ownership
        or planned.source_records
        != tuple(directory_ownership_file_records(source_ownership))  # type: ignore[arg-type]
        or planned.source_digest
        != directory_ownership_digest(source_ownership)  # type: ignore[arg-type]
    ):
        raise ValueError("strict BM25 plan belongs to another source generation")
    if planned.repository_fingerprint != repository_identity.fingerprint:
        raise ValueError("strict BM25 plan belongs to another repository source")
    if planned.view_config_digest != view_config_digest:
        raise ValueError("strict BM25 plan belongs to another view config")
    expected = _workspace_plan(
        source_digest=planned.source_digest,
        source_records=planned.source_records,
        output_records=planned.output_records,
        view_config_digest=planned.view_config_digest,
        repository_fingerprint=planned.repository_fingerprint,
    )
    if planned.plan != expected:
        raise ValueError("strict BM25 workspace plan is not canonical")


def _require_plan(
    planned: PlannedBm25View,
    *,
    source_receipt: PublishedWorkspaceReceipt,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config_digest: str,
) -> None:
    _require_plan_for_ownership(
        planned,
        source_ownership=source_receipt.ownership,
        repository_identity=repository_identity,
        view_config_digest=view_config_digest,
    )


def _candidate_records(
    candidate: PublicationDirectoryReader,
    *,
    planned: PlannedBm25View,
    repository_identity: RepositorySourceIdentitySnapshot,
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    authenticated_source_files: frozenset[str],
) -> tuple[TreeFileRecord, ...]:
    ownership = candidate.capture_ownership(entry_policy=_entry_policy)
    observed = tuple(directory_ownership_file_records(ownership))  # type: ignore[arg-type]
    if (
        directory_ownership_inventory(ownership)  # type: ignore[arg-type]
        != (
            ("bm25_metadata.json", "file"),
            ("documents.json", "file"),
        )
        or observed != planned.output_records
    ):
        raise RuntimeError("strict BM25 candidate differs from its exact plan")
    reader = _PublicationBm25Reader(candidate, ownership)
    metadata_bytes, document_chunks = _payloads(
        reader,
        repository_identity.root,
        forbidden_paths=forbidden_paths,
        environ=environ,
        authenticated_source_files=authenticated_source_files,
    )
    records = tuple(
        sorted(
            (
                _record_chunks(
                    "documents.json",
                    document_chunks,
                    max_bytes=_MAX_DOCUMENTS_JSON_BYTES,
                ),
                TreeFileRecord(
                    path="bm25_metadata.json",
                    mode=0o600,
                    size=len(metadata_bytes),
                    sha256=hashlib.sha256(metadata_bytes).hexdigest(),
                ),
            ),
            key=lambda item: item.path,
        )
    )
    reader.verify_root()
    return records


def _publish_planned_bm25_view_with_identity(
    *,
    planned: PlannedBm25View,
    source_generation: PublishedWorkspaceReceiptOwner,
    source_receipt: PublishedWorkspaceReceipt,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    lexical_destination: Path,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    config_digest, forbidden, authenticated_files = _policy(
        repository_identity,
        view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    _require_plan(
        planned,
        source_receipt=source_receipt,
        repository_identity=repository_identity,
        view_config_digest=config_digest,
    )
    request = StrictWorkspaceRequest(
        purpose="portable-bm25-normalization",
        destination=lexical_destination,
        plan=planned.plan,
        destination_expectation="provider-bound-exact",
    )

    def operation(session: StrictWorkspaceSession) -> None:
        if session.request != request:
            raise RuntimeError("strict BM25 provider changed its request")

        def replay_source(
            receipt: PublishedWorkspaceReceipt,
            publication: PublicationDirectoryReader,
        ) -> None:
            ownership, source_records, reader = _source_view(receipt, publication)
            if (
                ownership != planned.source_ownership
                or source_records != planned.source_records
            ):
                raise ValueError("strict BM25 source changed after planning")
            metadata_bytes, document_chunks = _payloads(
                reader,
                repository_identity.root,
                forbidden_paths=forbidden,
                environ=environ,
                authenticated_source_files=authenticated_files,
            )
            written = tuple(
                sorted(
                    (
                        session.write_file("documents.json", document_chunks),
                        session.write_file("bm25_metadata.json", (metadata_bytes,)),
                    ),
                    key=lambda item: item.path,
                )
            )
            reader.verify_root()
            if written != planned.output_records:
                raise RuntimeError("strict BM25 replay differs from its exact plan")

        def validate_candidate(candidate: PublicationDirectoryReader) -> None:
            with repository_source.read_session():
                if (
                    _candidate_records(
                        candidate,
                        planned=planned,
                        repository_identity=repository_identity,
                        forbidden_paths=forbidden,
                        environ=environ,
                        authenticated_source_files=authenticated_files,
                    )
                    != planned.output_records
                ):
                    raise RuntimeError("strict BM25 candidate is not canonical")

        with repository_source.read_session():
            source_generation.consume(replay_source)
        session.publish_validated(validate_candidate)

    run_strict_workspace(
        workspace_provider,
        request,
        receipt_owner=output_receipt_owner,
        operation=operation,
    )
    if not output_receipt_owner.active:
        raise RuntimeError("strict BM25 publication returned without a receipt")
    return planned.adjustments


def publish_planned_bm25_view_strict(
    destination: Path,
    *,
    planned: PlannedBm25View,
    source_generation: PublishedWorkspaceReceiptOwner,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Replay and durably publish one exact strict BM25 plan."""

    _preflight_publication_authorities(
        source_generation=source_generation,
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    repository_identity = _authenticated_repository_identity(repository_source)
    source_receipt, lexical_destination = _preflight_view_location(
        destination,
        source_generation=source_generation,
        repository_identity=repository_identity,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="portable BM25 view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    return _publish_planned_bm25_view_with_identity(
        planned=planned,
        source_generation=source_generation,
        source_receipt=source_receipt,
        repository_source=repository_source,
        repository_identity=repository_identity,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        lexical_destination=lexical_destination,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )


def _plan_recaptured_bm25_view_with_identity(
    lexical_source: Path,
    *,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> PlannedBm25View:
    config_digest, forbidden, authenticated_files = _policy(
        repository_identity,
        view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    source_ownership = capture_directory_ownership(
        lexical_source,
        entry_policy=_entry_policy,
    )

    def consume_source(publication: PublicationDirectoryReader) -> PlannedBm25View:
        return _planned_view_from_publication(
            publication,
            source_ownership,
            repository_identity=repository_identity,
            config_digest=config_digest,
            forbidden_paths=forbidden,
            environ=environ,
            authenticated_source_files=authenticated_files,
            source_label="strict BM25 recapture source",
        )

    with repository_source.read_session():
        return reopen_authenticated_directory(
            lexical_source,
            source_ownership,  # type: ignore[arg-type]
            consume_source,
        )


def _plan_recaptured_bm25_view(
    source: Path,
    destination: Path,
    *,
    repository_source: RepositorySourceBinding,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> PlannedBm25View:
    """Privately pre-plan an ordinary BM25 tree without workspace mutation."""

    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict BM25 repository source has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict BM25 repository source is not usable")
    repository_identity = _authenticated_repository_identity(repository_source)
    lexical_source, _lexical_destination = _recapture_locations(
        source,
        destination,
        repository_identity=repository_identity,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="portable BM25 view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    return _plan_recaptured_bm25_view_with_identity(
        lexical_source,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )


def _publish_recaptured_bm25_view_with_identity(
    lexical_source: Path,
    lexical_destination: Path,
    *,
    planned: PlannedBm25View,
    repository_source: RepositorySourceBinding,
    repository_identity: RepositorySourceIdentitySnapshot,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    config_digest, forbidden, authenticated_files = _policy(
        repository_identity,
        view_config,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    if type(planned) is not PlannedBm25View:
        raise TypeError("strict BM25 execution requires a PlannedBm25View")
    _require_plan_for_ownership(
        planned,
        source_ownership=planned.source_ownership,
        repository_identity=repository_identity,
        view_config_digest=config_digest,
    )
    observed_source_ownership = capture_directory_ownership(
        lexical_source,
        entry_policy=_entry_policy,
    )
    _require_plan_for_ownership(
        planned,
        source_ownership=observed_source_ownership,
        repository_identity=repository_identity,
        view_config_digest=config_digest,
    )
    request = StrictWorkspaceRequest(
        purpose="portable-bm25-recapture",
        destination=lexical_destination,
        plan=planned.plan,
        destination_expectation="missing",
    )

    def operation(session: StrictWorkspaceSession) -> None:
        if session.request != request:
            raise RuntimeError("strict BM25 provider changed its request")

        def replay_source(publication: PublicationDirectoryReader) -> None:
            ownership, source_records, reader = _captured_source_view(
                planned.source_ownership,
                publication,
                label="strict BM25 recapture source",
            )
            if (
                ownership != planned.source_ownership
                or source_records != planned.source_records
            ):
                raise ValueError("strict BM25 recapture source changed after planning")
            metadata_bytes, document_chunks = _payloads(
                reader,
                repository_identity.root,
                forbidden_paths=forbidden,
                environ=environ,
                authenticated_source_files=authenticated_files,
            )
            written = tuple(
                sorted(
                    (
                        session.write_file("documents.json", document_chunks),
                        session.write_file("bm25_metadata.json", (metadata_bytes,)),
                    ),
                    key=lambda item: item.path,
                )
            )
            reader.verify_root()
            if written != planned.output_records:
                raise RuntimeError("strict BM25 replay differs from its exact plan")

        def validate_candidate(candidate: PublicationDirectoryReader) -> None:
            with repository_source.read_session():
                if (
                    _candidate_records(
                        candidate,
                        planned=planned,
                        repository_identity=repository_identity,
                        forbidden_paths=forbidden,
                        environ=environ,
                        authenticated_source_files=authenticated_files,
                    )
                    != planned.output_records
                ):
                    raise RuntimeError("strict BM25 candidate is not canonical")

        with repository_source.read_session():
            reopen_authenticated_directory(
                lexical_source,
                planned.source_ownership,  # type: ignore[arg-type]
                replay_source,
            )
        session.publish_validated(validate_candidate)

    # Recheck mutable authority state after the potentially long source scan
    # and immediately before the provider can provision its first workspace.
    _preflight_recapture_publication_authorities(
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    _require_missing_recapture_destination(lexical_destination)
    run_strict_workspace(
        workspace_provider,
        request,
        receipt_owner=output_receipt_owner,
        operation=operation,
    )
    if not output_receipt_owner.active:
        raise RuntimeError("strict BM25 publication returned without a receipt")
    receipt = output_receipt_owner.receipt
    if receipt.path != lexical_destination or receipt.plan != planned.plan:
        raise RuntimeError("strict BM25 publication returned the wrong receipt")
    return planned.adjustments


def _publish_recaptured_bm25_view(
    source: Path,
    destination: Path,
    *,
    planned: PlannedBm25View,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Privately publish one exact pre-planned ordinary BM25 tree."""

    _preflight_recapture_publication_authorities(
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
        label="portable BM25 view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    return _publish_recaptured_bm25_view_with_identity(
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


def recapture_bm25_view_strict(
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
    """Recapture one ordinary BM25 tree into a missing strict workspace."""

    _preflight_recapture_publication_authorities(
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
        label="portable BM25 view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    planned = _plan_recaptured_bm25_view_with_identity(
        lexical_source,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )
    _preflight_recapture_publication_authorities(
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    lexical_source, lexical_destination = _recapture_locations(
        lexical_source,
        lexical_destination,
        repository_identity=repository_identity,
    )
    return _publish_recaptured_bm25_view_with_identity(
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


def normalize_owned_query_view_strict(
    destination: Path,
    *,
    source_generation: PublishedWorkspaceReceiptOwner,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_type: str,
    view_config: Mapping[str, Any],
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Plan and publish BM25 through retained source and workspace authority."""

    if view_type != "bm25":
        raise ValueError("strict portable normalization currently supports only bm25")
    _preflight_publication_authorities(
        source_generation=source_generation,
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    repository_identity = _authenticated_repository_identity(repository_source)
    source_receipt, lexical_destination = _preflight_view_location(
        destination,
        source_generation=source_generation,
        repository_identity=repository_identity,
    )
    config_snapshot = _json_object_snapshot(
        view_config,
        label="portable BM25 view config",
    )
    environment = _environment_snapshot(environ)
    forbidden_tail = tuple(forbidden_paths)
    planned = _plan_bm25_view_with_identity(
        source_generation,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )
    _preflight_publication_authorities(
        source_generation=source_generation,
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    return _publish_planned_bm25_view_with_identity(
        planned=planned,
        source_generation=source_generation,
        source_receipt=source_receipt,
        repository_source=repository_source,
        repository_identity=repository_identity,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        lexical_destination=lexical_destination,
        view_config=config_snapshot,
        forbidden_paths=forbidden_tail,
        environ=environment,
    )


__all__ = [
    "PlannedBm25View",
    "normalize_owned_query_view_strict",
    "plan_bm25_view_strict",
    "publish_planned_bm25_view_strict",
    "recapture_bm25_view_strict",
]
