# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Import compiler-cache query views through retained authority.

The compiler cache is mutable local state, so it is never handed directly to
the retained importer.  This coordinator holds one existing compiler cache
lock while it authenticates the fixed manifest and selected query-view trees,
plans the exact portable manifest, and publishes immutable view and context
generations.  Only the immutable context receipt crosses the lock boundary
into storage import.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
    plan_context_artifact_strict,
    publish_planned_context_artifact_strict,
)
from ..artifacts.strict_vector import (
    _plan_recaptured_vector_view,
    _publish_recaptured_vector_view,
)
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
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
from .index_compiler import IndexCompiler
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
    CURRENT_PORTABLE_BUILDER_SCHEMAS,
    DEFAULT_MAX_MANIFEST_BYTES,
    RepoManifestImportPlan,
    _manifest_limit,
    _preflight_json_bytes,
    _strict_json_loads,
    plan_repo_manifest_import_bytes,
)
from .snapshot_store import normalize_repo

_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SUPPORTED_CACHE_VIEWS = ("bm25", "vector")


class CompilerCacheTopologyGuard(Protocol):
    """Trusted application-level checks around one compiler-cache mutation."""

    def capture(self, cache: Path) -> None:
        """Capture topology after the cache lease binds its directory."""

    def verify(self, cache: Path) -> None:
        """Reject topology drift while the same cache lease remains held."""


@dataclass(frozen=True, slots=True)
class CompilerCacheViewRecaptureResult:
    """Detached evidence for one mutable-to-immutable view recapture."""

    view_type: str
    source_view: Path
    output_view: Path
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheViewRecaptureResult:
            raise TypeError("compiler cache view recapture must use the exact type")
        if (
            type(self.view_type) is not str
            or self.view_type not in _SUPPORTED_CACHE_VIEWS
        ):
            raise ValueError("compiler cache view recapture type is unsupported")
        if type(self.source_view) is not type(Path()) or type(
            self.output_view
        ) is not type(Path()):
            raise TypeError("compiler cache view recapture paths must be exact paths")
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        for records, label in (
            (source_records, "source"),
            (output_records, "output"),
        ):
            if not records or any(
                type(record) is not TreeFileRecord for record in records
            ):
                raise TypeError(
                    f"compiler cache view recapture {label} records are invalid"
                )
            if records != tuple(sorted(records, key=lambda item: item.path)):
                raise ValueError(
                    f"compiler cache view recapture {label} records are not canonical"
                )
            if len({record.path for record in records}) != len(records):
                raise ValueError(
                    f"compiler cache view recapture {label} records repeat a path"
                )
        if self.view_type == "bm25":
            expected = {"bm25_metadata.json", "documents.json"}
            if {record.path for record in source_records} != expected or {
                record.path for record in output_records
            } != expected:
                raise ValueError("compiler cache BM25 recapture records are incomplete")
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)

    @property
    def output_file_fingerprints(self) -> dict[str, dict[str, Any]]:
        """Return detached output fingerprints keyed by portable path."""

        return _fingerprints_for_records(self.output_records)


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
class CompilerCacheMultiViewImportResult:
    """Pure-data result of one atomic compiler-cache view-set import."""

    recaptures: tuple[CompilerCacheViewRecaptureResult, ...]
    canonical_manifest_bytes: bytes
    import_plan: RepoManifestImportPlan
    context_artifact: ContextArtifactResult
    import_result: RepoManifestImportResult

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheMultiViewImportResult:
            raise TypeError(
                "compiler cache multi-view import must use the exact result type"
            )
        recaptures = tuple(self.recaptures)
        if not recaptures or any(
            type(item) is not CompilerCacheViewRecaptureResult for item in recaptures
        ):
            raise TypeError("compiler cache multi-view recaptures are invalid")
        views = tuple(item.view_type for item in recaptures)
        if views != tuple(view for view in _SUPPORTED_CACHE_VIEWS if view in views):
            raise ValueError("compiler cache multi-view recaptures are not canonical")
        if len(set(views)) != len(views):
            raise ValueError("compiler cache multi-view recaptures repeat a view")
        if type(self.canonical_manifest_bytes) is not bytes:
            raise TypeError("compiler cache portable manifest must be exact bytes")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache multi-view import plan is invalid")
        if type(self.context_artifact) is not ContextArtifactResult:
            raise TypeError("compiler cache context artifact result is invalid")
        if type(self.import_result) is not RepoManifestImportResult:
            raise TypeError("compiler cache retained import result is invalid")
        if (
            self.import_plan.selection.selected_views != views
            or self.context_artifact.views != views
            or self.import_result.views != views
            or self.canonical_manifest_bytes
            != _pretty_manifest_bytes(self.import_plan.manifest)
        ):
            raise StorageIntegrityError(
                "compiler cache multi-view result identities are inconsistent"
            )
        object.__setattr__(self, "recaptures", recaptures)

    @property
    def views(self) -> tuple[str, ...]:
        return tuple(item.view_type for item in self.recaptures)

    @property
    def recapture_map(self) -> dict[str, CompilerCacheViewRecaptureResult]:
        return {item.view_type: item for item in self.recaptures}


@dataclass(frozen=True, slots=True)
class CompilerRetainedPublicationResult:
    """Result of updating compiler views and atomically retaining them."""

    manifest: RepoManifest
    retained_import: CompilerCacheMultiViewImportResult

    def __post_init__(self) -> None:
        if type(self) is not CompilerRetainedPublicationResult:
            raise TypeError("compiler retained publication must use the exact type")
        if type(self.manifest) is not RepoManifest:
            raise TypeError("compiler retained publication manifest is invalid")
        if type(self.retained_import) is not CompilerCacheMultiViewImportResult:
            raise TypeError("compiler retained publication import result is invalid")
        views = self.retained_import.views
        for view in views:
            _require_current_view(self.manifest, view=view)
        portable = self.retained_import.import_plan.manifest
        if (
            portable.commit != self.manifest.commit
            or portable.source_fingerprint != self.manifest.source_fingerprint
            or portable.file_count != self.manifest.file_count
            or portable.source_selection != self.manifest.source_selection
            or portable.last_indexed_commit != self.manifest.last_indexed_commit
            or portable.last_indexed_source_fingerprint
            != self.manifest.last_indexed_source_fingerprint
            or portable.last_indexed_source_selection_digest
            != self.manifest.last_indexed_source_selection_digest
        ):
            raise StorageIntegrityError(
                "compiler retained publication identities are inconsistent"
            )

    @property
    def views(self) -> tuple[str, ...]:
        return self.retained_import.views

    @property
    def import_result(self) -> RepoManifestImportResult:
        return self.retained_import.import_result


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


@dataclass(slots=True)
class _ImportOperation:
    cache: Path
    view_owners: dict[str, PublishedWorkspaceReceiptOwner]
    view_outputs: dict[str, Path]
    context_output: Path
    inputs: _ImportPreflight
    fixed_source_views: dict[str, Path]
    policy_forbidden: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _PreparedCompilerCacheImport:
    manifest: RepoManifest
    recaptures: tuple[CompilerCacheViewRecaptureResult, ...]
    canonical_manifest_bytes: bytes
    import_plan: RepoManifestImportPlan
    context_artifact: ContextArtifactResult


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
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    view_destinations: dict[str, Path],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    context_destination: Path,
) -> None:
    if type(views) is not tuple:
        raise TypeError("compiler cache views must be an exact tuple")
    if not views or any(type(view) is not str for view in views):
        raise ValueError("compiler cache views must contain exact view names")
    canonical_views = tuple(
        view for view in _SUPPORTED_CACHE_VIEWS if view in set(views)
    )
    if views != canonical_views:
        raise ValueError(
            "compiler cache views must be a non-empty canonical portable subset"
        )
    if type(view_output_owners) is not dict:
        raise TypeError("compiler cache view output owners must be an exact dict")
    if type(view_destinations) is not dict:
        raise TypeError("compiler cache view destinations must be an exact dict")
    if any(type(view) is not str for view in view_output_owners) or any(
        type(view) is not str for view in view_destinations
    ):
        raise TypeError("compiler cache view mapping keys must be exact text")
    if tuple(view_output_owners) != views or tuple(view_destinations) != views:
        raise ValueError(
            "compiler cache view mappings must have the exact selected keys and order"
        )
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("compiler cache source must be an exact source binding")
    owners = tuple(view_output_owners.values())
    if (
        any(type(owner) is not PublishedWorkspaceReceiptOwner for owner in owners)
        or type(context_output_owner) is not PublishedWorkspaceReceiptOwner
    ):
        raise TypeError("compiler cache outputs must be exact receipt owners")
    if len({id(owner) for owner in (*owners, context_output_owner)}) != len(owners) + 1:
        raise ValueError("compiler cache outputs must use distinct receipt owners")
    if any(type(path) is not type(Path()) for path in view_destinations.values()):
        raise TypeError("compiler cache view destinations must be exact paths")
    if type(context_destination) is not type(Path()):
        raise TypeError("compiler cache context destination must be an exact path")
    if not repository_source.usable:
        raise RuntimeError("compiler cache repository source is not usable")
    if any(owner.state != "empty" for owner in owners) or (
        context_output_owner.state != "empty"
    ):
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


def _preflight_cache_topology_guard(
    guard: CompilerCacheTopologyGuard | None,
) -> None:
    if guard is None:
        return
    for member in ("capture", "verify"):
        try:
            candidate = inspect.getattr_static(guard, member)
        except AttributeError as exc:
            raise TypeError(
                "compiler cache topology guard has an invalid contract"
            ) from exc
        if isinstance(candidate, (classmethod, staticmethod)):
            candidate = candidate.__func__
        if not callable(candidate):
            raise TypeError("compiler cache topology guard has an invalid contract")


def _cache_directory_identity(cache: Path) -> tuple[int, int]:
    try:
        metadata = cache.lstat()
    except OSError as exc:
        raise RuntimeError("compiler cache directory cannot be authenticated") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("compiler cache path is not a real directory")
    return (metadata.st_dev, metadata.st_ino)


def _run_cache_topology_guard(
    guard: CompilerCacheTopologyGuard | None,
    member: str,
    cache: Path,
) -> None:
    if guard is None:
        return
    result = getattr(guard, member)(cache)
    if result is not None:
        raise TypeError(f"compiler cache topology guard {member} must return None")


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
    view_destinations: tuple[Path, ...],
    context_destination: Path,
    *,
    cache: Path,
    repository: Path,
    source_views: tuple[Path, ...],
) -> None:
    lexical_boundaries = (cache, repository, *source_views)
    physical_boundaries = tuple(
        _resolved_path(path, strict=True, label="compiler cache input")
        for path in lexical_boundaries
    )
    outputs = (*view_destinations, context_destination)
    physical_destinations = tuple(
        _resolved_path(path, strict=False, label="compiler cache destination")
        for path in outputs
    )
    for destination, physical_destination in zip(
        outputs,
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
    for index, destination in enumerate(outputs):
        physical_destination = physical_destinations[index]
        for other, physical_other in zip(
            outputs[index + 1 :],
            physical_destinations[index + 1 :],
            strict=True,
        ):
            if (
                _path_relation(destination, other) != "disjoint"
                or _path_relation(physical_destination, physical_other) != "disjoint"
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
        raise ValueError(
            "compiler cache repository manifest is not exact "
            f"v{manifest.version} data"
        )
    return manifest


def compiler_cache_source_selection(
    cache_dir: str | Path,
    *,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> RepositorySourceSelection:
    """Return the exact source selection recorded by one compiler cache.

    The CLI needs this identity axis before it captures the retained repository
    source.  The import coordinator authenticates the same manifest again under
    its longer-lived cache lock, so a manifest replacement between these two
    reads can only make the later import fail closed.
    """

    cache = lexical_directory_path(Path(cache_dir))
    bounded_limit = _manifest_limit(max_manifest_bytes)
    with compiler_cache_lock(cache, create=False):
        manifest = _parse_exact_manifest(
            _read_manifest(cache, max_manifest_bytes=bounded_limit),
            max_manifest_bytes=bounded_limit,
        )
    selection = manifest.source_selection
    if selection is None:
        return DEFAULT_REPOSITORY_SOURCE_SELECTION
    return RepositorySourceSelection(selection.exclude_subtrees)


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
    selection = manifest.source_selection
    identity_selection = identity.source_selection
    if type(selection) is not RepositorySourceSelection:
        raise ValueError(
            "compiler cache repository manifest has no exact source selection"
        )
    if (
        type(identity_selection) is not RepositorySourceSelection
        or identity_selection != selection
        or identity_selection.digest != selection.digest
    ):
        raise StorageIntegrityError(
            "compiler cache repository source selection differs from the "
            "retained source"
        )


def _view_source(cache: Path, entry: IndexEntry, *, view: str) -> Path:
    declared = Path(entry.path).expanduser()
    if not declared.is_absolute():
        declared = cache / declared
    source = lexical_directory_path(declared)
    expected = lexical_directory_path(cache / view)
    if source != expected:
        raise ValueError(f"compiler cache {view} entry does not name its fixed view")
    return source


def _require_current_view(manifest: RepoManifest, *, view: str) -> IndexEntry:
    entry = manifest.indexes.get(view)
    selection_digest = manifest.source_selection_digest
    if (
        type(entry) is not IndexEntry
        or entry.index_type != view
        or entry.status != "fresh"
        or entry.commit != manifest.commit
        or entry.source_fingerprint != manifest.source_fingerprint
        or not manifest.index_is_current(view)
        or type(selection_digest) is not str
        or entry.source_selection_digest != selection_digest
        or entry.config.get("source_selection_digest") != selection_digest
        or type(entry.config.get("builder_schema")) is not int
        or entry.config.get("builder_schema") != CURRENT_PORTABLE_BUILDER_SCHEMAS[view]
    ):
        raise ValueError(
            f"compiler cache repository manifest has no exact current {view}"
        )
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


def _plan_cache_view(
    view: str,
    source: Path,
    destination: Path,
    *,
    repository_source: RepositorySourceBinding,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> Any:
    if view == "bm25":
        return _plan_recaptured_bm25_view(
            source,
            destination,
            repository_source=repository_source,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
        )
    if view == "vector":
        return _plan_recaptured_vector_view(
            source,
            destination,
            repository_source=repository_source,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
        )
    raise AssertionError(f"unsupported compiler cache view: {view}")


def _publish_cache_view(
    view: str,
    source: Path,
    destination: Path,
    *,
    planned: Any,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    if view == "bm25":
        return _publish_recaptured_bm25_view(
            source,
            destination,
            planned=planned,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=output_receipt_owner,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
        )
    if view == "vector":
        return _publish_recaptured_vector_view(
            source,
            destination,
            planned=planned,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=output_receipt_owner,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
        )
    raise AssertionError(f"unsupported compiler cache view: {view}")


def _planned_adjustments(view: str, planned: Any) -> dict[str, Any]:
    adjustments = planned.adjustments
    if type(adjustments) is not dict:
        raise TypeError(f"compiler cache {view} plan adjustments are invalid")
    if view == "bm25" and adjustments != _fingerprints_adjustment(
        planned.output_records
    ):
        raise StorageIntegrityError(
            "compiler cache BM25 plan adjustments differ from its output records"
        )
    if view == "vector" and set(adjustments) != {
        "artifact_scope",
        "portable_document_format",
        "persistence_config_fingerprint",
    }:
        raise StorageIntegrityError(
            "compiler cache vector plan adjustments are incomplete"
        )
    return copy.deepcopy(adjustments)


def _pretty_manifest_bytes(manifest: RepoManifest) -> bytes:
    return _canonical_json_bytes(
        manifest.to_dict(),
        label="compiler cache portable repository manifest",
    )


def _portable_manifest(
    manifest: RepoManifest,
    *,
    views: tuple[str, ...],
    planned_views: dict[str, Any],
) -> tuple[RepoManifest, bytes]:
    portable = copy.deepcopy(manifest.to_dict())
    portable["repo"]["path"] = "source"
    entries: dict[str, Any] = {}
    for view in views:
        entry = copy.deepcopy(portable["indexes"][view])
        entry["path"] = f"views/{view}"
        adjustments = _planned_adjustments(view, planned_views[view])
        for section_name in ("config", "metadata"):
            section = entry.get(section_name)
            if type(section) is not dict:
                raise ValueError(
                    f"compiler cache {view} {section_name} must be an exact object"
                )
            section.update(copy.deepcopy(adjustments))
        entries[view] = entry
    portable["indexes"] = entries
    portable["capabilities"] = _portable_capabilities(
        portable["capabilities"],
        views,
    )
    try:
        portable_manifest = RepoManifest.from_dict(portable)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("compiler cache portable manifest is invalid") from exc
    if portable_manifest.to_dict() != portable:
        raise ValueError("compiler cache portable manifest is not canonical")
    selection_digest = manifest.source_selection_digest
    if (
        portable_manifest.source_selection != manifest.source_selection
        or portable_manifest.last_indexed_source_selection_digest
        != manifest.last_indexed_source_selection_digest
        or type(selection_digest) is not str
        or any(
            portable_manifest.indexes[view].source_selection_digest != selection_digest
            or portable_manifest.indexes[view].config.get("source_selection_digest")
            != selection_digest
            for view in views
        )
    ):
        raise StorageIntegrityError(
            "compiler cache portable manifest changed its source selection"
        )
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


def _preflight_cache_import_operation(
    cache_dir: str | Path,
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destinations: dict[str, Path],
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
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
) -> _ImportOperation:
    """Validate borrowed authorities without touching the compiler cache."""

    _preflight_authorities(
        views=views,
        repository_source=repository_source,
        view_output_owners=view_output_owners,
        view_destinations=view_destinations,
        context_output_owner=context_output_owner,
        context_destination=context_destination,
    )
    view_owners = dict(view_output_owners)
    destination_inputs = dict(view_destinations)
    cache = lexical_directory_path(Path(cache_dir))
    view_outputs = {
        view: lexical_directory_path(destination_inputs[view]) for view in views
    }
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
    fixed_source_views = {view: lexical_directory_path(cache / view) for view in views}
    policy_forbidden = _snapshot_forbidden_paths(
        (
            *inputs.forbidden_paths,
            cache,
            *fixed_source_views.values(),
            *view_outputs.values(),
            context_output,
        )
    )
    _preflight_workspace_provider(workspace_provider)
    _preflight_catalog_contract(catalog)
    return _ImportOperation(
        cache=cache,
        view_owners=view_owners,
        view_outputs=view_outputs,
        context_output=context_output,
        inputs=inputs,
        fixed_source_views=fixed_source_views,
        policy_forbidden=policy_forbidden,
    )


def _prepare_compiler_cache_import_locked(
    operation: _ImportOperation,
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    workspace_provider: StrictWorkspaceProvider,
    expected_manifest: RepoManifest | None = None,
) -> _PreparedCompilerCacheImport:
    """Recapture selected views while the caller holds the cache lease."""

    cache = operation.cache
    inputs = operation.inputs
    source_manifest_bytes = _read_manifest(
        cache,
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    manifest = _parse_exact_manifest(
        source_manifest_bytes,
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    if expected_manifest is not None:
        if type(expected_manifest) is not RepoManifest:
            raise TypeError("compiler update returned an invalid repository manifest")
        if expected_manifest.to_dict() != manifest.to_dict():
            raise StorageIntegrityError(
                "compiler update result differs from its exact serialized manifest"
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
    entries = {view: _require_current_view(manifest, view=view) for view in views}
    source_views = {
        view: _view_source(cache, entries[view], view=view) for view in views
    }
    _destination_topology(
        tuple(operation.view_outputs.values()),
        operation.context_output,
        cache=cache,
        repository=lexical_directory_path(identity.root),
        source_views=tuple(source_views.values()),
    )
    if source_views != operation.fixed_source_views:  # pragma: no cover
        raise AssertionError("compiler cache fixed view sources changed")

    # Every raw-cache recapture plan plus the exact portable manifest and
    # retained import plan exists before the first provider call can mutate
    # any workspace destination.  The context plan is authority-dependent:
    # it is built later from the published view receipts, before context
    # workspace mutation.
    planned_views: dict[str, Any] = {}
    for view in views:
        planned = _plan_cache_view(
            view,
            source_views[view],
            operation.view_outputs[view],
            repository_source=repository_source,
            view_config=entries[view].config,
            forbidden_paths=operation.policy_forbidden,
            environ=inputs.environment,
        )
        if view == "bm25":
            _require_source_fingerprints(entries[view], planned)
        _planned_adjustments(view, planned)
        planned_views[view] = planned
    portable_manifest, canonical_manifest_bytes = _portable_manifest(
        manifest,
        views=views,
        planned_views=planned_views,
    )
    import_plan = plan_repo_manifest_import_bytes(
        canonical_manifest_bytes,
        views=views,
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    if (
        import_plan.selection.selected_views != views
        or import_plan.manifest.to_dict() != portable_manifest.to_dict()
    ):
        raise StorageIntegrityError(
            "compiler cache portable manifest changed during import planning"
        )

    recaptures: list[CompilerCacheViewRecaptureResult] = []
    for view in views:
        planned = planned_views[view]
        adjustments = _publish_cache_view(
            view,
            source_views[view],
            operation.view_outputs[view],
            planned=planned,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=operation.view_owners[view],
            view_config=entries[view].config,
            forbidden_paths=operation.policy_forbidden,
            environ=inputs.environment,
        )
        if adjustments != _planned_adjustments(view, planned):
            raise StorageIntegrityError(
                f"compiler cache {view} publication differs from its exact plan"
            )
        recaptures.append(
            CompilerCacheViewRecaptureResult(
                view_type=view,
                source_view=source_views[view],
                output_view=operation.view_outputs[view],
                source_records=planned.source_records,
                output_records=planned.output_records,
            )
        )

    planned_context = plan_context_artifact_strict(
        portable_manifest,
        repository=inputs.repository_key,
        repository_source=repository_source,
        view_generations=operation.view_owners,
        environ=inputs.environment,
    )
    if (
        planned_context.views != views
        or planned_context.manifest_payload != canonical_manifest_bytes
    ):
        raise StorageIntegrityError(
            "compiler cache context plan differs from its portable manifest"
        )
    context_artifact = publish_planned_context_artifact_strict(
        operation.context_output,
        planned=planned_context,
        manifest=portable_manifest,
        repository=inputs.repository_key,
        repository_source=repository_source,
        view_generations=operation.view_owners,
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
    return _PreparedCompilerCacheImport(
        manifest=manifest,
        recaptures=tuple(recaptures),
        canonical_manifest_bytes=canonical_manifest_bytes,
        import_plan=import_plan,
        context_artifact=context_artifact,
    )


def _commit_prepared_compiler_cache_import(
    preparation: _PreparedCompilerCacheImport,
    operation: _ImportOperation,
    *,
    repository_source: RepositorySourceBinding,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
) -> CompilerCacheMultiViewImportResult:
    """Commit immutable evidence after the mutable cache lease is released."""

    inputs = operation.inputs
    import_result = import_retained_repo_manifest(
        preparation.import_plan,
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
        forbidden_paths=operation.policy_forbidden,
        environ=inputs.environment,
    )
    return CompilerCacheMultiViewImportResult(
        recaptures=preparation.recaptures,
        canonical_manifest_bytes=preparation.canonical_manifest_bytes,
        import_plan=preparation.import_plan,
        context_artifact=preparation.context_artifact,
        import_result=import_result,
    )


def import_compiler_cache(
    cache_dir: str | Path,
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destinations: dict[str, Path],
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
) -> CompilerCacheMultiViewImportResult:
    """Recapture and import one exact current compiler-cache view set.

    All receipt owners and the retained repository binding are borrowed.  The
    caller remains responsible for closing them on success and failure.
    Published directories are immutable evidence and are never recursively
    removed when a later publication or retained import fails.
    """

    operation = _preflight_cache_import_operation(
        cache_dir,
        views=views,
        repository_source=repository_source,
        view_output_owners=view_output_owners,
        context_output_owner=context_output_owner,
        view_destinations=view_destinations,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
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
    with compiler_cache_lock(operation.cache, create=False):
        preparation = _prepare_compiler_cache_import_locked(
            operation,
            views=views,
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
        )

    # Catalog/CAS publication deliberately occurs after releasing the mutable
    # compiler cache lock.  The context receipt remains the exact authority
    # authenticated above, so the mutable cache is no longer consulted.
    return _commit_prepared_compiler_cache_import(
        preparation,
        operation,
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        catalog=catalog,
        object_store=object_store,
    )


def compile_and_import_repo(
    compiler: IndexCompiler,
    repo_path: str | Path,
    *,
    cache_dir: str | Path,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destinations: dict[str, Path],
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
    cache_topology_guard: CompilerCacheTopologyGuard | None = None,
) -> CompilerRetainedPublicationResult:
    """Update-or-create selected compiler views and retain one snapshot.

    The mutable compiler update and strict immutable recapture share one cache
    lease.  The lease is released before the retained importer invokes any
    CAS or catalog data-plane method; static contract probes may run earlier.
    ``cache_topology_guard`` is a trusted application-level denial check for
    topology that needs the newly created, lease-bound cache directory.  It is
    not an authority token: ``capture`` runs before compiler mutation and
    ``verify`` runs before retained workspace mutation and again before lease
    release.

    All source and receipt authorities are borrowed.  The caller owns their
    cleanup, including immutable evidence left by a later storage failure.
    There is intentionally no force-rebuild option: this route only performs
    the compiler's ordinary update-or-create policy.
    """

    if type(compiler) is not IndexCompiler:
        raise TypeError("compiler retained publication requires an IndexCompiler")
    _preflight_cache_topology_guard(cache_topology_guard)
    repository = lexical_directory_path(Path(repo_path))
    operation = _preflight_cache_import_operation(
        cache_dir,
        views=views,
        repository_source=repository_source,
        view_output_owners=view_output_owners,
        context_output_owner=context_output_owner,
        view_destinations=view_destinations,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
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

    with compiler_cache_lock(operation.cache):
        cache_identity = _cache_directory_identity(operation.cache)
        identity = repository_source.authenticated_identity_snapshot()
        if type(identity) is not RepositorySourceIdentitySnapshot:
            raise TypeError("compiler cache repository identity has an invalid type")
        if lexical_directory_path(identity.root) != repository:
            raise StorageIntegrityError(
                "compiler retained publication source belongs to another repository"
            )
        _cache_topology(operation.cache, identity)
        _run_cache_topology_guard(
            cache_topology_guard,
            "capture",
            operation.cache,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError(
                "compiler cache directory changed during topology capture"
            )
        compiled_manifest = compiler._update_repo_locked(
            str(repository),
            index_types=list(views),
            cache=str(operation.cache),
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError("compiler cache directory changed during compilation")
        _run_cache_topology_guard(
            cache_topology_guard,
            "verify",
            operation.cache,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError(
                "compiler cache directory changed during topology verify"
            )
        preparation = _prepare_compiler_cache_import_locked(
            operation,
            views=views,
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
            expected_manifest=compiled_manifest,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError("compiler cache directory changed during recapture")
        _run_cache_topology_guard(
            cache_topology_guard,
            "verify",
            operation.cache,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError(
                "compiler cache directory changed during topology verify"
            )

    retained_import = _commit_prepared_compiler_cache_import(
        preparation,
        operation,
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        catalog=catalog,
        object_store=object_store,
    )
    return CompilerRetainedPublicationResult(
        manifest=compiled_manifest,
        retained_import=retained_import,
    )


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
    """Compatibility wrapper for one exact current BM25 cache import."""

    result = import_compiler_cache(
        cache_dir,
        views=("bm25",),
        repository_source=repository_source,
        view_output_owners={"bm25": bm25_output_owner},
        context_output_owner=context_output_owner,
        view_destinations={"bm25": bm25_destination},
        context_destination=context_destination,
        workspace_provider=workspace_provider,
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
    recapture = result.recaptures[0]
    if recapture.view_type != "bm25":  # pragma: no cover - wrapper invariant
        raise AssertionError("compiler cache BM25 wrapper selected another view")
    return CompilerCacheImportResult(
        recapture=CompilerCacheBm25RecaptureResult(
            source_view=recapture.source_view,
            output_view=recapture.output_view,
            source_records=recapture.source_records,
            output_records=recapture.output_records,
            canonical_manifest_bytes=result.canonical_manifest_bytes,
        ),
        import_plan=result.import_plan,
        context_artifact=result.context_artifact,
        import_result=result.import_result,
    )


def _fingerprints_adjustment(
    records: tuple[TreeFileRecord, ...],
) -> dict[str, Any]:
    return {"artifact_file_fingerprints": _fingerprints_for_records(records)}


__all__ = [
    "CompilerCacheBm25RecaptureResult",
    "CompilerCacheImportResult",
    "CompilerCacheMultiViewImportResult",
    "CompilerCacheTopologyGuard",
    "CompilerCacheViewRecaptureResult",
    "CompilerRetainedPublicationResult",
    "compile_and_import_repo",
    "import_compiler_cache",
    "import_compiler_cache_bm25",
]
