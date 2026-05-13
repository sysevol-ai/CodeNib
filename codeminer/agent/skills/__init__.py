# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Declarative skill definitions for Codeminer agents.

Built-in skills are loaded from sub-packages via ``SkillLoader``.
User code can still define additional skills with the ``@skill`` decorator.
"""

from .core import Cost, SkillInputSpec, SkillMetadata, SkillOutputSpec, SkillType
from .loader import SkillLoader
from .registry import SkillRegistry, skill

__all__ = [
    "Cost",
    "SkillInputSpec",
    "SkillLoader",
    "SkillMetadata",
    "SkillOutputSpec",
    "SkillRegistry",
    "SkillType",
    "skill",
]
