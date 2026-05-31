# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tool primitives — a type distinct from retrieval *skills*.

The always-on filesystem primitives (``read`` / ``grep`` / ``glob`` / ``bash``)
used to masquerade as :class:`~codeminer.agent.skills.core.SkillMetadata`
objects tagged ``SkillType.CUSTOM``. That was a category error: a
``SkillMetadata`` is a *retrieval-operator* descriptor — it carries
``skill_type``, ``operator`` (the physical-operator value the compiler/bridge
consumes), ``index_requirements`` and ``dependencies``. The default tools have
none of those; they scan the raw filesystem and return strings. Forcing them
through ``SkillMetadata`` meant every compiler-facing field was a no-op, and a
``_GLOB_SKILL_DOC`` was really a *tool* doc.

This module gives tools their own small type and registry:

* :class:`ToolInputSpec` — one input parameter (same surface as
  ``SkillInputSpec`` so the shared schema emitter handles both).
* :class:`ToolSpec` — an always-on primitive: an id, a doc, typed inputs, an
  executor, and a (scalar) output type. No ``operator``/``index_requirements``/
  ``skill_type``.
* :class:`ToolRegistry` — a plain (non-singleton) catalogue the
  :class:`~codeminer.agent.runner.AgentRunner` holds alongside its
  ``SkillRegistry``. Tools and skills are two concepts; the runner exposes both
  to the model and dispatches over both, but they never share a namespace's
  lifecycle (skills are reset per query; tools are static).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass(slots=True)
class ToolInputSpec:
    """Type specification for a single tool input.

    Mirrors ``SkillInputSpec`` so ``tool_schema`` can emit a JSON-Schema for
    skills and tools with one code path.
    """

    name: str
    type_hint: str  # "str" / "int" / "bool" / "List[str]"
    required: bool = True
    default: Any = None
    description: str = ""
    # Closed value set -> JSON-Schema ``enum`` (e.g. grep's ``output_mode``).
    enum: Optional[List[str]] = None


@dataclass(slots=True)
class ToolSpec:
    """An always-on filesystem/shell primitive exposed to the agent.

    Unlike a skill, a tool has no index dependency, no physical operator, and
    no place in the retrieval-pipeline taxonomy — it is a primitive the model
    is pretrained on. Its output is a scalar string, not a node list.
    """

    tool_id: str
    executor_fn: Callable[..., Any]
    inputs: List[ToolInputSpec] = field(default_factory=list)
    tool_doc: str = ""  # agent-readable markdown (the per-tool description)
    output_type_hint: str = "str"
    defaults: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.tool_id:
            raise ValueError("tool_id is required")


class ToolRegistry:
    """Catalogue of :class:`ToolSpec` objects, separate from the skill registry.

    A plain instance (not a singleton): the runner builds one per construction
    and populates it with the default tools. Mirrors the small query surface of
    ``SkillRegistry`` (``register`` / ``get`` / ``list_tools`` / ``has``) so the
    runner can treat the two registries uniformly where it needs to.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.tool_id in self._tools:
            raise ValueError(f"Tool {spec.tool_id!r} already registered")
        self._tools[spec.tool_id] = spec

    def get(self, tool_id: str) -> Optional[ToolSpec]:
        return self._tools.get(tool_id)

    def list_tools(self) -> Dict[str, ToolSpec]:
        return dict(self._tools)

    def has(self, tool_id: str) -> bool:
        return tool_id in self._tools


__all__ = ["ToolInputSpec", "ToolSpec", "ToolRegistry"]
