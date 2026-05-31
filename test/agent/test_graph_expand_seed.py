# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for List[...] tool schemas used by graph_expand (#133).

The executor's behaviour (range / symbol input, dedup, modes) is covered in
``test/agent/test_graph_expand.py``; this module keeps the focused tool-schema
checks that guard the ``List[str]`` -> ``array`` lowering the agent relies on.
"""

from __future__ import annotations

from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.tool_schema import _schema_for_type, skill_to_tool_schema

# ---------------------------------------------------------------------------
# tool_schema: List[...] -> array, unknown -> string (never empty {})
# ---------------------------------------------------------------------------


def test_schema_for_scalar_and_list():
    assert _schema_for_type("str") == {"type": "string"}
    assert _schema_for_type("List[str]") == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert _schema_for_type("list[int]") == {
        "type": "array",
        "items": {"type": "integer"},
    }
    # Unknown inner type (e.g. QueriedNode) degrades to string, not {}.
    assert _schema_for_type("List[QueriedNode]") == {
        "type": "array",
        "items": {"type": "string"},
    }
    # Unknown scalar falls back to string, never an empty schema.
    assert _schema_for_type("WeirdType") == {"type": "string"}


def test_skill_to_tool_schema_emits_array_items():
    meta = SkillMetadata(
        skill_id="graph_expand",
        skill_type=SkillType.EXPAND,
        inputs=[
            SkillInputSpec(
                name="symbols",
                type_hint="List[str]",
                required=True,
                description="qualified names",
            )
        ],
        outputs=SkillOutputSpec(type_hint="List[QueriedNode]"),
        executor_fn=lambda **k: [],
    )
    schema = skill_to_tool_schema(meta)
    prop = schema["function"]["parameters"]["properties"]["symbols"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string"}
    assert schema["function"]["parameters"]["required"] == ["symbols"]
