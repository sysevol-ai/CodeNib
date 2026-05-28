# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Compiler infrastructure for Codeminer.

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

from .index_builders import IndexBuilderRegistry
from .index_compiler import IndexCompiler, IndexCompilerConfig
from .manifest import ManifestIndexStateStore, RepoManifest
from .params import ResolvedParams, SessionContext, resolve_params
from .resources import IndexRequirement, IndexState, ResourcePlan, ResourceResolver
from .skill_context import (
    build_skill_contexts,
    load_contexts_from_manifest,
    required_index_types,
)

__all__ = [
    # Index compilation
    "IndexCompiler",
    "IndexCompilerConfig",
    "IndexBuilderRegistry",
    # Manifest
    "RepoManifest",
    "ManifestIndexStateStore",
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
