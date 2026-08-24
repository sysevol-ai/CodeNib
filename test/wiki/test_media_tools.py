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


def test_tool_schema_copies_do_not_mutate_the_public_contract():
    router = MultimodalKnowledgeToolRouter(_view())
    schemas = router.tool_schemas()

    assert isinstance(schemas, list)
    schemas[0]["input_schema"]["properties"]["query"]["type"] = "integer"

    assert (
        MULTIMODAL_TOOL_SCHEMAS[0]["input_schema"]["properties"]["query"]["type"]
        == "string"
    )


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
            "limit": 1,
        },
    )

    assert result["links"][0]["binding"]["line"] == 42


def test_tool_router_limits_visual_code_links():
    view = _view()
    second = dict(view["entries"][0])
    second["artifact"] = {
        **second["artifact"],
        "path": "docs/second-architecture.svg",
    }
    view["entries"].append(second)
    router = MultimodalKnowledgeToolRouter(view)

    result = router.call_tool(
        "find_visual_code_links",
        {
            "source_path": "codenib/compiler/index_compiler.py",
            "limit": 1,
        },
    )

    assert len(result["links"]) == 1


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("unknown", {}, "unknown"),
        ("search_visual_context", {"query": ""}, "query"),
        ("search_visual_context", {"query": "x", "limit": 100}, "limit"),
        ("search_visual_context", {"query": 1}, "string"),
        ("search_visual_context", {"query": "x", "limit": "1"}, "integer"),
        (
            "search_visual_context",
            {"query": "x", "unexpected": True},
            "unexpected",
        ),
        ("get_visual_evidence", {"artifact_path": "bad\npath"}, "control"),
        ("get_visual_evidence", {"artifact_path": "../secret.svg"}, "relative"),
        ("get_visual_evidence", {"artifact_path": "/tmp/secret.svg"}, "relative"),
        ("get_visual_evidence", {"artifact_path": "docs\\secret.svg"}, "relative"),
        ("find_visual_code_links", {"source_path": ""}, "source_path"),
        ("find_visual_code_links", {"source_path": None}, "string"),
    ],
)
def test_tool_router_validates_inputs(name, arguments, message):
    router = MultimodalKnowledgeToolRouter(_view())

    with pytest.raises(ValueError, match=message):
        router.call_tool(name, arguments)


def test_tool_router_requires_an_argument_object():
    router = MultimodalKnowledgeToolRouter(_view())

    with pytest.raises(ValueError, match="object"):
        router.call_tool("search_visual_context", [])
