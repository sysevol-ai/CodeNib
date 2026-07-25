# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Typed skill-context layer.

Skill executors are factories of the shape ``create_executor(context) ->
Callable``. Historically the ``context`` argument was annotated ``Any`` in
every skill and the loader selected it via a *stringly-typed* dict lookup
(``_CONTEXT_KEY_FOR_TYPE``), so the concrete, well-typed dataclasses defined in
``codenib/ops/`` (``RetrieveContext``, ``RerankContext``, ``TransformContext``,
``ExpandContext``) were erased to ``Any`` the moment they crossed into a skill.

This module makes the boundary typed:

* :data:`SkillContext` — the union of the four per-op context dataclasses;
  the type a *single-context* skill's ``create_executor`` should accept.
* :class:`ComposerContexts` — a typed aggregate for *multi-context* (``custom``)
  composer skills (``codenib_context``, ``bm25_search``) that genuinely need
  more than one context. Replaces the untyped ``Dict[str, Any]`` they took.
* :data:`CONTEXT_KEY_FOR_TYPE` — ``SkillType -> context-dict key`` mapping,
  keyed on the enum (not a bare string), used by the loader to pick the single
  context a skill receives.

The dataclasses live in ``ops/`` (they carry behaviour like
``ensure_agent``); this module only re-exports them as a typed surface so the
agent layer can annotate against them without re-importing ``ops`` everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Mapping, Optional, Union

from .core import SkillType

if TYPE_CHECKING:  # avoid an import cycle (ops -> agent.* -> ops) at runtime
    from ...ops.expand import ExpandContext
    from ...ops.rerank import CrossEncoderContext, RerankContext
    from ...ops.retrieve import RetrieveContext
    from ...ops.transform import TransformContext

    # The context a single-context skill's create_executor() receives. A
    # composer takes :class:`ComposerContexts` instead.
    SkillContext = Union[
        RetrieveContext, RerankContext, TransformContext, ExpandContext
    ]
else:
    # Runtime placeholder so the public ``SkillContext`` name in ``__all__``
    # resolves. The precise Union (above) references ``ops.*`` dataclasses that
    # would form an import cycle at runtime; annotations stay lazy via
    # ``from __future__ import annotations``.
    SkillContext = object


# ``SkillType`` -> the key under which the loader/compiler files that type's
# context in the contexts dict. Keyed on the enum so callers get a real type
# error on a bad key rather than a silent miss. Mirrors
# ``compiler.skill_context._package_contexts`` (retrieval/aggregate share the
# ``retrieve`` context; expand skills share ``expand``; ...).
CONTEXT_KEY_FOR_TYPE: Dict[SkillType, str] = {
    SkillType.RETRIEVAL: "retrieve",
    SkillType.AGGREGATE: "retrieve",
    SkillType.RERANK: "rerank",
    SkillType.TRANSFORM: "transform",
    SkillType.EXPAND: "expand",
}


@dataclass
class ComposerContexts:
    """Typed bundle of per-op contexts for multi-context composer skills.

    ``custom`` skills that compose several stages (search + graph expand,
    e.g. ``codenib_context`` and the names-mode of ``bm25_search``) need
    more than one context. They used to receive the loader's raw
    ``Dict[str, Any]`` and reach into it with ``.get("retrieve")`` /
    ``getattr(..., None)``. This dataclass gives that bundle a name and a
    type; any field may be ``None`` when the corresponding index/context was
    not built.

    Fields mirror the keys produced by
    ``codenib.compiler.skill_context._package_contexts``.
    """

    retrieve: Optional["RetrieveContext"] = None
    expand: Optional["ExpandContext"] = None
    rerank: Optional["RerankContext"] = None
    transform: Optional["TransformContext"] = None
    cross_encoder: Optional["CrossEncoderContext"] = None

    @classmethod
    def from_mapping(
        cls, contexts: Optional[Mapping[str, object]]
    ) -> "ComposerContexts":
        """Build from the loader's ``{key: context}`` mapping (or ``None``)."""
        d = contexts or {}
        return cls(
            retrieve=d.get("retrieve"),  # type: ignore[arg-type]
            expand=d.get("expand"),  # type: ignore[arg-type]
            rerank=d.get("rerank"),  # type: ignore[arg-type]
            transform=d.get("transform"),  # type: ignore[arg-type]
            cross_encoder=d.get("cross_encoder"),  # type: ignore[arg-type]
        )


__all__ = ["SkillContext", "ComposerContexts", "CONTEXT_KEY_FOR_TYPE"]
