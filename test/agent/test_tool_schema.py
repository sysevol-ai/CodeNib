# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for tool schema generation (codeminer.agent.tool_schema)."""

from __future__ import annotations

import pytest

from codeminer.agent.skills.core import (
    Cost,
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.agent.tool_schema import registry_to_tools, skill_to_tool_schema

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_meta():
    return SkillMetadata(
        skill_id="test_search",
        skill_type=SkillType.RETRIEVAL,
        inputs=[
            SkillInputSpec(
                name="query",
                type_hint="str",
                required=True,
                description="Search query",
            ),
            SkillInputSpec(
                name="top_k",
                type_hint="int",
                required=False,
                default=20,
                description="Max results",
            ),
            SkillInputSpec(
                name="filter_test",
                type_hint="bool",
                required=False,
                default=False,
            ),
        ],
        outputs=SkillOutputSpec(type_hint="List[QueriedNode]"),
        skill_doc="A test search skill.\nSearches code.",
        cost=Cost.LOW,
    )


# ---------------------------------------------------------------------------
# skill_to_tool_schema tests
# ---------------------------------------------------------------------------


class TestSkillToToolSchema:
    def test_basic_structure(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)

        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "test_search"
        assert "A test search skill" in func["description"]

        params = func["parameters"]
        assert params["type"] == "object"
        assert "query" in params["properties"]
        assert params["required"] == ["query"]

    def test_type_mapping_str(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)
        props = schema["function"]["parameters"]["properties"]
        assert props["query"]["type"] == "string"

    def test_type_mapping_int(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)
        props = schema["function"]["parameters"]["properties"]
        assert props["top_k"]["type"] == "integer"

    def test_type_mapping_bool(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)
        props = schema["function"]["parameters"]["properties"]
        assert props["filter_test"]["type"] == "boolean"

    def test_type_mapping_float(self):
        meta = SkillMetadata(
            skill_id="test_float",
            skill_type=SkillType.CUSTOM,
            inputs=[SkillInputSpec(name="damping", type_hint="float")],
        )
        schema = skill_to_tool_schema(meta)
        assert (
            schema["function"]["parameters"]["properties"]["damping"]["type"]
            == "number"
        )

    def test_complex_list_type_maps_to_array(self):
        """List[X] → array schema; unknown inner type degrades to string.

        Previously complex types produced an empty ``{}`` schema, giving the
        model no hint how to populate list params (e.g.
        ``graph_expand.symbols``). Now they get ``array`` + ``items``.
        """
        meta = SkillMetadata(
            skill_id="test_complex",
            skill_type=SkillType.CUSTOM,
            inputs=[SkillInputSpec(name="nodes", type_hint="List[QueriedNode]")],
        )
        schema = skill_to_tool_schema(meta)
        props = schema["function"]["parameters"]["properties"]
        assert props["nodes"] == {"type": "array", "items": {"type": "string"}}

    def test_default_values(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)
        props = schema["function"]["parameters"]["properties"]
        assert props["top_k"]["default"] == 20

    def test_description_field(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)
        props = schema["function"]["parameters"]["properties"]
        assert props["query"]["description"] == "Search query"

    def test_required_only_required_params(self, sample_meta):
        schema = skill_to_tool_schema(sample_meta)
        required = schema["function"]["parameters"]["required"]
        assert required == ["query"]
        assert "top_k" not in required
        assert "filter_test" not in required

    def test_no_required_omits_key(self):
        """If no params are required, the 'required' key should be absent."""
        meta = SkillMetadata(
            skill_id="test_optional",
            skill_type=SkillType.CUSTOM,
            inputs=[SkillInputSpec(name="x", type_hint="int", required=False)],
        )
        schema = skill_to_tool_schema(meta)
        assert "required" not in schema["function"]["parameters"]

    def test_description_fallback_to_description(self):
        meta = SkillMetadata(
            skill_id="test_fb",
            skill_type=SkillType.CUSTOM,
            description="Fallback desc",
        )
        schema = skill_to_tool_schema(meta)
        assert schema["function"]["description"] == "Fallback desc"

    def test_description_empty(self):
        meta = SkillMetadata(
            skill_id="test_empty",
            skill_type=SkillType.CUSTOM,
        )
        schema = skill_to_tool_schema(meta)
        assert schema["function"]["description"] == ""

    def test_long_description_truncated(self):
        meta = SkillMetadata(
            skill_id="test_long",
            skill_type=SkillType.CUSTOM,
            skill_doc="x" * 2000,
        )
        schema = skill_to_tool_schema(meta)
        desc = schema["function"]["description"]
        assert len(desc) <= 1024
        assert desc.endswith("...")


# ---------------------------------------------------------------------------
# registry_to_tools tests
# ---------------------------------------------------------------------------


class TestRegistryToTools:
    @pytest.fixture(autouse=True)
    def _reset_registry(self):
        SkillRegistry.reset()
        yield
        SkillRegistry.reset()

    def test_empty_registry(self):
        tools = registry_to_tools(SkillRegistry())
        assert tools == []

    def test_populated_registry(self):
        reg = SkillRegistry()
        reg.register(
            SkillMetadata(
                skill_id="skill_a",
                skill_type=SkillType.RETRIEVAL,
                inputs=[SkillInputSpec(name="q", type_hint="str", required=True)],
            )
        )
        reg.register(
            SkillMetadata(
                skill_id="skill_b",
                skill_type=SkillType.TRANSFORM,
            )
        )

        tools = registry_to_tools(reg)
        assert len(tools) == 2
        names = {t["function"]["name"] for t in tools}
        assert names == {"skill_a", "skill_b"}

    def test_exclude_skills(self):
        reg = SkillRegistry()
        reg.register(SkillMetadata(skill_id="keep", skill_type=SkillType.CUSTOM))
        reg.register(SkillMetadata(skill_id="drop", skill_type=SkillType.CUSTOM))

        tools = registry_to_tools(reg, exclude={"drop"})
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "keep"
