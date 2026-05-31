# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Convert skills and tools to OpenAI function-calling tool schemas.

This module bridges the ``SkillRegistry`` (index-backed retrieval skills) and
the ``ToolRegistry`` (always-on filesystem primitives) to LLM tool-calling
APIs. ``registry_to_tools`` / ``tools_to_schemas`` produce the ``tools`` list
that litellm (and any OpenAI-compatible provider) accepts. Both kinds share one
schema emitter (:func:`_function_schema`) since their input specs have the same
surface (``name`` / ``type_hint`` / ``required`` / ``default`` / ``description``
/ ``enum``).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from .skills.core import SkillMetadata
from .skills.registry import SkillRegistry
from .tools.spec import ToolRegistry, ToolSpec

# Map ``type_hint`` strings to JSON Schema types.
_TYPE_MAP: Dict[str, Dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
}


def _schema_for_type(type_hint: str) -> Dict[str, Any]:
    """JSON Schema for a ``type_hint`` string.

    Handles ``List[X]`` / ``list[X]`` → ``{"type":"array","items":<X>}`` so
    list-valued params get a real schema instead of an empty ``{}`` that gives
    the model no hint how to populate them. Unknown inner or scalar hints fall
    back to ``string`` (the safest LLM-producible type), never an empty schema.
    """
    hint = (type_hint or "").strip()
    lowered = hint.lower()
    if lowered.startswith("list[") and hint.endswith("]"):
        inner = hint[len("list[") : -1].strip()
        return {"type": "array", "items": _schema_for_type(inner)}
    return dict(_TYPE_MAP.get(hint, {"type": "string"}))


def _function_schema(
    name: str, description: str, inputs: Iterable[Any]
) -> Dict[str, Any]:
    """Build an OpenAI function tool dict from a name, doc, and input specs.

    ``inputs`` is any iterable of objects exposing ``name`` / ``type_hint`` /
    ``required`` / ``default`` / ``description`` / ``enum`` — i.e. either
    ``SkillInputSpec`` or ``ToolInputSpec``.
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for inp in inputs:
        prop: Dict[str, Any] = _schema_for_type(inp.type_hint)
        if inp.description:
            prop["description"] = inp.description
        if getattr(inp, "enum", None):
            prop["enum"] = list(inp.enum)
        if inp.default is not None:
            prop["default"] = inp.default
        properties[inp.name] = prop

        if inp.required:
            required.append(inp.name)

    description = (description or "").strip()
    # Truncate long descriptions to avoid bloating the tool list.
    if len(description) > 1024:
        description = description[:1021] + "..."

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        },
    }


def skill_to_tool_schema(meta: SkillMetadata) -> Dict[str, Any]:
    """Convert a single ``SkillMetadata`` to an OpenAI function tool dict."""
    return _function_schema(
        meta.skill_id, meta.skill_doc or meta.description or "", meta.inputs
    )


def tool_to_schema(spec: ToolSpec) -> Dict[str, Any]:
    """Convert a single ``ToolSpec`` to an OpenAI function tool dict."""
    return _function_schema(
        spec.tool_id, spec.tool_doc or spec.description or "", spec.inputs
    )


def registry_to_tools(
    registry: Optional[SkillRegistry] = None,
    *,
    allow: Optional[Set[str]] = None,
    exclude: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Convert all skills in the registry to OpenAI tool schemas.

    Args:
        registry: Registry to read from; defaults to the singleton.
        allow: If provided, only skills in this set are included (allowlist,
            applied first). If ``None``, all registered skills are eligible.
        exclude: Skill IDs to skip (denylist, applied after ``allow``).

    Returns:
        List of tool dicts ready for ``litellm.completion(tools=...)``.
    """
    reg = registry or SkillRegistry()
    exclude = exclude or set()
    tools: List[Dict[str, Any]] = []

    for skill_id, meta in reg.list_skills().items():
        if allow is not None and skill_id not in allow:
            continue
        if skill_id in exclude:
            continue
        tools.append(skill_to_tool_schema(meta))

    return tools


def tools_to_schemas(
    tool_registry: ToolRegistry,
    *,
    allow: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Convert default tools to OpenAI schemas.

    Args:
        tool_registry: The runner's :class:`ToolRegistry`.
        allow: If provided, only tools whose id is in this set are emitted
            (the runner passes the resolved ``default_tool_ids`` subset). If
            ``None``, no tool is emitted — tools are opt-in via this set, never
            implicitly exposed.
    """
    if not allow:
        return []
    return [
        tool_to_schema(spec)
        for tool_id, spec in tool_registry.list_tools().items()
        if tool_id in allow
    ]
