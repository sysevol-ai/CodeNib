# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end construction for multimodal repository knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from .media_artifacts import discover_media_manifest
from .media_facts import VisualFactExtractor, build_visual_facts_manifest
from .media_grounding import (
    discover_source_symbol_candidates,
    ground_visual_facts_to_sources,
)
from .media_knowledge import build_multimodal_knowledge_view


def build_multimodal_repository_knowledge(
    repo_path: str | Path,
    *,
    commit: str | None = None,
    exclude_roots: tuple[str | Path, ...] = (),
    selection: RepositorySourceSelection = DEFAULT_REPOSITORY_SOURCE_SELECTION,
    extractor: VisualFactExtractor | None = None,
    max_artifacts: int = 4096,
    max_source_candidates: int = 8192,
) -> dict[str, Any]:
    """Build the deterministic multimodal repository knowledge bundle."""

    media_manifest = discover_media_manifest(
        repo_path,
        commit=commit,
        exclude_roots=exclude_roots,
        selection=selection,
        max_artifacts=max_artifacts,
    )
    facts_kwargs: dict[str, Any] = {}
    if extractor is not None:
        facts_kwargs["extractor"] = extractor
    visual_facts_manifest = build_visual_facts_manifest(media_manifest, **facts_kwargs)
    source_candidates = discover_source_symbol_candidates(
        repo_path,
        exclude_roots=exclude_roots,
        selection=selection,
        max_candidates=max_source_candidates,
    )
    grounding_manifest = ground_visual_facts_to_sources(
        visual_facts_manifest,
        source_candidates,
    )
    knowledge_view = build_multimodal_knowledge_view(
        media_manifest,
        visual_facts_manifest,
        grounding_manifest,
    )
    return {
        "media_manifest": media_manifest,
        "visual_facts_manifest": visual_facts_manifest,
        "source_candidate_count": len(source_candidates),
        "grounding_manifest": grounding_manifest,
        "knowledge_view": knowledge_view,
    }


__all__ = ["build_multimodal_repository_knowledge"]
