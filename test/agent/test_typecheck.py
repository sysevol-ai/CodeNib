# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the skill type layer: arg coercion + dataflow type checking.

Covers the runtime input-validation (``coerce_args``) and the output-type
dataflow check (``check_pipeline_coherent``) that make the previously-dead
``type_hint`` / ``output_type_hint`` metadata load-bearing.
"""

from __future__ import annotations

from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.agent.skills.typecheck import (
    check_pipeline_coherent,
    coerce_args,
    parse_type_hint,
    types_compatible,
)

# --------------------------------------------------------------------------
# parse_type_hint / types_compatible
# --------------------------------------------------------------------------


def test_parse_type_hint_kinds():
    assert parse_type_hint("str").kind == "scalar"
    assert parse_type_hint("").kind == "any"
    assert parse_type_hint("Any").kind == "any"
    assert parse_type_hint("QueriedNode").kind == "nominal"
    lst = parse_type_hint("List[QueriedNode]")
    assert lst.kind == "list" and lst.inner.kind == "nominal"
    nested = parse_type_hint("List[List[QueriedNode]]")
    assert nested.kind == "list" and nested.inner.kind == "list"
    d = parse_type_hint("Dict[str, Any]")
    assert d.kind == "dict" and d.inner.kind == "any"


def test_types_compatible_matches_and_rejects():
    qn = parse_type_hint("List[QueriedNode]")
    assert types_compatible(qn, qn)
    # a producer List[X] satisfies a List[List[X]] consumer (hybrid fusion)
    assert types_compatible(qn, parse_type_hint("List[List[QueriedNode]]"))
    # scalar mismatch
    assert not types_compatible(parse_type_hint("int"), parse_type_hint("str"))
    # Any matches either direction
    assert types_compatible(parse_type_hint("Any"), qn)
    assert types_compatible(qn, parse_type_hint("Any"))
    # a scalar producer does NOT satisfy a list consumer
    assert not types_compatible(parse_type_hint("int"), qn)


# --------------------------------------------------------------------------
# coerce_args
# --------------------------------------------------------------------------

_SPECS = [
    SkillInputSpec(name="query", type_hint="str", required=True),
    SkillInputSpec(name="top_k", type_hint="int", required=False, default=20),
    SkillInputSpec(name="names_only", type_hint="bool", required=False, default=False),
]


def test_coerce_scalars_from_strings():
    out, err = coerce_args(_SPECS, {"query": "x", "top_k": "10", "names_only": "true"})
    assert err is None
    assert out["top_k"] == 10 and out["names_only"] is True


def test_coerce_int_truncates_float():
    # A stray fractional float degrades to int (matches prior int(...) behaviour)
    out, err = coerce_args(_SPECS, {"query": "x", "top_k": 12.5})
    assert err is None and out["top_k"] == 12
    out, err = coerce_args(_SPECS, {"query": "x", "top_k": "12.5"})
    assert err is None and out["top_k"] == 12


def test_coerce_bad_int_reports_error():
    out, err = coerce_args(_SPECS, {"query": "x", "top_k": "abc"})
    assert err is not None and "top_k" in err


def test_required_missing_key_errors():
    _out, err = coerce_args(_SPECS, {"top_k": 5})
    assert err is not None and "query" in err


def test_required_explicit_null_errors():
    # An explicit JSON null for a required param counts as missing.
    _out, err = coerce_args(_SPECS, {"query": None})
    assert err is not None and "query" in err


def test_required_check_ignores_non_none_default():
    # required=True with a falsy-but-non-None default must still be enforced.
    specs = [
        SkillInputSpec(name="cands", type_hint="List[str]", required=True, default=[])
    ]
    _out, err = coerce_args(specs, {})
    assert err is not None and "cands" in err


def test_nominal_and_unknown_args_pass_through():
    specs = [
        SkillInputSpec(name="candidates", type_hint="List[QueriedNode]", required=True)
    ]
    payload = [{"node_name": "a"}, {"node_name": "b"}]
    out, err = coerce_args(specs, {"candidates": payload, "extra": 1})
    assert err is None
    assert out["candidates"] == payload  # nominal element type untouched
    assert out["extra"] == 1  # unknown arg passed through (executors use **kwargs)


# --------------------------------------------------------------------------
# check_pipeline_coherent (output_type_hint dataflow)
# --------------------------------------------------------------------------


def _meta(skill_id, skill_type, inputs, output):
    return SkillMetadata(
        skill_id=skill_id,
        skill_type=skill_type,
        inputs=inputs,
        outputs=SkillOutputSpec(type_hint=output),
    )


def test_pipeline_coherent_flags_orphan_consumer():
    SkillRegistry.reset()
    reg = SkillRegistry()
    reg.register(
        _meta(
            "bm25_search",
            SkillType.RETRIEVAL,
            [SkillInputSpec(name="query", type_hint="str")],
            "List[QueriedNode]",
        )
    )
    reg.register(
        _meta(
            "llm_rerank",
            SkillType.RERANK,
            [SkillInputSpec(name="candidates", type_hint="List[QueriedNode]")],
            "List[QueriedNode]",
        )
    )
    # rerank alone has no producer -> flagged
    assert check_pipeline_coherent(["llm_rerank"], reg)
    # paired with a producer -> coherent
    assert check_pipeline_coherent(["bm25_search", "llm_rerank"], reg) == []
    SkillRegistry.reset()
