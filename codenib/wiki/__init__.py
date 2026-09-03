# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Index-derived wiki generation for the DeepWiki-style demo.

Produces a per-repo page tree and source-grounded page content from the
already-loaded BM25 / vector indexes — no LLM required. When an LLM is
configured it can later refine the prose, but every code anchor here resolves
to a real symbol span pulled from the indexes (no fabricated lines).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from .builder import WikiBuilder
    from .media_artifacts import discover_media_manifest
    from .media_eval import (
        evaluate_mmwiki_predictions,
        evaluate_visual_code_grounding,
        evaluate_visual_fact_extraction,
    )
    from .media_evidence import build_media_evidence_pack
    from .media_facts import build_visual_facts_manifest, deterministic_visual_facts
    from .media_grounding import (
        VisualGroundingScorer,
        discover_source_symbol_candidates,
        ground_visual_facts_to_sources,
    )
    from .media_incremental import (
        diff_media_manifests,
        merge_incremental_visual_facts,
        plan_incremental_visual_fact_update,
    )
    from .media_knowledge import (
        build_multimodal_knowledge_view,
        find_visual_code_links,
        get_visual_evidence,
        search_visual_context,
    )
    from .media_pipeline import build_multimodal_repository_knowledge
    from .media_storage import (
        MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA,
        MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION,
        build_multimodal_knowledge_bundle,
        load_multimodal_knowledge_bundle,
        save_multimodal_knowledge_bundle,
        validate_multimodal_knowledge_bundle,
    )
    from .media_tools import MultimodalKnowledgeToolRouter, multimodal_tool_schemas
    from .media_vlm import (
        OpenAICompatibleVisualFactExtractor,
        visual_fact_extractor_from_config,
    )

_EXPORTS = {
    "MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA": (
        "codenib.wiki.media_storage",
        "MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA",
    ),
    "MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION": (
        "codenib.wiki.media_storage",
        "MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION",
    ),
    "MultimodalKnowledgeToolRouter": (
        "codenib.wiki.media_tools",
        "MultimodalKnowledgeToolRouter",
    ),
    "WikiBuilder": ("codenib.wiki.builder", "WikiBuilder"),
    "OpenAICompatibleVisualFactExtractor": (
        "codenib.wiki.media_vlm",
        "OpenAICompatibleVisualFactExtractor",
    ),
    "VisualGroundingScorer": (
        "codenib.wiki.media_grounding",
        "VisualGroundingScorer",
    ),
    "build_media_evidence_pack": (
        "codenib.wiki.media_evidence",
        "build_media_evidence_pack",
    ),
    "build_multimodal_knowledge_view": (
        "codenib.wiki.media_knowledge",
        "build_multimodal_knowledge_view",
    ),
    "build_multimodal_knowledge_bundle": (
        "codenib.wiki.media_storage",
        "build_multimodal_knowledge_bundle",
    ),
    "build_multimodal_repository_knowledge": (
        "codenib.wiki.media_pipeline",
        "build_multimodal_repository_knowledge",
    ),
    "build_visual_facts_manifest": (
        "codenib.wiki.media_facts",
        "build_visual_facts_manifest",
    ),
    "deterministic_visual_facts": (
        "codenib.wiki.media_facts",
        "deterministic_visual_facts",
    ),
    "diff_media_manifests": (
        "codenib.wiki.media_incremental",
        "diff_media_manifests",
    ),
    "discover_media_manifest": (
        "codenib.wiki.media_artifacts",
        "discover_media_manifest",
    ),
    "discover_source_symbol_candidates": (
        "codenib.wiki.media_grounding",
        "discover_source_symbol_candidates",
    ),
    "evaluate_mmwiki_predictions": (
        "codenib.wiki.media_eval",
        "evaluate_mmwiki_predictions",
    ),
    "evaluate_visual_code_grounding": (
        "codenib.wiki.media_eval",
        "evaluate_visual_code_grounding",
    ),
    "evaluate_visual_fact_extraction": (
        "codenib.wiki.media_eval",
        "evaluate_visual_fact_extraction",
    ),
    "find_visual_code_links": (
        "codenib.wiki.media_knowledge",
        "find_visual_code_links",
    ),
    "get_visual_evidence": (
        "codenib.wiki.media_knowledge",
        "get_visual_evidence",
    ),
    "ground_visual_facts_to_sources": (
        "codenib.wiki.media_grounding",
        "ground_visual_facts_to_sources",
    ),
    "merge_incremental_visual_facts": (
        "codenib.wiki.media_incremental",
        "merge_incremental_visual_facts",
    ),
    "multimodal_tool_schemas": (
        "codenib.wiki.media_tools",
        "multimodal_tool_schemas",
    ),
    "plan_incremental_visual_fact_update": (
        "codenib.wiki.media_incremental",
        "plan_incremental_visual_fact_update",
    ),
    "load_multimodal_knowledge_bundle": (
        "codenib.wiki.media_storage",
        "load_multimodal_knowledge_bundle",
    ),
    "save_multimodal_knowledge_bundle": (
        "codenib.wiki.media_storage",
        "save_multimodal_knowledge_bundle",
    ),
    "search_visual_context": (
        "codenib.wiki.media_knowledge",
        "search_visual_context",
    ),
    "validate_multimodal_knowledge_bundle": (
        "codenib.wiki.media_storage",
        "validate_multimodal_knowledge_bundle",
    ),
    "visual_fact_extractor_from_config": (
        "codenib.wiki.media_vlm",
        "visual_fact_extractor_from_config",
    ),
}

__all__ = [
    "MULTIMODAL_KNOWLEDGE_BUNDLE_SCHEMA",
    "MULTIMODAL_KNOWLEDGE_BUNDLE_VERSION",
    "MultimodalKnowledgeToolRouter",
    "WikiBuilder",
    "OpenAICompatibleVisualFactExtractor",
    "VisualGroundingScorer",
    "build_media_evidence_pack",
    "build_multimodal_knowledge_view",
    "build_multimodal_knowledge_bundle",
    "build_multimodal_repository_knowledge",
    "build_visual_facts_manifest",
    "deterministic_visual_facts",
    "diff_media_manifests",
    "discover_media_manifest",
    "discover_source_symbol_candidates",
    "evaluate_mmwiki_predictions",
    "evaluate_visual_code_grounding",
    "evaluate_visual_fact_extraction",
    "find_visual_code_links",
    "get_visual_evidence",
    "ground_visual_facts_to_sources",
    "merge_incremental_visual_facts",
    "multimodal_tool_schemas",
    "plan_incremental_visual_fact_update",
    "load_multimodal_knowledge_bundle",
    "save_multimodal_knowledge_bundle",
    "search_visual_context",
    "validate_multimodal_knowledge_bundle",
    "visual_fact_extractor_from_config",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
