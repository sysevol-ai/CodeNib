# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.wiki import (
    build_multimodal_knowledge_view,
    build_multimodal_repository_knowledge,
    build_visual_facts_manifest,
    discover_media_manifest,
    discover_source_symbol_candidates,
    find_visual_code_links,
    ground_visual_facts_to_sources,
    search_visual_context,
)


def test_multimodal_repository_knowledge_pipeline(tmp_path):
    (tmp_path / "docs" / "assets").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "assets" / "architecture.svg").write_text(
        "<svg>IndexCompiler VectorStore</svg>",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "\n".join(
            [
                "# Demo",
                "The architecture diagram shows IndexCompiler and VectorStore.",
                "![IndexCompiler architecture](docs/assets/architecture.svg)",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "src" / "compiler.py").write_text(
        "\n".join(
            [
                "class IndexCompiler:",
                "    pass",
                "",
                "class VectorStore:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    media = discover_media_manifest(tmp_path, commit="abc123")
    facts = build_visual_facts_manifest(media)
    sources = discover_source_symbol_candidates(tmp_path)
    grounding = ground_visual_facts_to_sources(facts, sources)
    view = build_multimodal_knowledge_view(media, facts, grounding)

    assert media["artifact_count"] == 1
    assert facts["fact_count"] == 1
    assert grounding["binding_count"] >= 1
    assert view["entry_count"] == 1
    assert search_visual_context(view, "IndexCompiler architecture")
    assert find_visual_code_links(view, "src/compiler.py", symbol="IndexCompiler")


def test_build_multimodal_repository_knowledge_wraps_pipeline(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "diagram.png").write_bytes(b"png")
    (tmp_path / "README.md").write_text(
        "![WikiService diagram](docs/diagram.png)",
        encoding="utf-8",
    )
    (tmp_path / "src" / "wiki.py").write_text(
        "class WikiService: pass",
        encoding="utf-8",
    )

    bundle = build_multimodal_repository_knowledge(tmp_path, commit="abc123")

    assert bundle["media_manifest"]["artifact_count"] == 1
    assert bundle["visual_facts_manifest"]["fact_count"] == 1
    assert bundle["source_candidate_count"] >= 1
    assert bundle["knowledge_view"]["entry_count"] == 1
