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
    from codenib.compiler.index_builders import IndexBuilderRegistry
    from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from codenib.compiler.manifest import ManifestIndexStateStore, RepoManifest
    from codenib.compiler.manifest_import import (
        RepoManifestImportResult,
        import_retained_repo_manifest,
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
    "IndexCompiler",
    "IndexCompilerConfig",
    "IndexBuilderRegistry",
    # Manifest
    "RepoManifest",
    "ManifestIndexStateStore",
    "RepoManifestImportResult",
    "RepoManifestImportPlan",
    "SourceIntent",
    "ViewImportIntent",
    "ViewSelection",
    "plan_repo_manifest_import",
    "plan_repo_manifest_import_bytes",
    "import_retained_repo_manifest",
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
