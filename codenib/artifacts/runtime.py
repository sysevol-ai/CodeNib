# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Verify and bind portable context artifacts for query-only runtimes."""

from __future__ import annotations

import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, NoReturn

from .._atomic_directory import (
    PublicationAuthenticatedFile,
    PublicationDirectoryReader,
    TreeFileRecord,
    capture_directory_ownership,
    directory_ownership_file_records,
    lexical_directory_path,
    reopen_authenticated_directory,
)
from .._bounded_json import iter_bounded_json_array
from .._contained_source import _attach_source_cleanup_owner
from ..compiler.checkout_identity import checkout_commit
from ..compiler.manifest import MANIFEST_FILENAME, MANIFEST_VERSION, RepoManifest
from ..compiler.snapshot_store import normalize_repo
from ..source_fingerprint import (
    RepositorySourceBinding,
    _SourceLifecycleRLock,
    _SourceLockLease,
    capture_repository_source,
    is_secure_source_fingerprint_v2,
    lexical_repository_path,
)
from .context import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    PORTABLE_CONTEXT_VIEWS,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_FINGERPRINT_RE = re.compile(r"^(?:sha256|sha256-v2):[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$")
_DEFAULT_MAX_FILES = 100_000
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS_BYTES = 256 * 1024 * 1024


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate context artifact JSON key: {key}")
        result[key] = value
    return result


def _bounded_json_int(value: str) -> int:
    if len(value) > 1_024:
        raise ValueError("context artifact JSON integer is too large")
    return int(value)


def _bounded_json_float(value: str) -> float:
    if len(value) > 1_024:
        raise ValueError("context artifact JSON number is too large")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("context artifact JSON number is not finite")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"context artifact JSON constant is not finite: {value}")


@dataclass(frozen=True, slots=True)
class VerifiedContextArtifact:
    """Integrity-checked, still-unbound portable context artifact."""

    root: Path
    ownership: object = dataclass_field(repr=False, compare=False)
    metadata_path: Path
    manifest_path: Path
    metadata: Mapping[str, Any]
    manifest: RepoManifest
    repository: str
    commit: str
    source_fingerprint: str
    views: tuple[str, ...]
    source_paths: tuple[str, ...]
    file_count: int
    byte_count: int


class SourceBindingCleanupOwner:
    """Explicit, retryable owner for source bindings awaiting cleanup."""

    def __init__(self) -> None:
        self._sources: list[object] = []

    def retain(self, source: object) -> None:
        close = getattr(source, "close", None)
        if not callable(close):
            raise TypeError("source cleanup resource must provide close()")
        if not any(candidate is source for candidate in self._sources):
            self._sources.append(source)

    @property
    def pending_sources(self) -> tuple[RepositorySourceBinding, ...]:
        pending: list[RepositorySourceBinding] = []
        for source in self._sources:
            nested = getattr(source, "pending_sources", None)
            if nested is None:
                pending.append(source)  # type: ignore[arg-type]
            else:
                pending.extend(nested)
        return tuple(pending)

    @property
    def closed(self) -> bool:
        return not self._sources

    def close(self) -> None:
        """Visit every source and retain only authorities that remain open."""

        first_failure: BaseException | None = None
        pending: list[object] = []
        for source in tuple(self._sources):
            try:
                source.close()  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - cleanup all sources
                if first_failure is None:
                    first_failure = exc
            try:
                fully_closed = bool(source.closed)  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - retain uncertain owner
                fully_closed = False
                if first_failure is None:
                    first_failure = exc
            if not fully_closed:
                pending.append(source)
        self._sources[:] = pending
        if first_failure is not None:
            raise first_failure


def _source_cleanup_owner_is_pending(owner: object | None) -> bool:
    """Treat uncertain cleanup ownership as pending and therefore non-degradable."""

    if owner is None:
        return False
    try:
        return not bool(owner.closed)  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - uncertain ownership is pending
        return True


def _raise_source_cleanup_failure(
    primary: BaseException,
    cleanup_failure: BaseException | None,
    pending_owner: object | None,
) -> NoReturn:
    """Apply source cleanup priority and retain every still-pending owner."""

    actual = primary
    if (
        cleanup_failure is not None
        and isinstance(primary, Exception)
        and not isinstance(cleanup_failure, Exception)
    ):
        actual = cleanup_failure

    prior_owner = getattr(primary, "source_cleanup_owner", None)
    if _source_cleanup_owner_is_pending(prior_owner):
        _attach_source_cleanup_owner(actual, prior_owner)
    if _source_cleanup_owner_is_pending(pending_owner):
        _attach_source_cleanup_owner(actual, pending_owner)

    if actual is cleanup_failure:
        raise actual from primary
    if cleanup_failure is not None:
        raise actual from cleanup_failure
    raise actual


_SOURCE_BINDING_AVAILABLE = object()
_SOURCE_BINDING_CLEANUP_PENDING = object()
_SOURCE_BINDING_CONSUMED = object()


@dataclass(slots=True)
class ContextArtifactBinding:
    """Verified artifact rebound to exact source bytes, not mutable Git state."""

    artifact: VerifiedContextArtifact
    repo_path: Path | None
    manifest: RepoManifest
    checkout_commit_observed: str | None = None
    _source_binding: RepositorySourceBinding | None = dataclass_field(
        default=None,
        repr=False,
        compare=False,
    )
    _transfer_lock: _SourceLifecycleRLock = dataclass_field(
        default_factory=_SourceLifecycleRLock,
        init=False,
        repr=False,
        compare=False,
    )
    _source_state: tuple[object, RepositorySourceBinding | None] = dataclass_field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        self._source_state = (_SOURCE_BINDING_AVAILABLE, self._source_binding)
        self._source_binding = None

    @property
    def source_binding(self) -> RepositorySourceBinding | None:
        """Return a non-owning snapshot for diagnostics only."""

        with self._transfer_lease():
            state, source = self._source_state
            return source if state is _SOURCE_BINDING_AVAILABLE else None

    @property
    def source_bound(self) -> bool:
        with self._transfer_lease():
            state, source = self._source_state
            return state is _SOURCE_BINDING_AVAILABLE and source is not None

    @contextmanager
    def _transfer_lease(self) -> Iterator[None]:
        with _SourceLockLease(self._transfer_lock) as acquisition_failure:
            if acquisition_failure is not None:
                raise acquisition_failure
            yield

    @property
    def source_verification_scope(self) -> str | None:
        """Describe the authority retained for later source reads."""

        return "content-bytes" if self.source_bound else None

    @property
    def commit_verified(self) -> bool:
        """Mutable Git HEAD is not attested by an M1 content binding."""

        return False

    def install_source_binding(
        self,
        install: Callable[[RepositorySourceBinding | None], None],
        *,
        expected: RepositorySourceBinding | None = None,
        require_expected: bool = False,
    ) -> None:
        """Install the one-shot source into an already-stable runtime owner.

        The callback must publish its argument in the destination owner before
        returning.  Unlike returning the capability, this keeps the destination
        reachable if cancellation lands between this method's return and the
        caller's next bytecode.
        """

        source: RepositorySourceBinding | None = None
        try:
            with self._transfer_lease():
                state, candidate = self._source_state
                if state is not _SOURCE_BINDING_AVAILABLE:
                    raise RuntimeError(
                        "context artifact source authority was already transferred"
                    )
                if require_expected and candidate is not expected:
                    raise ValueError(
                        "artifact source authority does not match its binding"
                    )
                source = candidate
                # Retain an explicit cleanup owner while the callback installs
                # the source. A failed or interrupted installation remains
                # retryable through close().
                self._source_state = (_SOURCE_BINDING_CLEANUP_PENDING, source)
                install(source)
                self._source_state = (_SOURCE_BINDING_CONSUMED, None)
        except BaseException:  # noqa: B036 - close unreturned capability
            self._recover_source_install(source)
            raise

    def _recover_source_install(
        self,
        source: RepositorySourceBinding | None,
    ) -> None:
        """Close or retain a source whose destination install did not finish."""

        try:
            with _SourceLockLease(self._transfer_lock):
                state, owned = self._source_state
                if state is not _SOURCE_BINDING_CLEANUP_PENDING or owned is not source:
                    return
                if source is not None:
                    try:
                        source.close()
                    except BaseException:  # noqa: B036 - preserve primary
                        pass
                self._source_state = (
                    (_SOURCE_BINDING_CONSUMED, None)
                    if source is None or source.closed
                    else (_SOURCE_BINDING_CLEANUP_PENDING, source)
                )
        except BaseException:  # noqa: B036 - binding still retains pending owner
            return

    def close(self) -> None:
        """Close an unconsumed live-source authority; query bindings are no-op."""

        deferred: BaseException | None = None
        try:
            with _SourceLockLease(self._transfer_lock) as acquisition_failure:
                if acquisition_failure is not None:
                    deferred = acquisition_failure
                state, source = self._source_state
                if state in {
                    _SOURCE_BINDING_AVAILABLE,
                    _SOURCE_BINDING_CLEANUP_PENDING,
                }:
                    if source is None:
                        self._source_state = (_SOURCE_BINDING_CONSUMED, None)
                    else:
                        try:
                            source.close()
                        except BaseException as exc:  # noqa: B036 - retain owner
                            if deferred is None:
                                deferred = exc
                        self._source_state = (
                            (_SOURCE_BINDING_CONSUMED, None)
                            if source.closed
                            else (_SOURCE_BINDING_CLEANUP_PENDING, source)
                        )
        except BaseException as exc:  # noqa: B036 - state retains retry owner
            if deferred is None:
                deferred = exc
        if deferred is not None:
            raise deferred

    def __enter__(self) -> ContextArtifactBinding:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _PublicationContextReader:
    """Context-artifact facade over an authority-bound publication reader."""

    def __init__(
        self,
        publication: PublicationDirectoryReader,
        ownership: object,
        *,
        entry_policy_factory: Callable[[], Callable[[str, str, int, int], None]],
    ) -> None:
        self._publication = publication
        self._ownership = ownership
        self._entry_policy_factory = entry_policy_factory
        self._records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)  # type: ignore[arg-type]
        }

    def _record(self, relative: str | PurePosixPath) -> TreeFileRecord:
        normalized = PurePosixPath(relative) if isinstance(relative, str) else relative
        try:
            return self._records[normalized.as_posix()]
        except KeyError as exc:
            raise ValueError(
                f"context artifact has no initial file record: {normalized}"
            ) from exc

    @contextmanager
    def open_file(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> Iterator[PublicationAuthenticatedFile]:
        normalized = PurePosixPath(relative) if isinstance(relative, str) else relative
        expected = self._record(normalized)
        with self._publication.open_authenticated_file(
            normalized,
            max_bytes=max_bytes,
        ) as source:
            yield source
        if source.record != expected:
            raise ValueError(
                f"context artifact file differs from its initial record: {normalized}"
            )

    def read_bytes(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        payload = bytearray()
        with self.open_file(relative, max_bytes=max_bytes) as source:
            for chunk in source.iter_bytes():
                payload.extend(chunk)
        return bytes(payload)

    def verify_root(self) -> None:
        observed = self._publication.capture_ownership(
            entry_policy=self._entry_policy_factory(),
        )
        if observed != self._ownership:
            raise ValueError("context artifact changed during verification")

    def close(self) -> None:
        return None


_ContextReader = _PublicationContextReader


def _load_json_object(
    reader: _ContextReader,
    relative: str | PurePosixPath,
    *,
    label: str,
    max_bytes: int,
) -> dict[str, Any]:
    try:
        value = json.loads(
            reader.read_bytes(relative, max_bytes=max_bytes),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_json_constant,
            parse_int=_bounded_json_int,
            parse_float=_bounded_json_float,
        )
    except ValueError as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {relative}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {relative}")
    return value


def _relative_path(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    return path


def _artifact_path(root: Path, value: object, *, label: str) -> Path:
    relative = _relative_path(value, label=label)
    return root.joinpath(*relative.parts)


def _inventory(
    metadata: Mapping[str, Any],
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[dict[str, tuple[int, str]], int]:
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("context artifact inventory must be a non-empty list")
    if len(raw_files) > max_files:
        raise ValueError(f"context artifact inventory exceeds {max_files} files")

    result: dict[str, tuple[int, str]] = {}
    total_bytes = 0
    for index, record in enumerate(raw_files):
        if not isinstance(record, dict):
            raise ValueError(f"context artifact file record {index} must be an object")
        relative = _relative_path(
            record.get("path"),
            label=f"context artifact file record {index} path",
        ).as_posix()
        if relative == CONTEXT_ARTIFACT_MANIFEST:
            raise ValueError("context artifact metadata cannot inventory itself")
        if relative in result:
            raise ValueError(f"duplicate context artifact inventory path: {relative}")
        size = record.get("bytes")
        digest = record.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"invalid byte size for context artifact file: {relative}")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 for context artifact file: {relative}")
        if PurePosixPath(relative).suffix.lower() in {".pickle", ".pkl"}:
            raise ValueError(
                f"portable context artifacts must not contain pickle: {relative}"
            )
        total_bytes += size
        if total_bytes > max_bytes:
            raise ValueError(f"context artifact exceeds {max_bytes} inventoried bytes")
        result[relative] = (size, digest)
    return result, total_bytes


def _repository_identity(metadata: Mapping[str, Any]) -> tuple[str, str, str]:
    repository = metadata.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("context artifact repository identity must be an object")
    slug = repository.get("slug")
    commit = repository.get("commit")
    source_fingerprint = repository.get("source_fingerprint")
    if not isinstance(slug, str):
        raise ValueError("context artifact repository slug must be a string")
    try:
        normalized_slug = normalize_repo(slug)
    except ValueError as exc:
        raise ValueError("context artifact repository slug is invalid") from exc
    if slug != normalized_slug or not _REPOSITORY_RE.fullmatch(slug):
        raise ValueError("context artifact repository slug is not canonical")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("context artifact commit must be a full lowercase Git SHA")
    if not isinstance(source_fingerprint, str) or not _SOURCE_FINGERPRINT_RE.fullmatch(
        source_fingerprint
    ):
        raise ValueError("context artifact source fingerprint is invalid")
    return slug, commit, source_fingerprint


def _artifact_views(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    raw_views = metadata.get("views")
    if not isinstance(raw_views, list) or not raw_views:
        raise ValueError("context artifact views must be a non-empty list")
    if not all(isinstance(view, str) for view in raw_views):
        raise ValueError("context artifact view names must be strings")
    views = tuple(raw_views)
    if len(set(views)) != len(views):
        raise ValueError("context artifact view names must be unique")
    unsupported = sorted(set(views) - PORTABLE_CONTEXT_VIEWS)
    if unsupported:
        raise ValueError(
            "context artifact contains unsupported portable views: "
            + ", ".join(unsupported)
        )
    return views


def _source_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be a repository-relative POSIX path") from exc
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) > 256
        or len(encoded) > 4_096
    ):
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    return path.as_posix()


def _document_source_paths(
    reader: _ContextReader,
    relative: str | PurePosixPath,
    *,
    label: str,
    max_bytes: int,
) -> set[str]:
    result: set[str] = set()
    with reader.open_file(relative, max_bytes=max_bytes) as source:
        for index, document in enumerate(iter_bounded_json_array(source, label=label)):
            if not isinstance(document, dict):
                raise ValueError(f"{label} document {index} must be an object")
            page_content = document.get("page_content")
            metadata = document.get("metadata")
            if not isinstance(page_content, str) or not isinstance(metadata, dict):
                raise ValueError(
                    f"{label} document {index} has invalid content or metadata"
                )
            raw_file = metadata.get("file")
            if raw_file is not None and raw_file != "":
                result.add(
                    _source_path(
                        raw_file,
                        label=f"{label} document {index} file",
                    )
                )
            for field in ("start_line", "end_line"):
                value = metadata.get(field)
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    raise ValueError(f"{label} document {index} has invalid {field}")
    return result


def _validate_view_payloads(
    root: Path,
    reader: _ContextReader,
    inventory: Mapping[str, tuple[int, str]],
    *,
    views: tuple[str, ...],
) -> tuple[str, ...]:
    source_paths: set[str] = set()
    inventory_paths = set(inventory)

    if "bm25" in views:
        metadata_relative = "views/bm25/bm25_metadata.json"
        documents_relative = "views/bm25/documents.json"
        required = {metadata_relative, documents_relative}
        if not required <= inventory_paths:
            raise ValueError("portable BM25 view is missing its serving files")
        metadata = _load_json_object(
            reader,
            metadata_relative,
            label="portable BM25 metadata",
            max_bytes=_MAX_METADATA_BYTES,
        )
        if metadata.get("project_root") != "source":
            raise ValueError("portable BM25 project root must be 'source'")
        source_paths.update(
            _document_source_paths(
                reader,
                documents_relative,
                label="portable BM25 documents",
                max_bytes=_MAX_DOCUMENTS_BYTES,
            )
        )

    if "vector" in views:
        document_paths = sorted(
            relative
            for relative in inventory_paths
            if PurePosixPath(relative).parent.name in {"l0", "l2"}
            and PurePosixPath(relative).parent.parent.as_posix() == "views/vector"
            and PurePosixPath(relative).name.startswith("documents_")
            and PurePosixPath(relative).suffix == ".json"
        )
        if not document_paths:
            raise ValueError("portable vector view has no JSON document store")
        for relative in document_paths:
            path = PurePosixPath(relative)
            suffix = path.name.removeprefix("documents_").removesuffix(".json")
            index_relative = (path.parent / f"index_{suffix}.faiss").as_posix()
            if not suffix or index_relative not in inventory_paths:
                raise ValueError(
                    f"portable vector documents have no matching FAISS index: {relative}"
                )
            source_paths.update(
                _document_source_paths(
                    reader,
                    path,
                    label=f"portable vector documents {relative}",
                    max_bytes=_MAX_DOCUMENTS_BYTES,
                )
            )
    return tuple(sorted(source_paths))


def _validate_manifest(
    root: Path,
    reader: _ContextReader,
    metadata: Mapping[str, Any],
    *,
    commit: str,
    source_fingerprint: str,
    views: tuple[str, ...],
    inventoried_paths: set[str],
) -> tuple[Path, RepoManifest]:
    manifest_record = metadata.get("manifest")
    if not isinstance(manifest_record, dict):
        raise ValueError("context artifact manifest descriptor must be an object")
    if manifest_record.get("repository_path") != "source":
        raise ValueError("context artifact manifest repository path must be 'source'")
    if manifest_record.get("paths") != "artifact-relative-posix":
        raise ValueError("context artifact manifest path contract is unsupported")
    if manifest_record.get("path") != MANIFEST_FILENAME:
        raise ValueError("context artifact manifest filename is unsupported")
    manifest_path = _artifact_path(
        root,
        manifest_record.get("path"),
        label="context artifact manifest path",
    )
    manifest_relative = manifest_path.relative_to(root).as_posix()
    if manifest_relative not in inventoried_paths:
        raise ValueError("context artifact manifest is absent from its inventory")
    try:
        manifest_payload = _load_json_object(
            reader,
            manifest_relative,
            label="context artifact repository manifest",
            max_bytes=_MAX_METADATA_BYTES,
        )
        manifest = RepoManifest.from_dict(manifest_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("context artifact repository manifest is invalid") from exc
    if manifest.version != MANIFEST_VERSION:
        raise ValueError(
            "context artifact repository manifest version is incompatible: "
            f"expected {MANIFEST_VERSION}, found {manifest.version}"
        )
    if manifest.repo_path != "source":
        raise ValueError("portable repository manifest path must be 'source'")
    if (
        not isinstance(manifest.file_count, int)
        or isinstance(manifest.file_count, bool)
        or manifest.file_count < 0
    ):
        raise ValueError("context artifact repository file count is invalid")
    if manifest.commit != commit or manifest.source_fingerprint != source_fingerprint:
        raise ValueError("context artifact and repository manifest identities differ")
    if set(manifest.indexes) != set(views):
        raise ValueError(
            "context artifact view list differs from its repository manifest"
        )

    metadata_capabilities = metadata.get("capabilities")
    if (
        not isinstance(metadata_capabilities, dict)
        or not all(isinstance(value, bool) for value in metadata_capabilities.values())
        or metadata_capabilities != manifest.capabilities
    ):
        raise ValueError("context artifact capability records differ")
    metadata_languages = metadata.get("repository", {}).get("languages")
    if (
        not isinstance(metadata_languages, list)
        or not all(isinstance(language, str) for language in metadata_languages)
        or metadata_languages != manifest.languages
    ):
        raise ValueError("context artifact language records differ")
    source_locations = metadata.get("source_locations")
    if not isinstance(source_locations, dict) or source_locations != {
        "path": "repository-relative-posix",
        "line_base": 1,
        "end_line": "inclusive",
        "commit": commit,
    }:
        raise ValueError("context artifact source-location contract is unsupported")
    builder = metadata.get("builder")
    if (
        not isinstance(builder, dict)
        or builder.get("manifest_version") != MANIFEST_VERSION
        or not isinstance(builder.get("codenib_version"), str)
        or not isinstance(builder.get("compiled_at"), str)
    ):
        raise ValueError("context artifact builder identity is invalid")

    for view in views:
        entry = manifest.indexes[view]
        if not isinstance(entry.config, dict) or not isinstance(entry.metadata, dict):
            raise ValueError(f"context artifact view metadata is invalid: {view}")
        if not manifest.index_is_current(view):
            raise ValueError(f"context artifact view is not current: {view}")
        if entry.index_type != view:
            raise ValueError(f"context artifact view type differs from its key: {view}")
        expected_view_path = f"views/{view}"
        if entry.path != expected_view_path:
            raise ValueError(
                f"context artifact {view} view path must be {expected_view_path!r}"
            )
        view_path = _artifact_path(
            root,
            entry.path,
            label=f"context artifact {view} view path",
        )
        view_relative = view_path.relative_to(root).as_posix()
        if not any(
            relative == view_relative or relative.startswith(f"{view_relative}/")
            for relative in inventoried_paths
        ):
            raise ValueError(f"context artifact view has no inventoried files: {view}")
        if view == "vector" and entry.config.get("portable_document_format") != (
            "codenib.vector-documents.v1"
        ):
            raise ValueError("portable vector document format is unsupported")
    return manifest_path, manifest


def _validate_context_limits(max_files: int, max_bytes: int) -> None:
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files <= 0
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
    ):
        raise ValueError("context artifact limits must be positive integers")


def _context_entry_policy(
    *,
    max_files: int,
    max_bytes: int,
) -> Callable[[str, str, int, int], None]:
    file_count = [0]
    captured_bytes = [0]

    def policy(relative: str, kind: str, mode: int, size: int) -> None:
        del mode
        if kind != "file":
            return
        file_count[0] += 1
        if file_count[0] > max_files + 1:
            raise ValueError(
                f"context artifact contains more than {max_files} inventoried files"
            )
        name = PurePosixPath(relative).name
        if name.startswith("documents") and name.endswith(".json"):
            per_file_limit = _MAX_DOCUMENTS_BYTES
        elif relative == CONTEXT_ARTIFACT_MANIFEST or name.endswith(".json"):
            per_file_limit = _MAX_METADATA_BYTES
        else:
            per_file_limit = max_bytes
        if size < 0 or size > per_file_limit:
            raise ValueError(
                f"context artifact file exceeds its {per_file_limit}-byte limit: "
                f"{relative}"
            )
        captured_bytes[0] += size
        if captured_bytes[0] > max_bytes + _MAX_METADATA_BYTES:
            raise ValueError("context artifact exceeds its captured byte limit")

    return policy


def _verify_context_artifact_reader(
    root: Path,
    ownership: object,
    reader: _ContextReader,
    *,
    expected_repository: str | None,
    expected_commit: str | None,
    max_files: int,
    max_bytes: int,
    capture_final: Callable[[], object],
) -> VerifiedContextArtifact:
    metadata_path = root / CONTEXT_ARTIFACT_MANIFEST
    try:
        metadata = _load_json_object(
            reader,
            CONTEXT_ARTIFACT_MANIFEST,
            label="context artifact metadata",
            max_bytes=_MAX_METADATA_BYTES,
        )
        if metadata.get("schema") != CONTEXT_ARTIFACT_SCHEMA:
            raise ValueError(
                "context artifact schema is incompatible: "
                f"expected {CONTEXT_ARTIFACT_SCHEMA}, "
                f"found {metadata.get('schema')!r}"
            )
        repository, commit, source_fingerprint = _repository_identity(metadata)
        views = _artifact_views(metadata)
        inventory, byte_count = _inventory(
            metadata,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)  # type: ignore[arg-type]
        }
        expected_files = set(inventory) | {CONTEXT_ARTIFACT_MANIFEST}
        actual_files = set(records)
        if actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            extra = sorted(actual_files - expected_files)
            raise ValueError(
                "context artifact file set differs from its inventory: "
                f"missing={missing}, extra={extra}"
            )
        for relative, (expected_size, expected_digest) in inventory.items():
            record = records[relative]
            if record.size != expected_size or record.sha256 != expected_digest:
                raise ValueError(f"context artifact file digest mismatch: {relative}")

        manifest_path, manifest = _validate_manifest(
            root,
            reader,
            metadata,
            commit=commit,
            source_fingerprint=source_fingerprint,
            views=views,
            inventoried_paths=set(inventory),
        )
        source_paths = _validate_view_payloads(
            root,
            reader,
            inventory,
            views=views,
        )
        if expected_repository is not None:
            expected = normalize_repo(expected_repository)
            if repository != expected:
                raise ValueError(
                    "context artifact repository mismatch: "
                    f"expected {expected}, found {repository}"
                )
        if expected_commit is not None:
            expected = expected_commit.strip().lower()
            if not _COMMIT_RE.fullmatch(expected):
                raise ValueError(
                    "expected context artifact commit must be a full Git SHA"
                )
            if commit != expected:
                raise ValueError(
                    "context artifact commit mismatch: "
                    f"expected {expected[:12]}, found {commit[:12]}"
                )
        reader.verify_root()
        if capture_final() != ownership:
            raise ValueError("context artifact changed during verification")
    finally:
        reader.close()

    return VerifiedContextArtifact(
        root=root,
        ownership=ownership,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        metadata=metadata,
        manifest=manifest,
        repository=repository,
        commit=commit,
        source_fingerprint=source_fingerprint,
        views=views,
        source_paths=source_paths,
        file_count=len(inventory),
        byte_count=byte_count,
    )


def verify_context_artifact_reader(
    publication: PublicationDirectoryReader,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    max_files: int = _DEFAULT_MAX_FILES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> VerifiedContextArtifact:
    """Verify an artifact through a publication authority without a path reopen."""

    _validate_context_limits(max_files, max_bytes)
    policy_factory = lambda: _context_entry_policy(  # noqa: E731
        max_files=max_files,
        max_bytes=max_bytes,
    )
    ownership = publication.capture_ownership(entry_policy=policy_factory())
    reader = _PublicationContextReader(
        publication,
        ownership,
        entry_policy_factory=policy_factory,
    )
    return _verify_context_artifact_reader(
        Path("/__codenib_context_artifact__"),
        ownership,
        reader,
        expected_repository=expected_repository,
        expected_commit=expected_commit,
        max_files=max_files,
        max_bytes=max_bytes,
        capture_final=lambda: publication.capture_ownership(
            entry_policy=policy_factory(),
        ),
    )


def verify_context_artifact(
    root: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    max_files: int = _DEFAULT_MAX_FILES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> VerifiedContextArtifact:
    """Verify an artifact completely without opening any persisted index."""

    _validate_context_limits(max_files, max_bytes)
    resolved = lexical_directory_path(Path(root))
    policy_factory = lambda: _context_entry_policy(  # noqa: E731
        max_files=max_files,
        max_bytes=max_bytes,
    )

    try:
        ownership = capture_directory_ownership(
            resolved,
            entry_policy=policy_factory(),
        )
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "context artifact directory could not be captured safely; it may "
            f"contain a symbolic link or unstable content: {resolved}"
        ) from exc

    def verify(publication: PublicationDirectoryReader) -> VerifiedContextArtifact:
        reader = _PublicationContextReader(
            publication,
            ownership,
            entry_policy_factory=policy_factory,
        )
        return _verify_context_artifact_reader(
            resolved,
            ownership,
            reader,
            expected_repository=expected_repository,
            expected_commit=expected_commit,
            max_files=max_files,
            max_bytes=max_bytes,
            capture_final=lambda: publication.capture_ownership(
                entry_policy=policy_factory(),
            ),
        )

    try:
        return reopen_authenticated_directory(resolved, ownership, verify)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "context artifact directory could not be reopened safely; it may "
            f"contain a symbolic link or unstable content: {resolved}"
        ) from exc


def bind_context_artifact(
    root: str | Path,
    repo_path: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
) -> ContextArtifactBinding:
    """Bind an artifact to retained, v2-authenticated repository content."""

    artifact = verify_context_artifact(
        root,
        expected_repository=expected_repository,
        expected_commit=expected_commit,
    )
    cleanup_owner = SourceBindingCleanupOwner()

    def bind(_publication: PublicationDirectoryReader) -> ContextArtifactBinding:
        if not is_secure_source_fingerprint_v2(artifact.source_fingerprint):
            raise ValueError(
                "legacy context artifact source fingerprints are query-only; "
                "rebuild with source fingerprint v2 before binding a live checkout"
            )
        repo = lexical_repository_path(repo_path)
        source = capture_repository_source(
            repo,
            exclude_roots=(artifact.root,),
            _source_owner=cleanup_owner.retain,
        )
        # This is diagnostic provenance only. A mutable Git HEAD cannot be
        # attested by any finite sequence of path reads, so it never gates the
        # retained content authority or native trust.
        actual_commit = checkout_commit(repo)
        source.verify_snapshot()
        if source.fingerprint != artifact.source_fingerprint:
            raise ValueError(
                "repository source files do not match the context artifact fingerprint"
            )
        if source.file_count != artifact.manifest.file_count:
            raise ValueError(
                "repository file count does not match the context artifact manifest"
            )
        source_records = {record.path for record in source.file_records}
        missing_source_paths = sorted(set(artifact.source_paths) - source_records)
        if missing_source_paths:
            raise ValueError(
                "context artifact source paths are absent from the authenticated "
                "repository: " + ", ".join(missing_source_paths)
            )

        manifest_data = artifact.manifest.to_dict()
        manifest_data["repo"]["path"] = str(repo)
        for view, entry in manifest_data["indexes"].items():
            entry["path"] = str(
                _artifact_path(
                    artifact.root,
                    entry["path"],
                    label=f"context artifact {view} view path",
                )
            )
        manifest = RepoManifest.from_dict(manifest_data)
        return ContextArtifactBinding(
            artifact=artifact,
            repo_path=repo,
            manifest=manifest,
            checkout_commit_observed=actual_commit or None,
            _source_binding=source,
        )

    try:
        return reopen_authenticated_directory(
            artifact.root,
            artifact.ownership,  # type: ignore[arg-type]
            bind,
        )
    except BaseException as exc:  # noqa: B036 - cleanup before propagation
        try:
            cleanup_owner.close()
        except BaseException:  # noqa: B036 - preserve primary, retain owner
            _attach_source_cleanup_owner(exc, cleanup_owner)
        if isinstance(exc, ValueError):
            raise
        if not isinstance(exc, (OSError, RuntimeError)):
            raise
        translated = ValueError(
            "context artifact changed between verification and source binding"
        )
        _attach_source_cleanup_owner(translated, cleanup_owner)
        raise translated from exc


def query_context_artifact(
    root: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
) -> ContextArtifactBinding:
    """Create an authority-bound, source-disabled portable query binding."""

    artifact = verify_context_artifact(
        root,
        expected_repository=expected_repository,
        expected_commit=expected_commit,
    )
    return ContextArtifactBinding(
        artifact=artifact,
        repo_path=None,
        manifest=artifact.manifest,
        checkout_commit_observed=None,
        _source_binding=None,
    )


__all__ = [
    "ContextArtifactBinding",
    "SourceBindingCleanupOwner",
    "VerifiedContextArtifact",
    "bind_context_artifact",
    "query_context_artifact",
    "verify_context_artifact",
    "verify_context_artifact_reader",
]
