# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Skill registry and ``@skill`` decorator.

The decorator captures pure metadata and registers it in a global registry.
The decorated function itself is the execution logic — untouched by the
compiler, called only by the scheduler at runtime.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional

from .core import Cost, SkillInputSpec, SkillMetadata, SkillOutputSpec, SkillType


class SkillRegistry:
    """
    Global catalogue of skill metadata.

    Implemented as an explicit singleton so that the ``@skill`` decorator and the
    compiler always share the same instance.
    """

    _instance: Optional["SkillRegistry"] = None
    _skills: Dict[str, SkillMetadata]

    def __new__(cls) -> "SkillRegistry":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._skills = {}
            cls._instance = inst
        return cls._instance

    # -- mutation --

    def register(self, metadata: SkillMetadata) -> None:
        if metadata.skill_id in self._skills:
            raise ValueError(f"Skill {metadata.skill_id!r} already registered")
        self._skills[metadata.skill_id] = metadata

    # -- queries --

    def get(self, skill_id: str) -> Optional[SkillMetadata]:
        return self._skills.get(skill_id)

    def list_skills(self) -> Dict[str, SkillMetadata]:
        return dict(self._skills)

    def has(self, skill_id: str) -> bool:
        return skill_id in self._skills

    # -- testing helpers --

    @classmethod
    def reset(cls) -> None:
        """Tear down the singleton — only for unit tests."""
        if cls._instance is not None:
            cls._instance._skills.clear()
            cls._instance = None


# ---------------------------------------------------------------------------
# @skill decorator
# ---------------------------------------------------------------------------


def skill(
    skill_id: str,
    skill_type: SkillType,
    inputs: Optional[List[SkillInputSpec]] = None,
    outputs: Optional[SkillOutputSpec] = None,
    *,
    dependencies: Optional[List[str]] = None,
    resources: Optional[List[str]] = None,
    cost: Cost = Cost.MEDIUM,
    operator: Optional[str] = None,
    async_capable: bool = False,
    cacheable: bool = True,
    defaults: Optional[Dict[str, Any]] = None,
    index_requirements: Optional[List[Any]] = None,
    skill_doc: str = "",
    description: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Register a function as a skill.

    Usage::

        @skill(
            skill_id="embedding_search",
            skill_type=SkillType.RETRIEVAL,
            inputs=[SkillInputSpec(name="query", type_hint="str")],
            outputs=SkillOutputSpec(type_hint="List[QueriedNode]"),
            operator="faiss.retrieve",
            cost=Cost.HIGH,
        )
        def embedding_search(query: str, top_k: int = 10) -> List[QueriedNode]:
            ...

    The decorator only stores metadata — the function body is the execution
    logic invoked by the scheduler.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _async = async_capable or inspect.iscoroutinefunction(fn)

        metadata = SkillMetadata(
            skill_id=skill_id,
            skill_type=skill_type,
            inputs=inputs or [],
            outputs=outputs or SkillOutputSpec(type_hint="Any"),
            executor_fn=fn,
            async_capable=_async,
            cacheable=cacheable,
            cost=cost,
            dependencies=dependencies or [],
            resources=resources or [],
            operator=operator,
            defaults=defaults or {},
            index_requirements=index_requirements or [],
            skill_doc=skill_doc,
            description=description or fn.__doc__ or "",
        )

        SkillRegistry().register(metadata)

        # Attach metadata to the function for introspection.
        fn._skill_metadata = metadata  # type: ignore[attr-defined]
        return fn

    return decorator
