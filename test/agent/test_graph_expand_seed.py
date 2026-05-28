# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for model-seedable graph_expand + List[...] tool schemas (#133)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
                name="seed_symbols",
                type_hint="List[str]",
                required=True,
                description="qualified names",
            )
        ],
        outputs=SkillOutputSpec(type_hint="List[QueriedNode]"),
        executor_fn=lambda **k: [],
    )
    schema = skill_to_tool_schema(meta)
    prop = schema["function"]["parameters"]["properties"]["seed_symbols"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string"}
    assert schema["function"]["parameters"]["required"] == ["seed_symbols"]


# ---------------------------------------------------------------------------
# graph_expand executor: informative errors instead of silent []
# ---------------------------------------------------------------------------


def _load_executor():
    # Import via the package path so the executor's relative imports resolve.
    from codeminer.agent.skills.graph_expand.executor import create_executor

    return create_executor


def test_graph_expand_raises_on_no_seeds():
    create_executor = _load_executor()
    ctx = SimpleNamespace(code_graph=SimpleNamespace(name_to_vertex={"a": 0}))
    execute = create_executor(ctx)
    with pytest.raises(ValueError, match="seed_symbols"):
        execute(seed_symbols=[])


def test_graph_expand_raises_when_all_seeds_unresolved():
    create_executor = _load_executor()
    ctx = SimpleNamespace(code_graph=SimpleNamespace(name_to_vertex={"real.sym": 0}))
    execute = create_executor(ctx)
    with pytest.raises(ValueError, match="resolve"):
        execute(seed_symbols=["nonexistent.symbol"])


def test_symbols_in_files_resolves_via_file_nodes():
    from codeminer.agent.skills.graph_expand.executor import _symbols_in_files

    class _VS:
        def __init__(self, names):
            self._n = names

        def __getitem__(self, vid):
            return {"name": self._n[vid]}

    class _Graph:
        def __init__(self, names):
            self.vs = _VS(names)

    class _CG:
        def __init__(self):
            self.graph = _Graph({0: "pkg.adminHandler", 1: "pkg.deleteConfig"})
            self._file_nodes = {"admin.go": [(1, 10, 0), (12, 20, 1)]}

    cg = _CG()
    # exact key
    assert _symbols_in_files(cg, ["admin.go"]) == [
        "pkg.adminHandler",
        "pkg.deleteConfig",
    ]
    # suffix / absolute-ish path still matches
    assert _symbols_in_files(cg, ["/repo/admin.go"]) == [
        "pkg.adminHandler",
        "pkg.deleteConfig",
    ]
    # unknown file → nothing
    assert _symbols_in_files(cg, ["nope.go"]) == []


def test_graph_expand_accepts_legacy_seed_nodes_shim():
    """Back-compat: objects with .node_name still work via seed_nodes."""
    create_executor = _load_executor()
    ctx = SimpleNamespace(code_graph=SimpleNamespace(name_to_vertex={"x.y": 0}))
    execute = create_executor(ctx)
    # All unresolved (node_name not in graph) → informative raise, proving the
    # shim extracted the name from the object rather than silently dropping it.
    with pytest.raises(ValueError, match="resolve"):
        execute(seed_nodes=[SimpleNamespace(node_name="ghost.sym")])
