# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

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

    assert bundle["schema"] == "codenib.multimodal-knowledge-bundle.v1"
    assert bundle["schema_version"] == 1
    assert len(bundle["bundle_sha256"]) == 64
    assert bundle["media_manifest"]["artifact_count"] == 1
    assert bundle["visual_facts_manifest"]["fact_count"] == 1
    assert bundle["source_candidate_count"] >= 1
    assert bundle["knowledge_view"]["entry_count"] == 1


def test_pipeline_forwards_custom_grounding_scorer(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "diagram.svg").write_text(
        "<svg>DiagramBox</svg>",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "![DiagramBox](docs/diagram.svg)",
        encoding="utf-8",
    )
    (tmp_path / "src" / "wiki.py").write_text(
        "class WikiService: pass",
        encoding="utf-8",
    )

    def scorer(entity, candidate):
        if entity["name"] == "DiagramBox" and candidate["symbol"] == "WikiService":
            return {"score": 0.91, "evidence": "pipeline scorer"}
        return None

    bundle = build_multimodal_repository_knowledge(tmp_path, scorer=scorer)

    assert any(
        binding["symbol"] == "WikiService"
        and binding["score"] == 0.91
        and binding["evidence"] == "pipeline scorer"
        for binding in bundle["grounding_manifest"]["bindings"]
    )


def test_pipeline_materializes_exclude_roots_for_each_stage(tmp_path):
    (tmp_path / "docs").mkdir()
    excluded = tmp_path / "generated"
    excluded.mkdir()
    (tmp_path / "docs" / "diagram.svg").write_text(
        "<svg>HiddenService</svg>",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "![HiddenService](docs/diagram.svg)",
        encoding="utf-8",
    )
    (excluded / "ignored.png").write_bytes(b"png")
    (excluded / "hidden.py").write_text(
        "class HiddenService: pass",
        encoding="utf-8",
    )

    bundle = build_multimodal_repository_knowledge(
        tmp_path,
        exclude_roots=(path for path in [excluded]),
    )

    assert bundle["media_manifest"]["artifact_count"] == 1
    assert bundle["grounding_manifest"]["binding_count"] == 0


def test_pipeline_rejects_invalid_repository_roots(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        build_multimodal_repository_knowledge(missing)

    file_path = tmp_path / "repository.txt"
    file_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="not a directory"):
        build_multimodal_repository_knowledge(file_path)
