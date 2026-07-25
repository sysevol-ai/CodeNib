# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Always-on default tool primitives for the agent.

This package is deliberately separate from ``codenib/agent/skills/`` — and
tools are now a separate *type* (:class:`ToolSpec`), not ``SkillMetadata``:

- **``tools/`` (here)** — always-on primitives that scan the *raw filesystem*
  (``read``, ``grep``, ``glob``, ``bash`` — the mainstream Claude Code tool
  shapes). They carry no ``index_requirements``, no ``operator``, and no
  retrieval ``skill_type``; they are never loaded from
  ``config.yaml``/``executor.py`` packages, and are never dropped by the
  ``allow_skills`` / ``exclude_skills`` filters. They live in a
  :class:`ToolRegistry` the runner holds alongside its ``SkillRegistry``.
- **``skills/``** — index-backed retrieval skills (``bm25_search``,
  ``embedding_search``, ``find_callers``, ...) declared as
  ``config.yaml`` + ``executor.py`` packages, gated by their declared index
  resources, and exposed through the ``SkillRegistry``.

The runner exposes both to the model and dispatches over both, but they are two
concepts with distinct lifecycles (skills reset per query; tools are static).
See :func:`ensure_default_tools_registered`.
"""

from .defaults import (
    DEFAULT_TOOL_IDS,
    ensure_default_tools_registered,
    get_default_tool_specs,
)
from .spec import ToolInputSpec, ToolRegistry, ToolSpec

__all__ = [
    "DEFAULT_TOOL_IDS",
    "ToolInputSpec",
    "ToolRegistry",
    "ToolSpec",
    "ensure_default_tools_registered",
    "get_default_tool_specs",
]
