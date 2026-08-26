# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strict, authority-bound publication for portable context generations."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .._atomic_directory import (
    PublicationDirectoryReader,
    TreeFileRecord,
    directory_ownership_digest,
    directory_ownership_file_records,
    directory_ownership_inventory,
    lexical_directory_path,
)
from .._captured_directory import (
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
    require_owned_workspace_publication_support,
)
from .._version import package_version
from .._workspace_provider import (
    StrictWorkspaceProvider,
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_strict_workspace,
)
from ..compiler.manifest import MANIFEST_FILENAME, MANIFEST_VERSION, RepoManifest
from ..compiler.snapshot_store import normalize_repo
from ..provider_routes import resolve_embedding_artifact_route
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
    is_secure_source_fingerprint_v2,
)
from .context import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    PORTABLE_CONTEXT_VIEWS,
    ContextArtifactResult,
)
from .portable_views import (
    _validate_content_bound_portable_query_view_reader_with_identity,
)
from .runtime import verify_context_artifact_reader
from .security import _assert_publishable_tree_reader_interruptibly
from .strict_bm25 import (
    _assert_authenticated_publishable_json_value,
    _environment_snapshot,
    _json_object_snapshot,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$")
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS_BYTES = 256 * 1024 * 1024
_STRICT_CONTEXT_PLAN_DOMAIN = "codenib-portable-context-strict-workspace-v1"
_MISSING = object()


def _interruptible_chunks(
    chunks: Iterable[bytes],
    check_cancelled: Callable[[], None] | None,
) -> Iterator[bytes]:
    iterator = iter(chunks)
    while True:
        if check_cancelled is not None:
            check_cancelled()
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        yield chunk


def _canonical_json_bytes(value: Any, *, label: str) -> bytes:
    """Return one bounded deterministic JSON representation."""

    try:
        payload = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical JSON data") from exc
    if len(payload) > _MAX_METADATA_BYTES:
        raise ValueError(f"{label} exceeds its {_MAX_METADATA_BYTES}-byte limit")
    return payload


def _record_payload(record: TreeFileRecord) -> dict[str, Any]:
    return {
        "path": record.path,
        "mode": record.mode,
        "size": record.size,
        "sha256": record.sha256,
    }


def _output_record(relative: str, payload: bytes) -> TreeFileRecord:
    return TreeFileRecord(
        path=relative,
        mode=0o600,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _prefixed_record(prefix: PurePosixPath, record: TreeFileRecord) -> TreeFileRecord:
    return TreeFileRecord(
        path=(prefix / PurePosixPath(record.path)).as_posix(),
        mode=record.mode,
        size=record.size,
        sha256=record.sha256,
    )


def _portable_capabilities(
    capabilities: Mapping[str, bool],
    views: Iterable[str],
) -> dict[str, bool]:
    selected = set(views)
    result = dict(capabilities)
    if any(not isinstance(value, bool) for value in result.values()):
        raise ValueError("strict context capabilities must be boolean")
    result.update(
        {
            "sparse_search": "bm25" in selected,
            "dense_search": "vector" in selected,
            "hybrid_search": {"bm25", "vector"} <= selected,
            "symbol_navigation": "symbol_graph" in selected,
        }
    )
    return result


def _merge_adjustments(
    entry: dict[str, Any],
    adjustments: Mapping[str, Any],
    *,
    view: str,
) -> None:
    for section_name in ("config", "metadata"):
        section = entry.get(section_name)
        if not isinstance(section, dict):
            raise ValueError(f"strict context {view} {section_name} must be an object")
        for key, value in adjustments.items():
            previous = section.get(key, _MISSING)
            if previous is not _MISSING and previous != value:
                raise ValueError(
                    f"strict context {view} {section_name} conflicts with "
                    f"its retained generation: {key}"
                )
            section[key] = value


def _view_adjustments(
    view: str,
    records: tuple[TreeFileRecord, ...],
    view_config: Mapping[str, Any],
) -> dict[str, Any]:
    by_path = {record.path: record for record in records}
    if len(by_path) != len(records):
        raise RuntimeError(f"strict context {view} source repeats a file")
    if view == "bm25":
        required = {"documents.json", "bm25_metadata.json"}
        if set(by_path) != required:
            raise ValueError("strict context BM25 generation is incomplete")
        return {
            "artifact_file_fingerprints": {
                relative: {
                    "size": by_path[relative].size,
                    "sha256": by_path[relative].sha256,
                }
                for relative in sorted(required)
            }
        }

    route = resolve_embedding_artifact_route(view_config, environ={})
    config_name = f"config_{route.model.replace('/', '__')}.json"
    config_record = by_path.get(config_name)
    if config_record is None:
        raise ValueError("strict context vector generation has no root config")
    return {
        "artifact_scope": "query-serving",
        "portable_document_format": "codenib.vector-documents.v1",
        "persistence_config_fingerprint": {
            "file": config_name,
            "size": config_record.size,
            "sha256": config_record.sha256,
        },
    }


@dataclass(frozen=True, slots=True)
class PlannedContextView:
    """One retained portable view bound into an exact context plan."""

    name: str
    source_plan_digest: str
    source_ownership: object
    source_digest: str
    source_inventory: tuple[tuple[str, str], ...]
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]
    config_payload: bytes
    entry_digest: str

    def __post_init__(self) -> None:
        inventory = tuple(self.source_inventory)
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        if self.name not in PORTABLE_CONTEXT_VIEWS:
            raise ValueError("strict context plan has an unsupported view")
        for label, value in (
            ("source plan", self.source_plan_digest),
            ("source tree", self.source_digest),
            ("entry", self.entry_digest),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"strict context {label} digest is invalid")
        if not isinstance(self.config_payload, bytes):
            raise TypeError("strict context view config payload must be bytes")
        if any(type(record) is not TreeFileRecord for record in source_records):
            raise TypeError("strict context source records have an invalid type")
        if any(type(record) is not TreeFileRecord for record in output_records):
            raise TypeError("strict context output records have an invalid type")
        if source_records != tuple(sorted(source_records, key=lambda item: item.path)):
            raise ValueError("strict context source records must be canonical")
        if output_records != tuple(sorted(output_records, key=lambda item: item.path)):
            raise ValueError("strict context output records must be canonical")
        prefix = f"views/{self.name}/"
        expected = tuple(
            _prefixed_record(PurePosixPath("views") / self.name, record)
            for record in source_records
        )
        if output_records != expected or any(
            not record.path.startswith(prefix) for record in output_records
        ):
            raise ValueError("strict context view output records are not exact")
        object.__setattr__(self, "source_inventory", inventory)
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)


@dataclass(frozen=True, slots=True)
class PlannedContextArtifact:
    """Immutable exact output plan for one portable context generation."""

    plan: WorkspacePlan
    manifest_digest: str
    repository: str
    commit: str
    repository_fingerprint: str
    repository_file_count: int
    views: tuple[str, ...]
    view_plans: tuple[PlannedContextView, ...]
    output_records: tuple[TreeFileRecord, ...]
    manifest_payload: bytes
    metadata_payload: bytes
    streaming_json_paths: tuple[str, ...]
    file_count: int
    byte_count: int

    def __post_init__(self) -> None:
        views = tuple(self.views)
        view_plans = tuple(self.view_plans)
        output_records = tuple(self.output_records)
        streaming_paths = tuple(self.streaming_json_paths)
        if type(self.plan) is not WorkspacePlan:
            raise TypeError("strict context plan must contain an exact WorkspacePlan")
        if views != tuple(sorted(views)) or not views or len(set(views)) != len(views):
            raise ValueError("strict context views must be non-empty and canonical")
        if tuple(view.name for view in view_plans) != views:
            raise ValueError("strict context view plans differ from selected views")
        if any(type(view) is not PlannedContextView for view in view_plans):
            raise TypeError("strict context view plans have an invalid type")
        if output_records != tuple(sorted(output_records, key=lambda item: item.path)):
            raise ValueError("strict context output records must be canonical")
        if any(type(record) is not TreeFileRecord for record in output_records):
            raise TypeError("strict context output records have an invalid type")
        if hashlib.sha256(self.manifest_payload).hexdigest() != self.manifest_digest:
            raise ValueError("strict context manifest digest differs from its bytes")
        if not _COMMIT_RE.fullmatch(self.commit):
            raise ValueError("strict context commit must be a full lowercase Git SHA")
        if self.repository != normalize_repo(
            self.repository
        ) or not _REPOSITORY_RE.fullmatch(self.repository):
            raise ValueError("strict context repository is not canonical")
        if (
            isinstance(self.repository_file_count, bool)
            or not isinstance(self.repository_file_count, int)
            or self.repository_file_count < 0
        ):
            raise ValueError("strict context repository file count is invalid")
        if (
            isinstance(self.file_count, bool)
            or not isinstance(self.file_count, int)
            or self.file_count <= 0
            or isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise ValueError("strict context output counts are invalid")
        if not isinstance(self.manifest_payload, bytes) or not isinstance(
            self.metadata_payload, bytes
        ):
            raise TypeError("strict context JSON payloads must be bytes")
        if streaming_paths != tuple(sorted(set(streaming_paths))):
            raise ValueError("strict context streaming paths must be canonical")
        output_by_path = {record.path: record for record in output_records}
        if len(output_by_path) != len(output_records):
            raise ValueError("strict context output records repeat a path")
        try:
            planned_files = tuple(
                TreeFileRecord(
                    path=item.path.as_posix(),
                    mode=item.mode,
                    size=item.max_bytes,
                    sha256=output_by_path[item.path.as_posix()].sha256,
                )
                for item in self.plan.files
            )
        except KeyError as exc:
            raise ValueError(
                "strict context workspace names an unknown output file"
            ) from exc
        if planned_files != output_records:
            raise ValueError("strict context workspace files differ from exact output")
        object.__setattr__(self, "views", views)
        object.__setattr__(self, "view_plans", view_plans)
        object.__setattr__(self, "output_records", output_records)
        object.__setattr__(self, "streaming_json_paths", streaming_paths)


@dataclass(frozen=True, slots=True)
class _FrozenContextInputs:
    manifest: dict[str, Any]
    repository: str
    repository_source: RepositorySourceBinding
    repository_identity: RepositorySourceIdentitySnapshot
    view_generations: tuple[tuple[str, PublishedWorkspaceReceiptOwner], ...]
    environment: dict[str, str]


def _snapshot_manifest(manifest: RepoManifest) -> dict[str, Any]:
    if type(manifest) is not RepoManifest:
        raise TypeError("strict context manifest must be a RepoManifest")
    snapshot = _json_object_snapshot(
        manifest.to_dict(),
        label="strict context repository manifest",
    )
    try:
        reconstructed = RepoManifest.from_dict(snapshot)
        canonical = reconstructed.to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("strict context repository manifest is invalid") from exc
    if canonical != snapshot:
        raise ValueError("strict context repository manifest is not canonical")
    return snapshot


def _snapshot_view_generations(
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
) -> tuple[tuple[str, PublishedWorkspaceReceiptOwner], ...]:
    if not isinstance(view_generations, Mapping):
        raise TypeError("strict context view generations must be a mapping")
    generations = dict(view_generations)
    if not generations:
        raise ValueError("strict context requires at least one portable view")
    if any(not isinstance(name, str) for name in generations):
        raise TypeError("strict context view names must be text")
    unsupported = sorted(set(generations) - PORTABLE_CONTEXT_VIEWS)
    if unsupported:
        raise ValueError(
            "strict context does not support views: " + ", ".join(unsupported)
        )
    ordered = tuple(sorted(generations.items()))
    owners = tuple(owner for _name, owner in ordered)
    if any(type(owner) is not PublishedWorkspaceReceiptOwner for owner in owners):
        raise TypeError("strict context views must be workspace receipt owners")
    if len({id(owner) for owner in owners}) != len(owners):
        raise ValueError("strict context views must use distinct receipt owners")
    if any(not owner.active for owner in owners):
        raise RuntimeError("strict context view generation must be active")
    return ordered


def _freeze_inputs(
    manifest: RepoManifest,
    *,
    repository: str,
    repository_source: RepositorySourceBinding,
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
    environ: Mapping[str, str] | None,
    check_cancelled: Callable[[], None] | None = None,
) -> _FrozenContextInputs:
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict context repository source has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict context repository source is not usable")
    repository_identity = (
        repository_source.authenticated_identity_snapshot()
        if check_cancelled is None
        else repository_source.authenticated_identity_snapshot(
            check_cancelled=check_cancelled,
        )
    )
    if type(repository_identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("strict context repository identity has an invalid type")
    if not isinstance(repository, str):
        raise TypeError("strict context repository must be text")
    normalized_repository = normalize_repo(repository)
    if normalized_repository != repository or not _REPOSITORY_RE.fullmatch(
        normalized_repository
    ):
        raise ValueError("strict context repository must be a canonical slug")
    return _FrozenContextInputs(
        manifest=_snapshot_manifest(manifest),
        repository=normalized_repository,
        repository_source=repository_source,
        repository_identity=repository_identity,
        view_generations=_snapshot_view_generations(view_generations),
        environment=_environment_snapshot(environ),
    )


def _validate_manifest_identity(inputs: _FrozenContextInputs) -> RepoManifest:
    manifest = RepoManifest.from_dict(inputs.manifest)
    source = inputs.repository_identity
    if manifest.version != MANIFEST_VERSION:
        raise ValueError(
            "strict context repository manifest version is incompatible: "
            f"{manifest.version!r}"
        )
    if not _COMMIT_RE.fullmatch(manifest.commit):
        raise ValueError("strict context requires a full lowercase Git commit")
    if not is_secure_source_fingerprint_v2(manifest.source_fingerprint):
        raise ValueError("strict context requires source fingerprint v2")
    if manifest.source_fingerprint != source.fingerprint:
        raise ValueError("strict context manifest belongs to another source generation")
    if manifest.file_count != source.file_count:
        raise ValueError("strict context manifest file count differs from its source")
    if manifest.source_selection != source.source_selection:
        raise ValueError("strict context manifest selection differs from its source")
    if not all(isinstance(language, str) for language in manifest.languages):
        raise ValueError("strict context manifest languages must be text")
    selected = tuple(name for name, _owner in inputs.view_generations)
    for view in selected:
        entry = manifest.indexes.get(view)
        if (
            entry is None
            or entry.index_type != view
            or not manifest.index_is_current(view)
        ):
            raise ValueError(f"strict context requires a current {view} view")
    return manifest


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _require_disjoint_paths(inputs: _FrozenContextInputs) -> tuple[Path, ...]:
    repository = lexical_directory_path(inputs.repository_identity.root)
    sources: list[Path] = []
    physical: list[Path] = []
    for view, owner in inputs.view_generations:
        path = lexical_directory_path(owner.receipt.path)
        if _overlaps(path, repository):
            raise ValueError(
                f"strict context {view} generation overlaps the repository source"
            )
        try:
            physical_path = path.resolve(strict=False)
            physical_repository = repository.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                "strict context view location cannot be authenticated"
            ) from exc
        if _overlaps(physical_path, physical_repository):
            raise ValueError(
                f"strict context {view} generation overlaps the repository source"
            )
        if any(_overlaps(path, previous) for previous in sources) or any(
            _overlaps(physical_path, previous) for previous in physical
        ):
            raise ValueError("strict context view generations overlap")
        sources.append(path)
        physical.append(physical_path)
    return tuple(sources)


def _require_destination_disjoint(
    destination: Path,
    inputs: _FrozenContextInputs,
    source_paths: tuple[Path, ...],
) -> Path:
    candidate = lexical_directory_path(destination)
    repository = lexical_directory_path(inputs.repository_identity.root)
    if _overlaps(candidate, repository) or any(
        _overlaps(candidate, source) for source in source_paths
    ):
        raise ValueError("strict context destination overlaps an input authority")
    try:
        physical_candidate = candidate.resolve(strict=False)
        forbidden = (repository.resolve(strict=False),) + tuple(
            source.resolve(strict=False) for source in source_paths
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError("strict context destination cannot be authenticated") from exc
    if any(_overlaps(physical_candidate, path) for path in forbidden):
        raise ValueError("strict context destination overlaps an input authority")
    return candidate


def _require_missing_destination(destination: Path) -> None:
    """Perform an early denial-only missing check before consuming sources.

    The provider remains authoritative for the race-free missing expectation;
    this check only prevents known-invalid requests from spending retained
    receipt or provider authority.
    """

    try:
        destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(
            "strict context destination cannot be inspected safely"
        ) from exc
    raise ValueError("strict context destination must be missing")


def _validate_source_view(
    *,
    view: str,
    owner: PublishedWorkspaceReceiptOwner,
    expected_receipt: PublishedWorkspaceReceipt,
    expected_ownership: object,
    config: Mapping[str, Any],
    inputs: _FrozenContextInputs,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    def validate(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> None:
        if (
            receipt is not expected_receipt
            or receipt.ownership != expected_ownership
            or receipt.plan_digest != expected_receipt.plan_digest
        ):
            raise RuntimeError(f"strict context {view} receipt changed")
        before = publication.capture_ownership(
            check_cancelled=check_cancelled,
        )
        if before != expected_ownership:
            raise RuntimeError(f"strict context {view} generation changed")
        if check_cancelled is not None:
            check_cancelled()
        _validate_content_bound_portable_query_view_reader_with_identity(
            publication,
            repository_source=inputs.repository_source,
            repository_identity=inputs.repository_identity,
            view_type=view,
            view_config=config,
            forbidden_paths=(),
            environ=inputs.environment,
            check_cancelled=check_cancelled,
        )
        if (
            publication.capture_ownership(
                check_cancelled=check_cancelled,
            )
            != before
        ):
            raise RuntimeError(f"strict context {view} generation changed")

    owner.consume(validate, check_cancelled=check_cancelled)
    if check_cancelled is not None:
        check_cancelled()


def _planned_view(
    view: str,
    owner: PublishedWorkspaceReceiptOwner,
    *,
    manifest: RepoManifest,
    inputs: _FrozenContextInputs,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[PlannedContextView, dict[str, Any]]:
    receipt = owner.receipt
    if not receipt.durable:
        raise RuntimeError(f"strict context {view} generation is not durable")
    if check_cancelled is not None:
        check_cancelled()
    ownership = receipt.ownership
    inventory = tuple(directory_ownership_inventory(ownership))  # type: ignore[arg-type]
    records = tuple(directory_ownership_file_records(ownership))  # type: ignore[arg-type]
    if records != tuple(sorted(records, key=lambda item: item.path)):
        raise RuntimeError(f"strict context {view} records are not canonical")
    entry = manifest.indexes[view].to_dict(manifest_version=manifest.version)
    config = entry.get("config")
    if not isinstance(config, dict):
        raise ValueError(f"strict context {view} config must be an object")
    adjustments = _view_adjustments(view, records, config)
    _merge_adjustments(entry, adjustments, view=view)
    entry["path"] = f"views/{view}"
    config = entry["config"]
    _assert_authenticated_publishable_json_value(
        entry,
        forbidden_paths=(inputs.repository_identity.root, receipt.path),
        environ=inputs.environment,
        label=f"strict context {view} manifest entry",
    )
    entry_payload = _canonical_json_bytes(
        entry,
        label=f"strict context {view} manifest entry",
    )
    config_payload = _canonical_json_bytes(
        config,
        label=f"strict context {view} config",
    )
    _validate_source_view(
        view=view,
        owner=owner,
        expected_receipt=receipt,
        expected_ownership=ownership,
        config=config,
        inputs=inputs,
        check_cancelled=check_cancelled,
    )
    prefix = PurePosixPath("views") / view
    outputs = tuple(_prefixed_record(prefix, record) for record in records)
    return (
        PlannedContextView(
            name=view,
            source_plan_digest=receipt.plan_digest,
            source_ownership=ownership,
            source_digest=directory_ownership_digest(ownership),  # type: ignore[arg-type]
            source_inventory=inventory,
            source_records=records,
            output_records=outputs,
            config_payload=config_payload,
            entry_digest=hashlib.sha256(entry_payload).hexdigest(),
        ),
        entry,
    )


def _workspace_directories(
    view_plans: tuple[PlannedContextView, ...],
) -> tuple[WorkspaceDirectory, ...]:
    paths = {"views"}
    for view in view_plans:
        prefix = PurePosixPath("views") / view.name
        paths.add(prefix.as_posix())
        for relative, kind in view.source_inventory:
            if kind == "directory":
                paths.add((prefix / PurePosixPath(relative)).as_posix())
    return tuple(WorkspaceDirectory(PurePosixPath(path)) for path in sorted(paths))


def _workspace_subject(
    *,
    repository: str,
    manifest: RepoManifest,
    manifest_digest: str,
    metadata_digest: str,
    view_plans: tuple[PlannedContextView, ...],
    output_records: tuple[TreeFileRecord, ...],
) -> str:
    identity = {
        "domain": _STRICT_CONTEXT_PLAN_DOMAIN,
        "schema": CONTEXT_ARTIFACT_SCHEMA,
        "repository": repository,
        "commit": manifest.commit,
        "source_fingerprint": manifest.source_fingerprint,
        "source_file_count": manifest.file_count,
        "manifest_sha256": manifest_digest,
        "metadata_sha256": metadata_digest,
        "views": [
            {
                "name": view.name,
                "source_plan_digest": view.source_plan_digest,
                "source_tree_digest": view.source_digest,
                "entry_digest": view.entry_digest,
                "source_inventory": [list(item) for item in view.source_inventory],
                "source_records": [
                    _record_payload(record) for record in view.source_records
                ],
            }
            for view in view_plans
        ],
        "output_records": [_record_payload(record) for record in output_records],
    }
    return hashlib.sha256(
        _canonical_json_bytes(identity, label="strict context plan identity")
    ).hexdigest()


def _build_plan(
    inputs: _FrozenContextInputs,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> PlannedContextArtifact:
    manifest = _validate_manifest_identity(inputs)
    _require_disjoint_paths(inputs)
    if check_cancelled is not None:
        check_cancelled()
    selected = tuple(name for name, _owner in inputs.view_generations)
    planned_entries: list[tuple[PlannedContextView, dict[str, Any]]] = []
    source_session = (
        inputs.repository_source.read_session()
        if check_cancelled is None
        else inputs.repository_source.read_session(
            check_cancelled=check_cancelled,
        )
    )
    with source_session:
        for view, owner in inputs.view_generations:
            if check_cancelled is None:
                planned_entry = _planned_view(
                    view,
                    owner,
                    manifest=manifest,
                    inputs=inputs,
                )
            else:
                planned_entry = _planned_view(
                    view,
                    owner,
                    manifest=manifest,
                    inputs=inputs,
                    check_cancelled=check_cancelled,
                )
            planned_entries.append(planned_entry)
    view_plans = tuple(item[0] for item in planned_entries)

    portable = json.loads(
        _canonical_json_bytes(
            inputs.manifest,
            label="strict context repository manifest snapshot",
        )
    )
    portable["repo"]["path"] = "source"
    portable["indexes"] = {
        planned_view.name: entry for planned_view, entry in planned_entries
    }
    portable["capabilities"] = _portable_capabilities(
        manifest.capabilities,
        selected,
    )
    manifest_payload = _canonical_json_bytes(
        portable,
        label="strict context portable repository manifest",
    )
    manifest_record = _output_record(MANIFEST_FILENAME, manifest_payload)

    inventoried_records = tuple(
        sorted(
            (manifest_record,)
            + tuple(record for view in view_plans for record in view.output_records),
            key=lambda item: item.path,
        )
    )
    metadata = {
        "schema": CONTEXT_ARTIFACT_SCHEMA,
        "repository": {
            "slug": inputs.repository,
            "commit": manifest.commit,
            "source_fingerprint": manifest.source_fingerprint,
            "languages": list(manifest.languages),
        },
        "builder": {
            "codenib_version": package_version(),
            "manifest_version": manifest.version,
            "compiled_at": manifest.compiled_at,
        },
        "manifest": {
            "path": MANIFEST_FILENAME,
            "repository_path": "source",
            "paths": "artifact-relative-posix",
        },
        "source_locations": {
            "path": "repository-relative-posix",
            "line_base": 1,
            "end_line": "inclusive",
            "commit": manifest.commit,
        },
        "views": list(selected),
        "capabilities": portable["capabilities"],
        "files": [
            {
                "path": record.path,
                "bytes": record.size,
                "sha256": record.sha256,
            }
            for record in inventoried_records
        ],
    }
    _assert_authenticated_publishable_json_value(
        metadata,
        forbidden_paths=(inputs.repository_identity.root,),
        environ=inputs.environment,
        label="strict context metadata",
    )
    metadata_payload = _canonical_json_bytes(
        metadata,
        label="strict context metadata",
    )
    metadata_record = _output_record(CONTEXT_ARTIFACT_MANIFEST, metadata_payload)
    output_records = tuple(
        sorted((*inventoried_records, metadata_record), key=lambda item: item.path)
    )
    manifest_digest = manifest_record.sha256
    subject_digest = _workspace_subject(
        repository=inputs.repository,
        manifest=manifest,
        manifest_digest=manifest_digest,
        metadata_digest=metadata_record.sha256,
        view_plans=view_plans,
        output_records=output_records,
    )
    workspace = WorkspacePlan(
        subject_digest=subject_digest,
        directories=_workspace_directories(view_plans),
        files=tuple(
            WorkspaceFile(
                PurePosixPath(record.path),
                mode=record.mode,
                max_bytes=record.size,
            )
            for record in output_records
        ),
    )
    streaming_json_paths = tuple(
        sorted(
            record.path
            for record in output_records
            if PurePosixPath(record.path).name.startswith("documents")
            and PurePosixPath(record.path).suffix == ".json"
        )
    )
    return PlannedContextArtifact(
        plan=workspace,
        manifest_digest=manifest_digest,
        repository=inputs.repository,
        commit=manifest.commit,
        repository_fingerprint=manifest.source_fingerprint,
        repository_file_count=manifest.file_count,
        views=selected,
        view_plans=view_plans,
        output_records=output_records,
        manifest_payload=manifest_payload,
        metadata_payload=metadata_payload,
        streaming_json_paths=streaming_json_paths,
        file_count=len(inventoried_records),
        byte_count=sum(record.size for record in inventoried_records),
    )


def _plan_context_artifact_strict_interruptibly(
    manifest: RepoManifest,
    *,
    repository: str,
    repository_source: RepositorySourceBinding,
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> PlannedContextArtifact:
    """Plan one exact context generation from retained portable views."""

    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("strict context cancellation check must be callable")
    inputs = _freeze_inputs(
        manifest,
        repository=repository,
        repository_source=repository_source,
        view_generations=view_generations,
        environ=environ,
        check_cancelled=check_cancelled,
    )
    return _build_plan(inputs, check_cancelled=check_cancelled)


def plan_context_artifact_strict(
    manifest: RepoManifest,
    *,
    repository: str,
    repository_source: RepositorySourceBinding,
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
    environ: Mapping[str, str] | None = None,
) -> PlannedContextArtifact:
    """Plan one exact context generation from retained portable views."""

    return _plan_context_artifact_strict_interruptibly(
        manifest,
        repository=repository,
        repository_source=repository_source,
        view_generations=view_generations,
        environ=environ,
    )


def _preflight_output_authority(
    planned: PlannedContextArtifact,
    *,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
) -> None:
    if type(planned) is not PlannedContextArtifact:
        raise TypeError("strict context execution requires a PlannedContextArtifact")
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict context repository source has an invalid type")
    if type(output_receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict context output receipt owner has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict context repository source is not usable")
    if output_receipt_owner.state != "empty":
        raise RuntimeError("strict context output receipt owner must be empty")
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


def _require_expected_plan(
    planned: PlannedContextArtifact,
    inputs: _FrozenContextInputs,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    expected = _build_plan(inputs, check_cancelled=check_cancelled)
    if planned != expected:
        raise ValueError("strict context plan differs from its exact inputs")


def _candidate_inventory(
    planned: PlannedContextArtifact,
) -> tuple[tuple[str, str], ...]:
    directories = tuple(
        (directory.path.as_posix(), "directory")
        for directory in planned.plan.directories
    )
    files = tuple((record.path, "file") for record in planned.output_records)
    return tuple(sorted((*directories, *files)))


def _validate_candidate(
    candidate: PublicationDirectoryReader,
    *,
    planned: PlannedContextArtifact,
    inputs: _FrozenContextInputs,
    forbidden_paths: tuple[Path, ...],
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    before = candidate.capture_ownership(
        check_cancelled=check_cancelled,
    )
    records = tuple(directory_ownership_file_records(before))  # type: ignore[arg-type]
    inventory = tuple(directory_ownership_inventory(before))  # type: ignore[arg-type]
    if records != planned.output_records or inventory != _candidate_inventory(planned):
        raise RuntimeError("strict context candidate differs from its exact plan")
    _assert_publishable_tree_reader_interruptibly(
        candidate,
        forbidden_paths=(inputs.repository_identity.root, *forbidden_paths),
        environ=inputs.environment,
        label="strict context artifact",
        streaming_json_paths=planned.streaming_json_paths,
        check_cancelled=check_cancelled,
    )
    verified = verify_context_artifact_reader(
        candidate,
        expected_repository=planned.repository,
        expected_commit=planned.commit,
        check_cancelled=check_cancelled,
    )
    if (
        verified.views != planned.views
        or verified.source_fingerprint != planned.repository_fingerprint
        or verified.file_count != planned.file_count
        or verified.byte_count != planned.byte_count
    ):
        raise RuntimeError("strict context candidate identity differs from its plan")
    after = candidate.capture_ownership(
        check_cancelled=check_cancelled,
    )
    if after != before:
        raise RuntimeError("strict context candidate changed during validation")


def _replay_view(
    session: StrictWorkspaceSession,
    *,
    planned_view: PlannedContextView,
    owner: PublishedWorkspaceReceiptOwner,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    def replay(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> None:
        if (
            receipt.plan_digest != planned_view.source_plan_digest
            or receipt.ownership != planned_view.source_ownership
        ):
            raise ValueError(
                f"strict context {planned_view.name} source changed after planning"
            )
        before = publication.capture_ownership(
            check_cancelled=check_cancelled,
        )
        if before != planned_view.source_ownership:
            raise RuntimeError(
                f"strict context {planned_view.name} source changed during replay"
            )
        if check_cancelled is not None:
            check_cancelled()
        written: list[TreeFileRecord] = []
        prefix = PurePosixPath("views") / planned_view.name
        for source_record, expected_output in zip(
            planned_view.source_records,
            planned_view.output_records,
            strict=True,
        ):
            if check_cancelled is not None:
                check_cancelled()
            with publication.open_authenticated_file(
                source_record.path,
                max_bytes=source_record.size,
            ) as source:
                written.append(
                    session.write_file(
                        prefix / PurePosixPath(source_record.path),
                        _interruptible_chunks(
                            source.iter_bytes(),
                            check_cancelled,
                        ),
                    )
                )
            if source.record != source_record:
                raise RuntimeError(
                    f"strict context {planned_view.name} source file changed"
                )
            if written[-1] != expected_output:
                raise RuntimeError(
                    f"strict context {planned_view.name} replay differs from its plan"
                )
            if check_cancelled is not None:
                check_cancelled()
        if tuple(written) != planned_view.output_records:
            raise RuntimeError(
                f"strict context {planned_view.name} replay differs from its plan"
            )
        if (
            publication.capture_ownership(
                check_cancelled=check_cancelled,
            )
            != before
        ):
            raise RuntimeError(
                f"strict context {planned_view.name} source changed during replay"
            )

    owner.consume(replay, check_cancelled=check_cancelled)
    if check_cancelled is not None:
        check_cancelled()


def _publish_frozen(
    destination: Path,
    *,
    planned: PlannedContextArtifact,
    inputs: _FrozenContextInputs,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    require_expected_plan: bool,
    check_cancelled: Callable[[], None] | None = None,
) -> ContextArtifactResult:
    if check_cancelled is not None:
        check_cancelled()
    if any(owner is output_receipt_owner for _view, owner in inputs.view_generations):
        raise ValueError("strict context input and output receipt owners must differ")
    source_paths = _require_disjoint_paths(inputs)
    lexical_destination = _require_destination_disjoint(
        destination,
        inputs,
        source_paths,
    )
    _require_missing_destination(lexical_destination)
    if require_expected_plan:
        _require_expected_plan(
            planned,
            inputs,
            check_cancelled=check_cancelled,
        )
    request = StrictWorkspaceRequest(
        purpose="portable-context-generation",
        destination=lexical_destination,
        plan=planned.plan,
        destination_binding=None,
    )

    owners = dict(inputs.view_generations)

    def operation(session: StrictWorkspaceSession) -> None:
        if session.request != request:
            raise RuntimeError("strict context provider changed its request")
        if check_cancelled is not None:
            check_cancelled()
        source_session = (
            inputs.repository_source.read_session()
            if check_cancelled is None
            else inputs.repository_source.read_session(
                check_cancelled=check_cancelled,
            )
        )
        with source_session:
            expected_by_path = {
                record.path: record for record in planned.output_records
            }
            written = [
                session.write_file(MANIFEST_FILENAME, (planned.manifest_payload,)),
                session.write_file(
                    CONTEXT_ARTIFACT_MANIFEST,
                    (planned.metadata_payload,),
                ),
            ]
            if any(record != expected_by_path.get(record.path) for record in written):
                raise RuntimeError(
                    "strict context metadata replay differs from its plan"
                )
            if check_cancelled is not None:
                check_cancelled()
            for planned_view in planned.view_plans:
                if check_cancelled is not None:
                    check_cancelled()
                _replay_view(
                    session,
                    planned_view=planned_view,
                    owner=owners[planned_view.name],
                    check_cancelled=check_cancelled,
                )
            expected_static = {
                MANIFEST_FILENAME,
                CONTEXT_ARTIFACT_MANIFEST,
            }
            if {record.path for record in written} != expected_static:
                raise RuntimeError(
                    "strict context metadata replay differs from its plan"
                )

        def validate_with_check(
            candidate: PublicationDirectoryReader,
            validation_check: Callable[[], None] | None,
        ) -> None:
            # Each staged/published callback owns its source postcondition.
            # In particular, the published-side check must finish inside the
            # directory transaction's rollback window, not after commit.
            source_session = (
                inputs.repository_source.read_session()
                if validation_check is None
                else inputs.repository_source.read_session(
                    check_cancelled=validation_check,
                )
            )
            with source_session:
                _validate_candidate(
                    candidate,
                    planned=planned,
                    inputs=inputs,
                    forbidden_paths=source_paths,
                    check_cancelled=validation_check,
                )

        def validate(candidate: PublicationDirectoryReader) -> None:
            validate_with_check(candidate, check_cancelled)

        def validate_published(candidate: PublicationDirectoryReader) -> None:
            validate_with_check(candidate, None)

        if check_cancelled is not None:
            check_cancelled()
        session.publish_validated(
            validate,
            validate_published_destination=validate_published,
        )

    run_strict_workspace(
        workspace_provider,
        request,
        receipt_owner=output_receipt_owner,
        operation=operation,
        check_cancelled=check_cancelled,
    )
    if not output_receipt_owner.active:
        raise RuntimeError("strict context publication returned without a receipt")
    receipt = output_receipt_owner.receipt
    if receipt.path != lexical_destination or receipt.plan != planned.plan:
        raise RuntimeError("strict context receipt differs from its plan")
    return ContextArtifactResult(
        output_dir=lexical_destination,
        metadata_path=lexical_destination / CONTEXT_ARTIFACT_MANIFEST,
        manifest_path=lexical_destination / MANIFEST_FILENAME,
        repository=planned.repository,
        commit=planned.commit,
        views=planned.views,
        file_count=planned.file_count,
        byte_count=planned.byte_count,
    )


def _publish_planned_context_artifact_strict_interruptibly(
    destination: Path,
    *,
    planned: PlannedContextArtifact,
    manifest: RepoManifest,
    repository: str,
    repository_source: RepositorySourceBinding,
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> ContextArtifactResult:
    """Replay and durably publish one exact strict context plan."""

    _preflight_output_authority(
        planned,
        repository_source=repository_source,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("strict context cancellation check must be callable")
    if check_cancelled is not None:
        check_cancelled()
    inputs = _freeze_inputs(
        manifest,
        repository=repository,
        repository_source=repository_source,
        view_generations=view_generations,
        environ=environ,
        check_cancelled=check_cancelled,
    )
    return _publish_frozen(
        destination,
        planned=planned,
        inputs=inputs,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        require_expected_plan=True,
        check_cancelled=check_cancelled,
    )


def publish_planned_context_artifact_strict(
    destination: Path,
    *,
    planned: PlannedContextArtifact,
    manifest: RepoManifest,
    repository: str,
    repository_source: RepositorySourceBinding,
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    environ: Mapping[str, str] | None = None,
) -> ContextArtifactResult:
    """Replay and durably publish one exact strict context plan."""

    return _publish_planned_context_artifact_strict_interruptibly(
        destination,
        planned=planned,
        manifest=manifest,
        repository=repository,
        repository_source=repository_source,
        view_generations=view_generations,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        environ=environ,
    )


def stage_context_artifact_strict(
    destination: Path,
    *,
    manifest: RepoManifest,
    repository: str,
    repository_source: RepositorySourceBinding,
    view_generations: Mapping[str, PublishedWorkspaceReceiptOwner],
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    environ: Mapping[str, str] | None = None,
) -> ContextArtifactResult:
    """Plan and publish one immutable context generation through strict authority."""

    # Reject unusable output authority before consuming any source generation.
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("strict context repository source has an invalid type")
    if type(output_receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict context output receipt owner has an invalid type")
    if not repository_source.usable:
        raise RuntimeError("strict context repository source is not usable")
    if output_receipt_owner.state != "empty":
        raise RuntimeError("strict context output receipt owner must be empty")
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
    inputs = _freeze_inputs(
        manifest,
        repository=repository,
        repository_source=repository_source,
        view_generations=view_generations,
        environ=environ,
    )
    if any(owner is output_receipt_owner for _view, owner in inputs.view_generations):
        raise ValueError("strict context input and output receipt owners must differ")
    # Destination authorization is independent of source semantics. Reject it
    # before consuming any retained view or asking the provider to provision.
    source_paths = _require_disjoint_paths(inputs)
    lexical_destination = _require_destination_disjoint(
        destination,
        inputs,
        source_paths,
    )
    _require_missing_destination(lexical_destination)
    planned = _build_plan(inputs)
    return _publish_frozen(
        destination,
        planned=planned,
        inputs=inputs,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        require_expected_plan=False,
    )


__all__ = [
    "PlannedContextArtifact",
    "PlannedContextView",
    "plan_context_artifact_strict",
    "publish_planned_context_artifact_strict",
    "stage_context_artifact_strict",
]
