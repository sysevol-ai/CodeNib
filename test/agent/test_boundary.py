# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the agent-boundary line-numbering helpers (issue #153).

Internals are 0-based; the LLM sees 1-based. These tests pin the single
conversion site each direction, the round-trip identity, and the wiring
into ``AgentRunner`` (output via ``_serialize_result``, input via
``is_line_number`` skill inputs).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from codeminer.agent.boundary import (
    AGENT_LINE_OFFSET,
    from_agent_repr,
    from_agent_repr_arg,
    is_line_bearing,
    to_agent_repr,
)
from codeminer.agent.runner import AgentRunner, _serialize_result
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.types import QueriedNode

# ---------------------------------------------------------------------------
# to_agent_repr — outward (+1)
# ---------------------------------------------------------------------------


class TestToAgentRepr:
    def test_queried_node_shifts_to_one_based(self):
        node = QueriedNode(node_name="foo", file="a.py", start_line=41, end_line=50)
        out = to_agent_repr(node)
        assert out["start_line"] == 42
        assert out["end_line"] == 51
        assert out["node_name"] == "foo"
        assert out["file"] == "a.py"

    def test_zero_based_first_line_becomes_one(self):
        node = QueriedNode(node_name="top", start_line=0, end_line=0)
        out = to_agent_repr(node)
        assert out["start_line"] == 1
        assert out["end_line"] == 1

    def test_none_lines_preserved(self):
        out = to_agent_repr(QueriedNode(node_name="x"))
        # exclude_none drops them entirely; absence is fine, never shifted.
        assert "start_line" not in out or out["start_line"] is None

    def test_dict_input(self):
        out = to_agent_repr({"start_line": 0, "end_line": 9, "score": 0.5})
        assert out["start_line"] == 1
        assert out["end_line"] == 10
        assert out["score"] == 0.5

    def test_bool_not_treated_as_int(self):
        # Defensive: a bool must never be incremented as a line number.
        out = to_agent_repr({"start_line": True, "end_line": 2})
        assert out["start_line"] is True
        assert out["end_line"] == 3


# ---------------------------------------------------------------------------
# from_agent_repr — inward (-1)
# ---------------------------------------------------------------------------


class TestFromAgentRepr:
    def test_one_based_to_zero_based(self):
        assert from_agent_repr(42) == 41
        assert from_agent_repr(1) == 0

    def test_none_passthrough(self):
        assert from_agent_repr(None) is None

    def test_malformed_zero_or_negative_clamped(self):
        # 0 and negatives are not valid 1-based lines; clamp at 0.
        assert from_agent_repr(0) == 0
        assert from_agent_repr(-5) == 0


class TestFromAgentReprArg:
    def test_scalar(self):
        assert from_agent_repr_arg(10) == 9

    def test_list_of_scalars(self):
        assert from_agent_repr_arg([1, 2, 3]) == [0, 1, 2]

    def test_list_of_range_pairs(self):
        assert from_agent_repr_arg([[10, 20], [5, 5]]) == [[9, 19], [4, 4]]

    def test_range_dicts(self):
        out = from_agent_repr_arg([{"start_line": 10, "end_line": 20}])
        assert out == [{"start_line": 9, "end_line": 19}]

    def test_non_line_shapes_passthrough(self):
        assert from_agent_repr_arg("a.py") == "a.py"
        assert from_agent_repr_arg(None) is None
        assert from_agent_repr_arg(True) is True


# ---------------------------------------------------------------------------
# Round-trip identity (the #153 acceptance test)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("internal", [0, 1, 41, 999])
    def test_seed_round_trips_to_identity(self, internal):
        node = QueriedNode(node_name="n", start_line=internal, end_line=internal)
        agent_view = to_agent_repr(node)
        back = from_agent_repr(agent_view["start_line"])
        assert back == internal

    def test_offset_is_one(self):
        assert AGENT_LINE_OFFSET == 1


# ---------------------------------------------------------------------------
# is_line_bearing
# ---------------------------------------------------------------------------


class TestIsLineBearing:
    def test_queried_node(self):
        assert is_line_bearing(QueriedNode(node_name="x", start_line=0))

    def test_dict_with_line(self):
        assert is_line_bearing({"start_line": 3})
        assert is_line_bearing({"end_line": 3})

    def test_non_line(self):
        assert not is_line_bearing({"score": 1.0})
        assert not is_line_bearing("just a string")
        assert not is_line_bearing(42)


# ---------------------------------------------------------------------------
# _serialize_result — output boundary integration
# ---------------------------------------------------------------------------


class TestSerializeResultBoundary:
    def test_list_of_nodes_is_one_based(self):
        results = [
            QueriedNode(node_name="a", file="a.py", start_line=0, end_line=4),
            QueriedNode(node_name="b", file="b.py", start_line=10, end_line=12),
        ]
        payload = json.loads(_serialize_result(results))
        assert payload[0]["start_line"] == 1
        assert payload[0]["end_line"] == 5
        assert payload[1]["start_line"] == 11
        assert payload[1]["end_line"] == 13

    def test_single_node_is_one_based(self):
        payload = json.loads(
            _serialize_result(QueriedNode(node_name="a", start_line=7, end_line=9))
        )
        assert payload["start_line"] == 8
        assert payload["end_line"] == 10

    def test_string_result_untouched(self):
        assert _serialize_result("plain text") == "plain text"

    def test_non_line_dicts_untouched(self):
        payload = json.loads(_serialize_result([{"score": 0.9, "name": "x"}]))
        assert payload == [{"score": 0.9, "name": "x"}]


# ---------------------------------------------------------------------------
# _apply_input_boundary — input boundary integration
# ---------------------------------------------------------------------------


def _runner() -> AgentRunner:
    return AgentRunner(llm=MagicMock(spec=LiteLLMChat), registry=SkillRegistry())


def _meta_with_line_input(name: str = "ranges") -> SkillMetadata:
    return SkillMetadata(
        skill_id="line_skill",
        skill_type=SkillType.EXPAND,
        inputs=[
            SkillInputSpec(name=name, type_hint="Any", is_line_number=True),
            SkillInputSpec(name="top_k", type_hint="int", required=False),
        ],
        outputs=SkillOutputSpec(type_hint="Any"),
    )


class TestInputBoundary:
    def test_marked_input_converted_to_zero_based(self):
        runner = _runner()
        meta = _meta_with_line_input("ranges")
        out = runner._apply_input_boundary(meta, {"ranges": [[10, 20]], "top_k": 5})
        assert out["ranges"] == [[9, 19]]
        # Non-line args untouched.
        assert out["top_k"] == 5

    def test_no_line_inputs_is_noop_same_object_contents(self):
        runner = _runner()
        meta = SkillMetadata(
            skill_id="plain",
            skill_type=SkillType.RETRIEVAL,
            inputs=[SkillInputSpec(name="query", type_hint="str")],
            outputs=SkillOutputSpec(type_hint="Any"),
        )
        args = {"query": "find foo", "top_k": 20}
        assert runner._apply_input_boundary(meta, args) == args

    def test_scalar_line_input(self):
        runner = _runner()
        meta = _meta_with_line_input("line")
        out = runner._apply_input_boundary(meta, {"line": 42})
        assert out["line"] == 41


# ---------------------------------------------------------------------------
# SkillInputSpec.is_line_number contract
# ---------------------------------------------------------------------------


class TestSkillInputSpecField:
    def test_defaults_false(self):
        assert SkillInputSpec(name="q", type_hint="str").is_line_number is False

    def test_serialized_in_to_dict(self):
        meta = SkillMetadata(
            skill_id="s",
            skill_type=SkillType.EXPAND,
            inputs=[
                SkillInputSpec(name="ranges", type_hint="Any", is_line_number=True)
            ],
            outputs=SkillOutputSpec(type_hint="Any"),
        )
        d = meta.to_dict()
        assert d["inputs"][0]["is_line_number"] is True
