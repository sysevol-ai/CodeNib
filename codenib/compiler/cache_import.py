# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Import one compiler-cache BM25 generation through retained authority.

The compiler cache is mutable local state, so it is never handed directly to
the retained importer.  This coordinator holds the compiler cache lock while
it authenticates the fixed manifest and BM25 tree, plans the exact portable
manifest, and publishes immutable BM25 and context generations.  Only the
immutable context receipt crosses the lock boundary into storage import.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._atomic_directory import (
    PublicationDirectoryReader,
    TreeFileRecord,
    lexical_directory_path,
)
from .._captured_directory import (
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
)
from .._workspace_provider import StrictWorkspaceProvider
from ..artifacts.context import ContextArtifactResult
from ..artifacts.portable_views import _read_bounded_json
from ..artifacts.strict_bm25 import (
    PlannedBm25View,
    _plan_recaptured_bm25_view,
    _publish_recaptured_bm25_view,
)
from ..artifacts.strict_context import (
    _canonical_json_bytes,
    _portable_capabilities,
    stage_context_artifact_strict,
)
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
    is_secure_source_fingerprint_v2,
)
from ..storage.models import (
    DEFAULT_NAMESPACE_NAME,
    RepositoryIdentity,
    StorageIntegrityError,
    StorageValidationError,
)
from ..storage.protocols import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    RetainedImportCatalog,
    RetainedImportObjectStore,
)
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
)
from .cache_lock import compiler_cache_lock
from .manifest import MANIFEST_FILENAME, MANIFEST_VERSION, IndexEntry, RepoManifest
from .manifest_import import (
    _CATALOG_METHODS,
    _OBJECT_STORE_METHODS,
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    DEFAULT_MAX_PROJECTION_BYTES,
    DEFAULT_REF_NAME,
    RepoManifestImportResult,
    _exact_text,
    _expected_generation,
    _expected_namespace_id,
    _positive_limit,
    _require_static_methods,
    _snapshot_environment,
    _snapshot_forbidden_paths,
    import_retained_repo_manifest,
)
from .manifest_storage import (
    DEFAULT_MAX_MANIFEST_BYTES,
    RepoManifestImportPlan,
    _manifest_limit,
    _preflight_json_bytes,
    _strict_json_loads,
    plan_repo_manifest_import_bytes,
)
from .snapshot_store import normalize_repo

_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class CompilerCacheBm25RecaptureResult:
    """Detached evidence for the mutable-to-immutable BM25 recapture."""

    source_view: Path
    output_view: Path
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]
    canonical_manifest_bytes: bytes

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheBm25RecaptureResult:
            raise TypeError("compiler cache recapture must use the exact result type")
        if type(self.source_view) is not type(Path()) or type(
            self.output_view
        ) is not type(Path()):
            raise TypeError("compiler cache recapture paths must be exact paths")
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        for records, label in (
            (source_records, "source"),
            (output_records, "output"),
        ):
            if any(type(record) is not TreeFileRecord for record in records):
                raise TypeError(f"compiler cache recapture {label} records are invalid")
            if records != tuple(sorted(records, key=lambda item: item.path)):
                raise ValueError(
                    f"compiler cache recapture {label} records are not canonical"
                )
            if {record.path for record in records} != {
                "bm25_metadata.json",
                "documents.json",
            }:
                raise ValueError(
                    f"compiler cache recapture {label} records are incomplete"
                )
        if type(self.canonical_manifest_bytes) is not bytes:
            raise TypeError("compiler cache portable manifest must be exact bytes")
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)

    @property
    def manifest_digest(self) -> str:
        return hashlib.sha256(self.canonical_manifest_bytes).hexdigest()

    @property
    def artifact_file_fingerprints(self) -> dict[str, dict[str, Any]]:
        return _fingerprints_for_records(self.output_records)


@dataclass(frozen=True, slots=True)
class CompilerCacheImportResult:
    """Pure-data result of one compiler-cache BM25 retained import."""

    recapture: CompilerCacheBm25RecaptureResult
    import_plan: RepoManifestImportPlan
    context_artifact: ContextArtifactResult
    import_result: RepoManifestImportResult

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheImportResult:
            raise TypeError("compiler cache import must use the exact result type")
        if type(self.recapture) is not CompilerCacheBm25RecaptureResult:
            raise TypeError("compiler cache import recapture result is invalid")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache import plan is invalid")
        if type(self.context_artifact) is not ContextArtifactResult:
            raise TypeError("compiler cache context artifact result is invalid")
        if type(self.import_result) is not RepoManifestImportResult:
            raise TypeError("compiler cache retained import result is invalid")
        if (
            self.import_plan.selection.selected_views != ("bm25",)
            or self.context_artifact.views != ("bm25",)
            or self.import_result.views != ("bm25",)
            or self.recapture.canonical_manifest_bytes
            != _pretty_manifest_bytes(self.import_plan.manifest)
        ):
            raise StorageIntegrityError(
                "compiler cache import result identities are inconsistent"
            )


@dataclass(frozen=True, slots=True)
class _ImportPreflight:
    repository_key: str
    namespace_name: str
    ref_name: str
    expected_generation: int
    max_manifest_bytes: int
    max_context_files: int
    max_context_bytes: int
    max_bundle_files: int
    max_bundle_bytes: int
    max_bundle_metadata_bytes: int
    max_projection_bytes: int
    forbidden_paths: tuple[Path, ...]
    environment: Mapping[str, str]


def _path_relation(path: Path, boundary: Path) -> str:
    if path == boundary:
        return "same"
    if boundary in path.parents:
        return "descendant"
    if path in boundary.parents:
        return "ancestor"
    return "disjoint"


def _resolved_path(path: Path, *, strict: bool, label: str) -> Path:
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} cannot be authenticated") from exc


def _require_missing(path: Path, *, label: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected safely") from exc
    raise FileExistsError(f"{label} must be missing")


def _preflight_authorities(
    *,
    repository_source: RepositorySourceBinding,
    bm25_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
) -> None:
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("compiler cache source must be an exact source binding")
    if (
        type(bm25_output_owner) is not PublishedWorkspaceReceiptOwner
        or type(context_output_owner) is not PublishedWorkspaceReceiptOwner
    ):
        raise TypeError("compiler cache outputs must be exact receipt owners")
    if bm25_output_owner is context_output_owner:
        raise ValueError("compiler cache outputs must use distinct receipt owners")
    if not repository_source.usable:
        raise RuntimeError("compiler cache repository source is not usable")
    if bm25_output_owner.state != "empty" or context_output_owner.state != "empty":
        raise RuntimeError("compiler cache output receipt owners must be empty")


def _preflight_workspace_provider(
    workspace_provider: StrictWorkspaceProvider,
) -> None:
    for member in ("require_support", "run_workspace"):
        try:
            candidate = inspect.getattr_static(workspace_provider, member)
        except AttributeError as exc:
            raise TypeError(
                "compiler cache workspace provider has an invalid contract"
            ) from exc
        if isinstance(candidate, (classmethod, staticmethod)):
            candidate = candidate.__func__
        if not callable(candidate):
            raise TypeError("compiler cache workspace provider has an invalid contract")
    workspace_provider.require_support()


def _preflight_import_inputs(
    *,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str,
    ref_name: str,
    expected_generation: int,
    max_manifest_bytes: int,
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    max_projection_bytes: int,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str] | None,
) -> _ImportPreflight:
    repository = _exact_text(repository_key, "repository key")
    try:
        normalized_repository = normalize_repo(repository)
    except ValueError as exc:
        raise StorageValidationError("repository key is not canonical") from exc
    if normalized_repository != repository:
        raise StorageValidationError("repository key is not canonical")
    namespace = _exact_text(namespace_name, "namespace name")
    ref = _exact_text(ref_name, "ref name")
    expected = _expected_generation(expected_generation)
    manifest_limit = _manifest_limit(max_manifest_bytes)
    context_files = _positive_limit(max_context_files, "context file limit")
    context_bytes = _positive_limit(max_context_bytes, "context byte limit")
    bundle_files = _positive_limit(max_bundle_files, "bundle file limit")
    bundle_bytes = _positive_limit(max_bundle_bytes, "bundle byte limit")
    bundle_metadata_bytes = _positive_limit(
        max_bundle_metadata_bytes,
        "bundle metadata byte limit",
    )
    projection_bytes = _positive_limit(max_projection_bytes, "projection byte limit")
    environment = _snapshot_environment(environ)
    forbidden = _snapshot_forbidden_paths(forbidden_paths)

    if not isinstance(catalog, RetainedImportCatalog):
        raise TypeError("compiler cache import catalog lacks required capabilities")
    if not isinstance(object_store, RetainedImportObjectStore):
        raise TypeError(
            "compiler cache import object store lacks required capabilities"
        )
    _require_static_methods(
        object_store,
        label="retained import object store",
        names=_OBJECT_STORE_METHODS,
    )
    _require_static_methods(
        catalog,
        label="retained import catalog",
        names=_CATALOG_METHODS,
    )
    namespace_id = _expected_namespace_id(namespace)
    RepositoryIdentity(namespace_id=namespace_id, repository_key=repository)
    return _ImportPreflight(
        repository_key=repository,
        namespace_name=namespace,
        ref_name=ref,
        expected_generation=expected,
        max_manifest_bytes=manifest_limit,
        max_context_files=context_files,
        max_context_bytes=context_bytes,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata_bytes,
        max_projection_bytes=projection_bytes,
        forbidden_paths=forbidden,
        environment=environment,
    )


def _preflight_catalog_contract(catalog: RetainedImportCatalog) -> None:
    contract = catalog.retained_import_contract()
    if type(contract) is not str or contract != RETAINED_IMPORT_CATALOG_CONTRACT:
        raise TypeError("compiler cache import catalog contract is incompatible")


def _cache_topology(
    cache: Path,
    identity: RepositorySourceIdentitySnapshot,
) -> None:
    repository = lexical_directory_path(identity.root)
    lexical_relation = _path_relation(cache, repository)
    physical_cache = _resolved_path(cache, strict=True, label="compiler cache")
    physical_repository = _resolved_path(
        repository,
        strict=True,
        label="compiler cache repository",
    )
    physical_relation = _path_relation(physical_cache, physical_repository)
    if lexical_relation != physical_relation:
        raise ValueError(
            "compiler cache has inconsistent lexical and physical repository topology"
        )
    if lexical_relation in {"same", "ancestor"}:
        raise ValueError("compiler cache must not contain the repository source")
    if lexical_relation != "descendant":
        return
    relative = cache.relative_to(repository).as_posix()
    prefix = relative + "/"
    if any(
        record.path == relative or record.path.startswith(prefix)
        for record in identity.file_records
    ):
        raise StorageIntegrityError(
            "compiler cache is present in the authenticated repository source records"
        )


def _destination_topology(
    bm25_destination: Path,
    context_destination: Path,
    *,
    cache: Path,
    repository: Path,
    source_view: Path,
) -> None:
    lexical_boundaries = (cache, repository, source_view)
    physical_boundaries = tuple(
        _resolved_path(path, strict=True, label="compiler cache input")
        for path in lexical_boundaries
    )
    physical_destinations = tuple(
        _resolved_path(path, strict=False, label="compiler cache destination")
        for path in (bm25_destination, context_destination)
    )
    for destination, physical_destination in zip(
        (bm25_destination, context_destination),
        physical_destinations,
        strict=True,
    ):
        if any(
            _path_relation(destination, boundary) != "disjoint"
            for boundary in lexical_boundaries
        ) or any(
            _path_relation(physical_destination, boundary) != "disjoint"
            for boundary in physical_boundaries
        ):
            raise ValueError("compiler cache destination overlaps an input authority")
        _require_missing(destination, label="compiler cache destination")
    if _path_relation(bm25_destination, context_destination) != "disjoint" or (
        _path_relation(physical_destinations[0], physical_destinations[1]) != "disjoint"
    ):
        raise ValueError("compiler cache output destinations overlap")


def _read_manifest(cache: Path, *, max_manifest_bytes: int) -> bytes:
    return bytes(
        _read_bounded_json(
            cache / MANIFEST_FILENAME,
            label="compiler cache repository manifest",
            max_bytes=max_manifest_bytes,
        )
    )


def _parse_exact_manifest(payload: bytes, *, max_manifest_bytes: int) -> RepoManifest:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ValueError("compiler cache repository manifest must not contain a BOM")
    _preflight_json_bytes(
        payload,
        label="compiler cache repository manifest",
        max_bytes=max_manifest_bytes,
    )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(
            "compiler cache repository manifest is not valid UTF-8"
        ) from exc
    data = _strict_json_loads(text, label="compiler cache repository manifest")
    try:
        manifest = RepoManifest.from_dict(data)
        exact = manifest.to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("compiler cache repository manifest is invalid") from exc
    if exact != data:
        raise ValueError("compiler cache repository manifest is not exact v1.1 data")
    return manifest


def _require_manifest_source(
    manifest: RepoManifest,
    identity: RepositorySourceIdentitySnapshot,
) -> None:
    if manifest.version != MANIFEST_VERSION:
        raise ValueError("compiler cache repository manifest version is incompatible")
    declared_path = Path(manifest.repo_path)
    if not manifest.repo_path or not declared_path.is_absolute():
        raise ValueError("compiler cache repository path must be absolute")
    declared_repository = lexical_directory_path(declared_path)
    if declared_repository != lexical_directory_path(identity.root):
        raise StorageIntegrityError(
            "compiler cache repository manifest belongs to another repository"
        )
    if (
        not is_secure_source_fingerprint_v2(manifest.source_fingerprint)
        or manifest.source_fingerprint != identity.fingerprint
        or manifest.file_count != identity.file_count
    ):
        raise StorageIntegrityError(
            "compiler cache repository manifest differs from the retained source"
        )


def _bm25_source(cache: Path, entry: IndexEntry) -> Path:
    declared = Path(entry.path).expanduser()
    if not declared.is_absolute():
        declared = cache / declared
    source = lexical_directory_path(declared)
    expected = lexical_directory_path(cache / "bm25")
    if source != expected:
        raise ValueError("compiler cache BM25 entry does not name its fixed view")
    return source


def _require_current_bm25(manifest: RepoManifest) -> IndexEntry:
    entry = manifest.indexes.get("bm25")
    if (
        type(entry) is not IndexEntry
        or entry.index_type != "bm25"
        or entry.status != "fresh"
        or entry.commit != manifest.commit
        or entry.source_fingerprint != manifest.source_fingerprint
        or not manifest.index_is_current("bm25")
    ):
        raise ValueError("compiler cache repository manifest has no exact current BM25")
    return entry


def _fingerprints_for_records(
    records: tuple[TreeFileRecord, ...],
) -> dict[str, dict[str, Any]]:
    return {
        record.path: {"size": record.size, "sha256": record.sha256}
        for record in records
    }


def _require_source_fingerprints(
    entry: IndexEntry,
    planned: PlannedBm25View,
) -> None:
    expected = _fingerprints_for_records(planned.source_records)
    if (
        entry.config.get("artifact_file_fingerprints") != expected
        or entry.metadata.get("artifact_file_fingerprints") != expected
    ):
        raise StorageIntegrityError(
            "compiler cache BM25 files differ from their manifest fingerprints"
        )


def _pretty_manifest_bytes(manifest: RepoManifest) -> bytes:
    return _canonical_json_bytes(
        manifest.to_dict(),
        label="compiler cache portable repository manifest",
    )


def _portable_manifest(
    manifest: RepoManifest,
    planned: PlannedBm25View,
) -> tuple[RepoManifest, bytes]:
    portable = copy.deepcopy(manifest.to_dict())
    portable["repo"]["path"] = "source"
    entry = copy.deepcopy(portable["indexes"]["bm25"])
    entry["path"] = "views/bm25"
    fingerprints = _fingerprints_for_records(planned.output_records)
    for section_name in ("config", "metadata"):
        section = entry.get(section_name)
        if not isinstance(section, dict):
            raise ValueError(
                f"compiler cache BM25 {section_name} must be an exact object"
            )
        section["artifact_file_fingerprints"] = copy.deepcopy(fingerprints)
    portable["indexes"] = {"bm25": entry}
    portable["capabilities"] = _portable_capabilities(
        portable["capabilities"],
        ("bm25",),
    )
    try:
        portable_manifest = RepoManifest.from_dict(portable)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("compiler cache portable manifest is invalid") from exc
    if portable_manifest.to_dict() != portable:
        raise ValueError("compiler cache portable manifest is not canonical")
    return portable_manifest, _pretty_manifest_bytes(portable_manifest)


def _read_context_manifest(
    owner: PublishedWorkspaceReceiptOwner,
    *,
    max_manifest_bytes: int,
) -> bytes:
    def read(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> bytes:
        del receipt
        before = publication.capture_ownership()
        with publication.open_authenticated_file(
            MANIFEST_FILENAME,
            max_bytes=max_manifest_bytes,
        ) as source:
            payload = b"".join(source.iter_bytes())
        if publication.capture_ownership() != before:
            raise StorageIntegrityError(
                "compiler cache context artifact changed while reading its manifest"
            )
        return payload

    return owner.consume(read)


def import_compiler_cache_bm25(
    cache_dir: str | Path,
    *,
    repository_source: RepositorySourceBinding,
    bm25_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    bm25_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int = 0,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheImportResult:
    """Recapture and import the exact current BM25 compiler-cache generation.

    Both receipt owners and the retained repository binding are borrowed.  The
    caller remains responsible for closing them, on both success and failure.
    Published directories are immutable evidence and are never recursively
    removed by this coordinator when a later stage fails.
    """

    _preflight_authorities(
        repository_source=repository_source,
        bm25_output_owner=bm25_output_owner,
        context_output_owner=context_output_owner,
    )
    cache = lexical_directory_path(Path(cache_dir))
    bm25_output = lexical_directory_path(bm25_destination)
    context_output = lexical_directory_path(context_destination)
    inputs = _preflight_import_inputs(
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        ref_name=ref_name,
        expected_generation=expected_generation,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        max_projection_bytes=max_projection_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    fixed_source_view = lexical_directory_path(cache / "bm25")
    policy_forbidden = _snapshot_forbidden_paths(
        (
            *inputs.forbidden_paths,
            cache,
            fixed_source_view,
            bm25_output,
            context_output,
        )
    )
    _preflight_workspace_provider(workspace_provider)
    _preflight_catalog_contract(catalog)

    with compiler_cache_lock(cache, create=False):
        source_manifest_bytes = _read_manifest(
            cache,
            max_manifest_bytes=inputs.max_manifest_bytes,
        )
        manifest = _parse_exact_manifest(
            source_manifest_bytes,
            max_manifest_bytes=inputs.max_manifest_bytes,
        )
        if _COMMIT_RE.fullmatch(manifest.commit) is None:
            raise ValueError(
                "compiler cache repository manifest commit must be a full "
                "lowercase Git SHA"
            )
        identity = repository_source.authenticated_identity_snapshot()
        if type(identity) is not RepositorySourceIdentitySnapshot:
            raise TypeError("compiler cache repository identity has an invalid type")
        _cache_topology(cache, identity)
        _require_manifest_source(manifest, identity)
        bm25_entry = _require_current_bm25(manifest)
        source_view = _bm25_source(cache, bm25_entry)
        _destination_topology(
            bm25_output,
            context_output,
            cache=cache,
            repository=lexical_directory_path(identity.root),
            source_view=source_view,
        )
        if source_view != fixed_source_view:  # pragma: no cover - helper invariant
            raise AssertionError("compiler cache fixed BM25 source changed")

        # This exact plan, portable manifest, and import plan all exist before
        # the first provider call can mutate either destination.
        planned_bm25 = _plan_recaptured_bm25_view(
            source_view,
            bm25_output,
            repository_source=repository_source,
            view_config=bm25_entry.config,
            forbidden_paths=policy_forbidden,
            environ=inputs.environment,
        )
        _require_source_fingerprints(bm25_entry, planned_bm25)
        portable_manifest, canonical_manifest_bytes = _portable_manifest(
            manifest,
            planned_bm25,
        )
        import_plan = plan_repo_manifest_import_bytes(
            canonical_manifest_bytes,
            views=("bm25",),
            max_manifest_bytes=inputs.max_manifest_bytes,
        )
        if (
            import_plan.selection.selected_views != ("bm25",)
            or import_plan.manifest.to_dict() != portable_manifest.to_dict()
        ):
            raise StorageIntegrityError(
                "compiler cache portable manifest changed during import planning"
            )

        adjustments = _publish_recaptured_bm25_view(
            source_view,
            bm25_output,
            planned=planned_bm25,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=bm25_output_owner,
            view_config=bm25_entry.config,
            forbidden_paths=policy_forbidden,
            environ=inputs.environment,
        )
        if adjustments != _fingerprints_adjustment(planned_bm25.output_records):
            raise StorageIntegrityError(
                "compiler cache BM25 publication differs from its exact plan"
            )

        context_artifact = stage_context_artifact_strict(
            context_output,
            manifest=portable_manifest,
            repository=inputs.repository_key,
            repository_source=repository_source,
            view_generations={"bm25": bm25_output_owner},
            workspace_provider=workspace_provider,
            output_receipt_owner=context_output_owner,
            environ=inputs.environment,
        )
        observed_manifest_bytes = _read_context_manifest(
            context_output_owner,
            max_manifest_bytes=inputs.max_manifest_bytes,
        )
        if observed_manifest_bytes != canonical_manifest_bytes:
            raise StorageIntegrityError(
                "compiler cache context manifest differs from its preplanned bytes"
            )
        if repository_source.authenticated_identity_snapshot() != identity:
            raise StorageIntegrityError(
                "compiler cache repository source changed during recapture"
            )
        if (
            _read_manifest(cache, max_manifest_bytes=inputs.max_manifest_bytes)
            != source_manifest_bytes
        ):
            raise StorageIntegrityError(
                "compiler cache repository manifest changed during recapture"
            )

        recapture = CompilerCacheBm25RecaptureResult(
            source_view=source_view,
            output_view=bm25_output,
            source_records=planned_bm25.source_records,
            output_records=planned_bm25.output_records,
            canonical_manifest_bytes=canonical_manifest_bytes,
        )

    # Catalog/CAS publication deliberately occurs after releasing the mutable
    # compiler cache lock.  The context receipt remains the exact authority
    # authenticated above, so the mutable cache is no longer consulted.
    import_result = import_retained_repo_manifest(
        import_plan,
        artifact_owner=context_output_owner,
        repository_source=repository_source,
        repository_key=inputs.repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=inputs.namespace_name,
        ref_name=inputs.ref_name,
        expected_generation=inputs.expected_generation,
        max_context_files=inputs.max_context_files,
        max_context_bytes=inputs.max_context_bytes,
        max_bundle_files=inputs.max_bundle_files,
        max_bundle_bytes=inputs.max_bundle_bytes,
        max_bundle_metadata_bytes=inputs.max_bundle_metadata_bytes,
        max_projection_bytes=inputs.max_projection_bytes,
        forbidden_paths=policy_forbidden,
        environ=inputs.environment,
    )
    return CompilerCacheImportResult(
        recapture=recapture,
        import_plan=import_plan,
        context_artifact=context_artifact,
        import_result=import_result,
    )


def _fingerprints_adjustment(
    records: tuple[TreeFileRecord, ...],
) -> dict[str, Any]:
    return {"artifact_file_fingerprints": _fingerprints_for_records(records)}


__all__ = [
    "CompilerCacheBm25RecaptureResult",
    "CompilerCacheImportResult",
    "import_compiler_cache_bm25",
]
