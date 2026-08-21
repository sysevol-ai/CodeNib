# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.repository_source_selection import RepositorySourceSelection
from codenib.wiki.media_grounding import (
    discover_source_symbol_candidates,
    ground_visual_facts_to_sources,
)


def test_discover_source_symbol_candidates_extracts_files_and_symbols(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "compiler.py").write_text(
        "\n".join(
            [
                "class IndexCompiler:",
                "    pass",
                "",
                "def build_vector_store():",
                "    VectorStore = object()",
            ]
        ),
        encoding="utf-8",
    )

    candidates = discover_source_symbol_candidates(tmp_path)

    assert {
        "path": "src/compiler.py",
        "symbol": "",
        "kind": "source",
        "line": 0,
    } in candidates
    symbols = {candidate["symbol"] for candidate in candidates}
    assert "IndexCompiler" in symbols
    assert "build_vector_store" in symbols
    assert "VectorStore" in symbols


def test_discover_source_symbol_candidates_respects_source_selection(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "visible.py").write_text(
        "class Visible: pass", encoding="utf-8"
    )
    (tmp_path / "hidden").mkdir()
    (tmp_path / "hidden" / "secret.py").write_text(
        "class Secret: pass", encoding="utf-8"
    )

    candidates = discover_source_symbol_candidates(
        tmp_path,
        selection=RepositorySourceSelection(["hidden"]),
    )

    assert all(not candidate["path"].startswith("hidden/") for candidate in candidates)
    assert any(candidate["symbol"] == "Visible" for candidate in candidates)


@pytest.mark.parametrize(
    ("filename", "source", "symbol"),
    [
        ("service.cpp", "class NativeService {};", "NativeService"),
        ("service.cs", "class ManagedService {}", "ManagedService"),
        ("service.rb", "class RubyService\nend", "RubyService"),
        ("service.php", "class PhpService {}", "PhpService"),
    ],
)
def test_source_symbol_inventory_uses_registered_language_extensions(
    tmp_path,
    filename,
    source,
    symbol,
):
    (tmp_path / filename).write_text(source, encoding="utf-8")

    candidates = discover_source_symbol_candidates(tmp_path)

    assert any(candidate["symbol"] == symbol for candidate in candidates)


def test_ground_visual_facts_to_sources_binds_entities_to_symbols():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "docs/assets/architecture.svg",
                "entities": [
                    {
                        "name": "IndexCompiler",
                        "grounding_candidates": ["IndexCompiler"],
                    },
                    {
                        "name": "Vector Store",
                        "grounding_candidates": ["VectorStore"],
                    },
                ],
            }
        ],
    }
    source_candidates = [
        {
            "path": "codenib/compiler/index_compiler.py",
            "symbol": "IndexCompiler",
            "kind": "symbol",
            "line": 42,
        },
        {
            "path": "codenib/index/embedding/vector_store.py",
            "symbol": "VectorStore",
            "kind": "symbol",
            "line": 10,
        },
    ]

    manifest = ground_visual_facts_to_sources(visual_facts, source_candidates)

    assert manifest["schema"] == "codenib.media-grounding.v1"
    assert manifest["visual_facts_manifest_sha256"] == "visual-facts-hash"
    bindings = manifest["bindings"]
    assert {
        "artifact_path": "docs/assets/architecture.svg",
        "entity_name": "IndexCompiler",
        "source_path": "codenib/compiler/index_compiler.py",
        "symbol": "IndexCompiler",
        "kind": "symbol",
        "line": 42,
        "score": 1.0,
        "evidence": "exact symbol match",
    } in bindings
    assert any(binding["entity_name"] == "Vector Store" for binding in bindings)
    assert manifest["manifest_sha256"]


def test_ground_visual_facts_to_sources_prefers_exact_symbol_match():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [{"name": "WikiService"}],
            }
        ],
    }
    manifest = ground_visual_facts_to_sources(
        visual_facts,
        [
            {
                "path": "codenib/wiki/wiki_service.py",
                "symbol": "",
                "kind": "source",
                "line": 0,
            },
            {
                "path": "codenib/wiki/service.py",
                "symbol": "WikiService",
                "kind": "symbol",
                "line": 17,
            },
        ],
    )

    assert manifest["bindings"][0]["symbol"] == "WikiService"
    assert manifest["bindings"][0]["score"] == 1.0


def test_ground_visual_facts_to_sources_deduplicates_bindings():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [
                    {
                        "name": "WikiService",
                        "grounding_candidates": ["WikiService", "WikiService"],
                    }
                ],
            }
        ],
    }
    source = {
        "path": "codenib/wiki/service.py",
        "symbol": "WikiService",
        "kind": "symbol",
        "line": 17,
    }

    manifest = ground_visual_facts_to_sources(visual_facts, [source, source])

    assert len(manifest["bindings"]) == 1


def test_grounding_deduplicates_candidates_before_applying_result_limit():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [{"name": "WikiService"}],
            }
        ],
    }
    exact = {
        "path": "src/wiki.py",
        "symbol": "WikiService",
        "kind": "symbol",
        "line": 3,
    }
    partial = {
        "path": "src/wiki_service.py",
        "symbol": "WikiServiceAdapter",
        "kind": "symbol",
        "line": 7,
    }

    manifest = ground_visual_facts_to_sources(
        visual_facts,
        [exact, exact, partial],
        max_bindings_per_entity=2,
    )

    assert [binding["symbol"] for binding in manifest["bindings"]] == [
        "WikiService",
        "WikiServiceAdapter",
    ]


def test_ground_visual_facts_to_sources_drops_unsafe_external_paths():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "/tmp/leak.svg",
                "entities": [{"name": "Unsafe"}],
            },
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [
                    {
                        "name": "WikiService\nwith control",
                        "grounding_candidates": ["WikiService"],
                    }
                ],
            },
        ],
    }
    source_candidates = [
        {
            "path": "/tmp/leak.py",
            "symbol": "WikiService",
            "kind": "symbol",
            "line": 1,
        },
        {
            "path": "../secret.py",
            "symbol": "WikiService",
            "kind": "symbol",
            "line": 2,
        },
        {
            "path": "src/wiki.py",
            "symbol": "WikiService",
            "kind": "symbol",
            "line": 3,
        },
    ]

    manifest = ground_visual_facts_to_sources(visual_facts, source_candidates)

    assert manifest["binding_count"] == 1
    assert manifest["bindings"][0]["artifact_path"] == "docs/architecture.svg"
    assert manifest["bindings"][0]["source_path"] == "src/wiki.py"
    assert manifest["bindings"][0]["entity_name"] == "WikiServicewith control"


def test_grounding_rejects_limits_above_contract_maximum(tmp_path):
    with pytest.raises(ValueError, match="max_candidates"):
        discover_source_symbol_candidates(tmp_path, max_candidates=8193)
    with pytest.raises(ValueError, match="max_bindings_per_entity"):
        ground_visual_facts_to_sources({}, (), max_bindings_per_entity=6)


def test_ground_visual_facts_to_sources_accepts_custom_scorer():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [{"name": "DiagramBox"}],
            }
        ],
    }

    def scorer(entity, candidate):
        if entity["name"] == "DiagramBox" and candidate["symbol"] == "WikiService":
            return {
                "score": 0.88,
                "evidence": "graph scorer match",
                "source_path": "../../forged.py",
            }
        return None

    manifest = ground_visual_facts_to_sources(
        visual_facts,
        [
            {
                "path": "codenib/wiki/service.py",
                "symbol": "WikiService",
                "kind": "symbol",
                "line": 17,
            }
        ],
        scorer=scorer,
    )

    assert manifest["bindings"] == [
        {
            "artifact_path": "docs/architecture.svg",
            "entity_name": "DiagramBox",
            "source_path": "codenib/wiki/service.py",
            "symbol": "WikiService",
            "kind": "symbol",
            "line": 17,
            "score": 0.88,
            "evidence": "graph scorer match",
        }
    ]


def test_grounding_rejects_invalid_or_nonfinite_custom_scores():
    visual_facts = {
        "manifest_sha256": "visual-facts-hash",
        "facts": [
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [{"name": "DiagramBox"}],
            }
        ],
    }
    candidates = [
        {
            "path": "codenib/wiki/service.py",
            "symbol": "WikiService",
            "kind": "symbol",
            "line": 17,
        }
    ]

    with pytest.raises(ValueError, match="scorer must be callable"):
        ground_visual_facts_to_sources(visual_facts, candidates, scorer="invalid")

    manifest = ground_visual_facts_to_sources(
        visual_facts,
        candidates,
        scorer=lambda entity, candidate: {"score": float("inf")},
    )

    assert manifest["binding_count"] == 0
