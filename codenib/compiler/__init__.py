# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Compiler infrastructure for CodeNib.

The package provides:

- **Index compilation** (Phase 1): ``IndexCompiler`` builds all indexes for a
  repository and writes a ``RepoManifest`` recording what was built, where,
  and when.

- **Resource management**: ``ResourceResolver`` checks index freshness and
  produces ``ResourcePlan`` decisions used by ``ResourceGuard`` to filter
  available skills at query time.

- **Parameter resolution**: ``SessionContext`` + ``resolve_params`` merge
  config defaults, session adjustments, and query-time overrides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from codenib.compiler.cache_import import (
        CompilerCacheBm25RecaptureResult,
        CompilerCacheImportResult,
        CompilerCacheMultiViewImportResult,
        CompilerCacheTopologyGuard,
        CompilerCacheViewRecaptureResult,
        CompilerRetainedPublicationResult,
        compile_and_import_repo,
        import_compiler_cache,
        import_compiler_cache_bm25,
    )
    from codenib.compiler.index_builders import IndexBuilderRegistry
    from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from codenib.compiler.manifest import ManifestIndexStateStore, RepoManifest
    from codenib.compiler.manifest_export import (
        RepoManifestExportReceipt,
        RepoManifestExportResult,
        RepoManifestViewExportReceipt,
        export_retained_repo_manifest_ref,
        export_retained_repo_manifest_snapshot,
    )
    from codenib.compiler.manifest_import import (
        RepoManifestImportResult,
        import_retained_repo_manifest,
    )
    from codenib.compiler.manifest_materialization import (
        RepoManifestMaterializationResult,
        materialize_retained_context_artifact,
        materialize_retained_repo_manifest_ref,
        materialize_retained_repo_manifest_snapshot,
    )
    from codenib.compiler.manifest_storage import (
        RepoManifestImportPlan,
        SourceIntent,
        ViewImportIntent,
        ViewSelection,
        plan_repo_manifest_import,
        plan_repo_manifest_import_bytes,
    )
    from codenib.compiler.params import ResolvedParams, SessionContext, resolve_params
    from codenib.compiler.resources import (
        IndexRequirement,
        IndexState,
        ResourcePlan,
        ResourceResolver,
    )
    from codenib.compiler.skill_context import (
        build_skill_contexts,
        load_contexts_from_manifest,
        required_index_types,
    )

_EXPORTS = {
    "CompilerCacheBm25RecaptureResult": (
        "codenib.compiler.cache_import",
        "CompilerCacheBm25RecaptureResult",
    ),
    "CompilerCacheImportResult": (
        "codenib.compiler.cache_import",
        "CompilerCacheImportResult",
    ),
    "CompilerCacheMultiViewImportResult": (
        "codenib.compiler.cache_import",
        "CompilerCacheMultiViewImportResult",
    ),
    "CompilerCacheTopologyGuard": (
        "codenib.compiler.cache_import",
        "CompilerCacheTopologyGuard",
    ),
    "CompilerCacheViewRecaptureResult": (
        "codenib.compiler.cache_import",
        "CompilerCacheViewRecaptureResult",
    ),
    "CompilerRetainedPublicationResult": (
        "codenib.compiler.cache_import",
        "CompilerRetainedPublicationResult",
    ),
    "compile_and_import_repo": (
        "codenib.compiler.cache_import",
        "compile_and_import_repo",
    ),
    "import_compiler_cache": (
        "codenib.compiler.cache_import",
        "import_compiler_cache",
    ),
    "import_compiler_cache_bm25": (
        "codenib.compiler.cache_import",
        "import_compiler_cache_bm25",
    ),
    "IndexCompiler": ("codenib.compiler.index_compiler", "IndexCompiler"),
    "IndexCompilerConfig": (
        "codenib.compiler.index_compiler",
        "IndexCompilerConfig",
    ),
    "IndexBuilderRegistry": (
        "codenib.compiler.index_builders",
        "IndexBuilderRegistry",
    ),
    "RepoManifest": ("codenib.compiler.manifest", "RepoManifest"),
    "ManifestIndexStateStore": (
        "codenib.compiler.manifest",
        "ManifestIndexStateStore",
    ),
    "RepoManifestExportReceipt": (
        "codenib.compiler.manifest_export",
        "RepoManifestExportReceipt",
    ),
    "RepoManifestExportResult": (
        "codenib.compiler.manifest_export",
        "RepoManifestExportResult",
    ),
    "RepoManifestViewExportReceipt": (
        "codenib.compiler.manifest_export",
        "RepoManifestViewExportReceipt",
    ),
    "RepoManifestImportResult": (
        "codenib.compiler.manifest_import",
        "RepoManifestImportResult",
    ),
    "RepoManifestImportPlan": (
        "codenib.compiler.manifest_storage",
        "RepoManifestImportPlan",
    ),
    "SourceIntent": ("codenib.compiler.manifest_storage", "SourceIntent"),
    "ViewImportIntent": (
        "codenib.compiler.manifest_storage",
        "ViewImportIntent",
    ),
    "ViewSelection": ("codenib.compiler.manifest_storage", "ViewSelection"),
    "plan_repo_manifest_import": (
        "codenib.compiler.manifest_storage",
        "plan_repo_manifest_import",
    ),
    "plan_repo_manifest_import_bytes": (
        "codenib.compiler.manifest_storage",
        "plan_repo_manifest_import_bytes",
    ),
    "import_retained_repo_manifest": (
        "codenib.compiler.manifest_import",
        "import_retained_repo_manifest",
    ),
    "materialize_retained_context_artifact": (
        "codenib.compiler.manifest_materialization",
        "materialize_retained_context_artifact",
    ),
    "RepoManifestMaterializationResult": (
        "codenib.compiler.manifest_materialization",
        "RepoManifestMaterializationResult",
    ),
    "materialize_retained_repo_manifest_ref": (
        "codenib.compiler.manifest_materialization",
        "materialize_retained_repo_manifest_ref",
    ),
    "materialize_retained_repo_manifest_snapshot": (
        "codenib.compiler.manifest_materialization",
        "materialize_retained_repo_manifest_snapshot",
    ),
    "export_retained_repo_manifest_ref": (
        "codenib.compiler.manifest_export",
        "export_retained_repo_manifest_ref",
    ),
    "export_retained_repo_manifest_snapshot": (
        "codenib.compiler.manifest_export",
        "export_retained_repo_manifest_snapshot",
    ),
    "ResourceResolver": ("codenib.compiler.resources", "ResourceResolver"),
    "ResourcePlan": ("codenib.compiler.resources", "ResourcePlan"),
    "IndexRequirement": ("codenib.compiler.resources", "IndexRequirement"),
    "IndexState": ("codenib.compiler.resources", "IndexState"),
    "SessionContext": ("codenib.compiler.params", "SessionContext"),
    "resolve_params": ("codenib.compiler.params", "resolve_params"),
    "ResolvedParams": ("codenib.compiler.params", "ResolvedParams"),
    "build_skill_contexts": (
        "codenib.compiler.skill_context",
        "build_skill_contexts",
    ),
    "load_contexts_from_manifest": (
        "codenib.compiler.skill_context",
        "load_contexts_from_manifest",
    ),
    "required_index_types": (
        "codenib.compiler.skill_context",
        "required_index_types",
    ),
}

__all__ = [
    # Index compilation
    "CompilerCacheBm25RecaptureResult",
    "CompilerCacheImportResult",
    "CompilerCacheMultiViewImportResult",
    "CompilerCacheTopologyGuard",
    "CompilerCacheViewRecaptureResult",
    "CompilerRetainedPublicationResult",
    "compile_and_import_repo",
    "import_compiler_cache",
    "import_compiler_cache_bm25",
    "IndexCompiler",
    "IndexCompilerConfig",
    "IndexBuilderRegistry",
    # Manifest
    "RepoManifest",
    "ManifestIndexStateStore",
    "RepoManifestExportReceipt",
    "RepoManifestExportResult",
    "RepoManifestViewExportReceipt",
    "RepoManifestImportResult",
    "RepoManifestImportPlan",
    "SourceIntent",
    "ViewImportIntent",
    "ViewSelection",
    "plan_repo_manifest_import",
    "plan_repo_manifest_import_bytes",
    "import_retained_repo_manifest",
    "RepoManifestMaterializationResult",
    "materialize_retained_context_artifact",
    "materialize_retained_repo_manifest_ref",
    "materialize_retained_repo_manifest_snapshot",
    "export_retained_repo_manifest_ref",
    "export_retained_repo_manifest_snapshot",
    # Resources
    "ResourceResolver",
    "ResourcePlan",
    "IndexRequirement",
    "IndexState",
    # Parameters
    "SessionContext",
    "resolve_params",
    "ResolvedParams",
    # Skill-aware contexts
    "build_skill_contexts",
    "load_contexts_from_manifest",
    "required_index_types",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
