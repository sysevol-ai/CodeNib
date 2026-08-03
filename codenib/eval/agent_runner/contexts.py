# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable skill-context loading for agent-runner evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple


def _ordered_unique_strings(
    values: Iterable[str],
    *,
    field_name: str,
    allow_empty: bool,
) -> Tuple[str, ...]:
    seen = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} entries must be strings")
        name = value.strip()
        if not name:
            raise ValueError(f"{field_name} entries must be non-empty strings")
        if name not in seen:
            seen.append(name)
    if not seen and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    return tuple(seen)


def default_agent_skills_dir() -> Path:
    """Return the packaged CodeNib agent skills directory."""
    return Path(__file__).resolve().parents[2] / "agent" / "skills"


@dataclass(frozen=True)
class AgentSkillContextSpec:
    """Declarative inputs for loading agent skill contexts.

    The spec is intentionally limited to reusable runtime/index parameters.
    Experiment arms, scorer fields, dataset ids, and model-routing policy stay
    with the caller.
    """

    skill_ids: Sequence[str]
    embedding_model: str
    embedding_dimension: int
    top_k: int
    embedding_revision: Optional[str] = None
    languages: Sequence[str] = ("python",)
    default_level: str = "l2"
    rebuild: bool = False
    skills_dir: Optional[Path] = None
    trust_remote_code: bool = False
    embedding_batch_size: Optional[int] = None

    def __post_init__(self) -> None:
        if self.embedding_dimension <= 0:
            raise ValueError("embedding_dimension must be positive")
        if self.top_k <= 0:
            raise ValueError("top_k must be positive")
        if not self.embedding_model.strip():
            raise ValueError("embedding_model must be non-empty")
        if not self.default_level.strip():
            raise ValueError("default_level must be non-empty")

        object.__setattr__(
            self,
            "skill_ids",
            _ordered_unique_strings(
                self.skill_ids,
                field_name="skill_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "languages",
            _ordered_unique_strings(
                self.languages,
                field_name="languages",
                allow_empty=False,
            ),
        )

    def resolved_skills_dir(self) -> Path:
        """Return the configured skills directory or the packaged default."""
        if self.skills_dir is None:
            return default_agent_skills_dir()
        return Path(self.skills_dir)


def load_agent_skill_contexts(
    *,
    repo_path: str,
    cache_dir: str,
    spec: AgentSkillContextSpec,
) -> Dict[str, Any]:
    """Build contexts and load all agent skills against those contexts.

    Skill packages are loaded twice: first metadata-only so the compiler can
    inspect requirements, then with built contexts so executors are wired for
    the next agent run.
    """
    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.compiler import build_skill_contexts

    skills_dir = str(spec.resolved_skills_dir())

    SkillRegistry.reset()
    SkillLoader().load_all(skills_dir, contexts=None)

    contexts = build_skill_contexts(
        repo_path=repo_path,
        skill_ids=spec.skill_ids,
        languages=spec.languages,
        cache_dir=cache_dir,
        skills_dir=skills_dir,
        embedding_model=spec.embedding_model,
        embedding_revision=spec.embedding_revision,
        embedding_dimension=spec.embedding_dimension,
        default_top_k=spec.top_k,
        default_level=spec.default_level,
        rebuild=spec.rebuild,
        trust_remote_code=spec.trust_remote_code,
        embedding_batch_size=spec.embedding_batch_size,
    )

    SkillRegistry.reset()
    SkillLoader().load_all(skills_dir, contexts=contexts)
    return contexts
