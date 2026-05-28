# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Convert SkillMetadata to OpenAI function-calling tool schemas.

This module bridges the SkillRegistry to LLM tool-calling APIs.
``registry_to_tools`` produces the ``tools`` list that litellm
(and any OpenAI-compatible provider) accepts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .skills.core import SkillMetadata
from .skills.registry import SkillRegistry

# Map SkillInputSpec.type_hint strings to JSON Schema types.
_TYPE_MAP: Dict[str, Dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
}


def _schema_for_type(type_hint: str) -> Dict[str, Any]:
    """JSON Schema for a SkillInputSpec ``type_hint`` string.

    Handles ``List[X]`` / ``list[X]`` → ``{"type":"array","items":<X>}`` so
    list-valued params (e.g. ``graph_expand.seed_symbols: List[str]``,
    ``edge_types: List[str]``) get a real schema instead of an empty ``{}``
    that gives the model no hint how to populate them. Unknown inner or
    scalar hints fall back to ``string`` (the safest LLM-producible type),
    never an empty schema.
    """
    hint = (type_hint or "").strip()
    lowered = hint.lower()
    if lowered.startswith("list[") and hint.endswith("]"):
        inner = hint[len("list[") : -1].strip()
        return {"type": "array", "items": _schema_for_type(inner)}
    return dict(_TYPE_MAP.get(hint, {"type": "string"}))


def skill_to_tool_schema(meta: SkillMetadata) -> Dict[str, Any]:
    """Convert a single ``SkillMetadata`` to an OpenAI function tool dict."""
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for inp in meta.inputs:
        prop: Dict[str, Any] = _schema_for_type(inp.type_hint)
        if inp.description:
            prop["description"] = inp.description
        if inp.default is not None:
            prop["default"] = inp.default
        properties[inp.name] = prop

        if inp.required:
            required.append(inp.name)

    description = (meta.skill_doc or meta.description or "").strip()
    # Truncate long descriptions to avoid bloating the tool list
    if len(description) > 1024:
        description = description[:1021] + "..."

    return {
        "type": "function",
        "function": {
            "name": meta.skill_id,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        },
    }


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
