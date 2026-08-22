# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end construction for multimodal repository knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from .media_artifacts import discover_media_manifest
from .media_facts import VisualFactExtractor, build_visual_facts_manifest
from .media_grounding import (
    VisualGroundingScorer,
    discover_source_symbol_candidates,
    ground_visual_facts_to_sources,
)
from .media_knowledge import build_multimodal_knowledge_view
from .media_storage import build_multimodal_knowledge_bundle


def build_multimodal_repository_knowledge(
    repo_path: str | Path,
    *,
    commit: str | None = None,
    exclude_roots: Iterable[str | Path] = (),
    selection: RepositorySourceSelection = DEFAULT_REPOSITORY_SOURCE_SELECTION,
    extractor: VisualFactExtractor | None = None,
    scorer: VisualGroundingScorer | None = None,
    max_artifacts: int = 4096,
    max_source_candidates: int = 8192,
) -> dict[str, Any]:
    """Build the deterministic multimodal repository knowledge bundle."""

    root = _repository_root(repo_path)
    excluded = tuple(exclude_roots)
    media_manifest = discover_media_manifest(
        root,
        commit=commit,
        exclude_roots=excluded,
        selection=selection,
        max_artifacts=max_artifacts,
    )
    facts_kwargs: dict[str, Any] = {}
    if extractor is not None:
        facts_kwargs["extractor"] = extractor
    visual_facts_manifest = build_visual_facts_manifest(media_manifest, **facts_kwargs)
    source_candidates = discover_source_symbol_candidates(
        root,
        exclude_roots=excluded,
        selection=selection,
        max_candidates=max_source_candidates,
    )
    grounding_manifest = ground_visual_facts_to_sources(
        visual_facts_manifest,
        source_candidates,
        scorer=scorer,
    )
    knowledge_view = build_multimodal_knowledge_view(
        media_manifest,
        visual_facts_manifest,
        grounding_manifest,
    )
    return build_multimodal_knowledge_bundle(
        media_manifest=media_manifest,
        visual_facts_manifest=visual_facts_manifest,
        source_candidate_count=len(source_candidates),
        grounding_manifest=grounding_manifest,
        knowledge_view=knowledge_view,
    )


def _repository_root(repo_path: str | Path) -> Path:
    root = Path(repo_path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"repository root is not a directory: {root}")
    return root


__all__ = ["build_multimodal_repository_knowledge"]
