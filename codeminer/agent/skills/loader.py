# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Skill package loader.

Scans a directory for skill packages (subdirectories containing
``config.yaml``, ``skill.md``, and ``executor.py``) and loads them
into ``SkillMetadata`` objects registered in the ``SkillRegistry``.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .context import CONTEXT_KEY_FOR_TYPE, ComposerContexts
from .core import Cost, SkillInputSpec, SkillMetadata, SkillOutputSpec, SkillType
from .registry import SkillRegistry

# Map config.yaml string values to enums.
_SKILL_TYPE_MAP = {t.value: t for t in SkillType}
_COST_MAP = {c.value: c for c in Cost}


class SkillLoader:
    """
    Load skill packages from a directory into the ``SkillRegistry``.

    Each skill package is a subdirectory containing:

    - ``config.yaml`` — skill metadata and default parameters
    - ``skill.md``    — agent-readable description (optional)
    - ``executor.py`` — ``create_executor(context) -> Callable`` factory
    """

    def load_all(
        self,
        skills_dir: str,
        contexts: Optional[Dict[str, Any]] = None,
        *,
        registry: Optional[SkillRegistry] = None,
    ) -> List[SkillMetadata]:
        """
        Scan *skills_dir* for subdirectories containing ``config.yaml``
        and load each as a skill package.

        Args:
            skills_dir: Path to the skills directory.
            contexts: Mapping of context-type names to context objects
                (e.g. ``{"retrieve": RetrieveContext(...)}``).
            registry: Registry to populate; defaults to the singleton.

        Returns:
            List of loaded ``SkillMetadata`` objects.
        """
        contexts = contexts or {}
        reg = registry or SkillRegistry()
        loaded: List[SkillMetadata] = []

        skills_path = Path(skills_dir)
        if not skills_path.is_dir():
            return loaded

        for entry in sorted(skills_path.iterdir()):
            if not entry.is_dir():
                continue
            config_file = entry / "config.yaml"
            if not config_file.exists():
                continue

            metadata = self.load_skill(str(entry), contexts)
            if metadata is not None:
                if not reg.has(metadata.skill_id):
                    reg.register(metadata)
                loaded.append(metadata)

        return loaded

    def load_skill(
        self,
        skill_dir: str,
        contexts: Optional[Dict[str, Any]] = None,
    ) -> Optional[SkillMetadata]:
        """
        Load a single skill package from *skill_dir*.

        Returns ``None`` if the directory lacks a ``config.yaml``.
        """
        contexts = contexts or {}
        skill_path = Path(skill_dir)

        config_file = skill_path / "config.yaml"
        if not config_file.exists():
            return None

        # --- 1. Parse config.yaml ---
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}

        skill_id: str = config["skill_id"]
        skill_type = _SKILL_TYPE_MAP.get(
            config.get("skill_type", "custom"), SkillType.CUSTOM
        )
        cost = _COST_MAP.get(config.get("cost", "medium"), Cost.MEDIUM)
        operator = config.get("operator")
        defaults = config.get("defaults", {})
        resources = config.get("resources", [])
        dependencies = config.get("dependencies", [])

        inputs = _parse_inputs(config.get("inputs", []))
        outputs = _parse_outputs(config.get("outputs", {}))
        index_requirements = _parse_index_requirements(
            config.get("index_requirements", [])
        )

        # --- 2. Read skill.md ---
        skill_doc = ""
        skill_md_file = skill_path / "skill.md"
        if skill_md_file.exists():
            skill_doc = skill_md_file.read_text(encoding="utf-8")

        # --- 3. Load executor ---
        executor_fn = _load_executor(skill_path, skill_type, contexts)

        # --- 4. Build SkillMetadata ---
        return SkillMetadata(
            skill_id=skill_id,
            skill_type=skill_type,
            inputs=inputs,
            outputs=outputs,
            executor_fn=executor_fn,
            async_capable=False,
            cacheable=config.get("cacheable", True),
            cost=cost,
            dependencies=dependencies,
            resources=resources,
            operator=operator,
            defaults=defaults,
            index_requirements=index_requirements,
            skill_doc=skill_doc,
            description=skill_doc or config.get("description", ""),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_inputs(raw: List[Dict[str, Any]]) -> List[SkillInputSpec]:
    result: List[SkillInputSpec] = []
    for item in raw:
        result.append(
            SkillInputSpec(
                name=item["name"],
                type_hint=item.get("type", "Any"),
                required=item.get("required", True),
                default=item.get("default"),
                description=item.get("description", ""),
                is_line_number=item.get("is_line_number", False),
            )
        )
    return result


def _parse_outputs(raw: Dict[str, Any]) -> SkillOutputSpec:
    if not raw:
        return SkillOutputSpec(type_hint="Any")
    return SkillOutputSpec(
        type_hint=raw.get("type", "Any"),
        description=raw.get("description", ""),
    )


def _parse_index_requirements(raw: List[Dict[str, Any]]) -> List[Any]:
    from ...compiler.resources import IndexRequirement

    result: List[IndexRequirement] = []
    for item in raw:
        result.append(
            IndexRequirement(
                index_type=item["type"],
                max_age_seconds=float(item.get("max_age_seconds", 3600)),
                scope=item.get("scope", "current_repo"),
                required=item.get("required", True),
            )
        )
    return result


def _load_executor(
    skill_path: Path,
    skill_type: SkillType,
    contexts: Dict[str, Any],
) -> Optional[Callable[..., Any]]:
    """Import ``executor.py`` and call ``create_executor(context)``."""
    executor_file = skill_path / "executor.py"
    if not executor_file.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        f"codeminer.agent.skills.{skill_path.name}.executor",
        str(executor_file),
    )
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    create_fn = getattr(module, "create_executor", None)
    if create_fn is None:
        return None

    # CUSTOM composer skills (e.g. ``codeminer_context``, the names-mode of
    # ``bm25_search``) need more than one context, so they receive a typed
    # ``ComposerContexts`` bundle. Fall back to no-arg for context-free
    # custom skills.
    if skill_type == SkillType.CUSTOM:
        # Decide arg-passing by the factory's *signature* rather than catching
        # TypeError — a bare ``except TypeError`` would swallow a genuine
        # TypeError raised inside the factory body and re-raise a misleading
        # "missing argument" error.
        if inspect.signature(create_fn).parameters:
            return create_fn(ComposerContexts.from_mapping(contexts))
        return create_fn()

    # Single-context skills receive the one typed context dataclass for their
    # type (``RetrieveContext``/``RerankContext``/...), keyed on the enum. It
    # may be ``None`` when the backing index wasn't built — the executor
    # surfaces that as a clean error only if the LLM actually calls it.
    ctx_key = CONTEXT_KEY_FOR_TYPE.get(skill_type)
    context = contexts.get(ctx_key) if ctx_key else None
    return create_fn(context)
