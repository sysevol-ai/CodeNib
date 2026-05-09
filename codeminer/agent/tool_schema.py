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


def skill_to_tool_schema(meta: SkillMetadata) -> Dict[str, Any]:
    """Convert a single ``SkillMetadata`` to an OpenAI function tool dict."""
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for inp in meta.inputs:
        prop: Dict[str, Any] = dict(_TYPE_MAP.get(inp.type_hint, {}))
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
