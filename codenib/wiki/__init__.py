# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Index-derived wiki generation for the DeepWiki-style demo.

Produces a per-repo page tree and source-grounded page content from the
already-loaded BM25 / vector indexes — no LLM required. When an LLM is
configured it can later refine the prose, but every code anchor here resolves
to a real symbol span pulled from the indexes (no fabricated lines).
"""

from .builder import WikiBuilder
from .media_artifacts import discover_media_manifest
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
from .media_tools import MultimodalKnowledgeToolRouter, multimodal_tool_schemas
from .media_vlm import OpenAICompatibleVisualFactExtractor

__all__ = [
    "MultimodalKnowledgeToolRouter",
    "WikiBuilder",
    "OpenAICompatibleVisualFactExtractor",
    "VisualGroundingScorer",
    "build_media_evidence_pack",
    "build_multimodal_knowledge_view",
    "build_multimodal_repository_knowledge",
    "build_visual_facts_manifest",
    "deterministic_visual_facts",
    "diff_media_manifests",
    "discover_media_manifest",
    "discover_source_symbol_candidates",
    "find_visual_code_links",
    "get_visual_evidence",
    "ground_visual_facts_to_sources",
    "merge_incremental_visual_facts",
    "multimodal_tool_schemas",
    "plan_incremental_visual_fact_update",
    "search_visual_context",
]
