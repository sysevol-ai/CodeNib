# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.wiki.media_tools import (
    MULTIMODAL_TOOL_SCHEMAS,
    MultimodalKnowledgeToolRouter,
)


def _view():
    return {
        "entries": [
            {
                "artifact": {
                    "path": "docs/architecture.svg",
                    "caption": "IndexCompiler architecture",
                    "role_hint": "architecture_diagram",
                },
                "facts": {
                    "entities": [{"name": "IndexCompiler", "type": "component"}],
                    "claims": [{"text": "IndexCompiler writes to VectorStore."}],
                },
                "bindings": [
                    {
                        "artifact_path": "docs/architecture.svg",
                        "entity_name": "IndexCompiler",
                        "source_path": "codenib/compiler/index_compiler.py",
                        "symbol": "IndexCompiler",
                        "kind": "symbol",
                        "line": 42,
                        "score": 1.0,
                        "evidence": "exact symbol match",
                    }
                ],
                "search_text": (
                    "docs/architecture.svg IndexCompiler architecture "
                    "codenib/compiler/index_compiler.py"
                ),
            }
        ]
    }


def test_multimodal_tool_schemas_are_exposed():
    names = {schema["name"] for schema in MULTIMODAL_TOOL_SCHEMAS}

    assert names == {
        "search_visual_context",
        "get_visual_evidence",
        "find_visual_code_links",
    }


def test_tool_router_searches_visual_context():
    router = MultimodalKnowledgeToolRouter(_view())

    result = router.call_tool(
        "search_visual_context",
        {"query": "IndexCompiler", "limit": 1},
    )

    assert result["results"][0]["artifact_path"] == "docs/architecture.svg"


def test_tool_router_gets_visual_evidence():
    router = MultimodalKnowledgeToolRouter(_view())

    result = router.call_tool(
        "get_visual_evidence",
        {"artifact_path": "docs/architecture.svg"},
    )

    assert result["evidence"]["artifact"]["caption"] == "IndexCompiler architecture"


def test_tool_router_finds_visual_code_links():
    router = MultimodalKnowledgeToolRouter(_view())

    result = router.call_tool(
        "find_visual_code_links",
        {
            "source_path": "codenib/compiler/index_compiler.py",
            "symbol": "IndexCompiler",
        },
    )

    assert result["links"][0]["binding"]["line"] == 42


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("unknown", {}, "unknown"),
        ("search_visual_context", {"query": ""}, "query"),
        ("search_visual_context", {"query": "x", "limit": 100}, "limit"),
        ("get_visual_evidence", {"artifact_path": "bad\npath"}, "control"),
        ("find_visual_code_links", {"source_path": ""}, "source_path"),
    ],
)
def test_tool_router_validates_inputs(name, arguments, message):
    router = MultimodalKnowledgeToolRouter(_view())

    with pytest.raises(ValueError, match=message):
        router.call_tool(name, arguments)
