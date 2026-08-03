# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Manifest-aware resource checking for the agent.

Before the agent loop starts, ``ResourceGuard`` checks which skills can
actually execute given the current index state (fresh / stale / missing).
Skills whose required indexes are missing are excluded from the tool list;
stale index warnings are injected into the system prompt so the LLM can
inform the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from ..compiler.manifest import ManifestIndexStateStore, RepoManifest
from ..compiler.resources import IndexRequirement, ResourceResolver
from .skills.registry import SkillRegistry


@dataclass(slots=True)
class PreflightReport:
    """Result of checking all skills against the manifest."""

    available: Set[str] = field(default_factory=set)
    degraded: Set[str] = field(default_factory=set)
    unavailable: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)


class ResourceGuard:
    """Check skill availability against a repo manifest.

    Usage::

        guard = ResourceGuard(manifest, registry)
        report = guard.preflight()
        # report.available  — skills ready to use
        # report.warnings   — stale index notices for the system prompt
    """

    def __init__(
        self,
        manifest: RepoManifest,
        registry: Optional[SkillRegistry] = None,
    ) -> None:
        self._manifest = manifest
        self._registry = registry or SkillRegistry()
        self._state_store = ManifestIndexStateStore(manifest)
        self._resolver = ResourceResolver(self._state_store)

    def preflight(self) -> PreflightReport:
        """Check all registered skills against the manifest."""
        report = PreflightReport()

        for skill_id, meta in self._registry.list_skills().items():
            requirements: List[IndexRequirement] = getattr(
                meta, "index_requirements", []
            )
            if not requirements:
                report.available.add(skill_id)
                continue

            plan = self._resolver.resolve(requirements)

            if not plan.can_execute:
                report.unavailable.add(skill_id)
                missing = ", ".join(plan.blocking_builds)
                report.warnings.append(
                    f"Skill {skill_id!r} unavailable: missing indexes [{missing}]"
                )
            elif plan.warnings:
                report.degraded.add(skill_id)
                report.available.add(skill_id)
                report.warnings.extend(plan.warnings)
            else:
                report.available.add(skill_id)

        return report

    def available_skill_ids(self) -> Set[str]:
        """Return skill IDs whose required indexes exist."""
        return self.preflight().available

    def stale_warnings(self) -> List[str]:
        """Return human-readable warnings about stale indexes."""
        return self.preflight().warnings
