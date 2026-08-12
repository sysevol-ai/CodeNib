# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared immutable contracts for retained RepoManifest projections."""

from __future__ import annotations

from typing import Any

from ..storage.models import ViewProfile
from .manifest import MANIFEST_VERSION
from .manifest_storage import ViewImportIntent

REPO_MANIFEST_IMPORT_GENERATION_CONTRACT = "codenib.repo-manifest-import-generation.v2"
REPO_MANIFEST_PROJECTION_VIEW = "codenib.internal.repo-manifest"
REPO_MANIFEST_PROJECTION_SCHEMA = "codenib.internal.repo-manifest.v2"
REPO_MANIFEST_PROJECTION_MEDIA_TYPE = (
    "application/vnd.codenib.repo-manifest-projection.v2+json"
)
REPO_MANIFEST_PROJECTION_PROFILE_NAME = "canonical"


def repo_manifest_projection_profile() -> ViewProfile:
    return ViewProfile.create(
        REPO_MANIFEST_PROJECTION_VIEW,
        {
            "contract": REPO_MANIFEST_PROJECTION_SCHEMA,
            "builder_schema": REPO_MANIFEST_PROJECTION_SCHEMA,
            "artifact_schema": REPO_MANIFEST_PROJECTION_SCHEMA,
        },
        name=REPO_MANIFEST_PROJECTION_PROFILE_NAME,
    )


def repo_manifest_generation_metadata(intent: ViewImportIntent) -> dict[str, Any]:
    return {
        "contract": REPO_MANIFEST_IMPORT_GENERATION_CONTRACT,
        "manifest_version": MANIFEST_VERSION,
        "generation_record_digest": intent.generation_record_digest,
        "verification_scope": "content-bytes",
        "native_execution": (
            "inert" if intent.view_type == "vector" else "not-required"
        ),
    }


__all__ = [
    "REPO_MANIFEST_IMPORT_GENERATION_CONTRACT",
    "REPO_MANIFEST_PROJECTION_MEDIA_TYPE",
    "REPO_MANIFEST_PROJECTION_PROFILE_NAME",
    "REPO_MANIFEST_PROJECTION_SCHEMA",
    "REPO_MANIFEST_PROJECTION_VIEW",
    "repo_manifest_generation_metadata",
    "repo_manifest_projection_profile",
]
