# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Always-on default tool primitives for the agent.

This package is deliberately separate from ``codeminer/agent/skills/``:

- **``tools/`` (here)** — always-on primitives that scan the *raw
  filesystem* (``file_read``, ``file_search``). They carry no
  ``index_requirements``, are never loaded from ``config.yaml``/``executor.py``
  packages, and are never dropped by the ``allow_skills`` / ``exclude_skills``
  filters. Every ``Ax`` subset builds on top of them.
- **``skills/``** — index-backed retrieval skills (``bm25_search``,
  ``embedding_search``, ``graph_expand``, ...) declared as
  ``config.yaml`` + ``executor.py`` packages and gated by their declared
  index resources.

Both are expressed as :class:`~codeminer.agent.skills.core.SkillMetadata`
so they share the same registry and tool-schema machinery; the package
split is what marks the "always-on primitive" vs "optional index-backed
skill" boundary. See :func:`ensure_defaults_registered`.
"""

from .defaults import (
    DEFAULT_SKILL_IDS,
    ensure_defaults_registered,
    get_default_skill_metadata,
)

__all__ = [
    "DEFAULT_SKILL_IDS",
    "ensure_defaults_registered",
    "get_default_skill_metadata",
]
