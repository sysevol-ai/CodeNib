# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic multimodal planning hooks for wiki pages.

The first multimodal wiki layer plans source-grounded media opportunities
without calling an image, video, or diagram model. Later generators can consume
these slots and write concrete assets while preserving the same page contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from itertools import islice
from typing import Any, Iterable, Literal, Mapping

from .importance import FIGURE_TIERS, TIER_DETAIL
from .visual_ir import (
    architecture_contract_from_plan,
    flow_contract_from_plan,
    flow_contract_from_relations,
)

MediaKind = Literal["diagram", "image", "storyboard", "chart", "video"]
MediaPlacement = Literal["lead", "section", "aside", "appendix"]
MEDIA_PLAN_VERSION = 14

_MAX_SOURCE_CITATIONS = 6


@dataclass(frozen=True)
class WikiMediaSlot:
    """A planned multimodal asset for one wiki page."""

    id: str
    kind: MediaKind
    placement: MediaPlacement
    title: str
    purpose: str
    source_citations: tuple[str, ...] = ()
    prompt: str = ""
    render_contract: dict[str, Any] | None = None
    human_prior: dict[str, Any] = field(
        default_factory=lambda: {"editable": True, "notes": []}
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_citations"] = list(self.source_citations)
        if self.render_contract is None:
            data.pop("render_contract")
        return data


def _citation_files(citations: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    files = []
    seen = set()
    try:
        values = islice(iter(citations or ()), _MAX_SOURCE_CITATIONS * 4)
    except TypeError:
        return ()
    for citation in values:
        if not isinstance(citation, Mapping):
            continue
        file = str(citation.get("file") or "").strip()
        if not file or len(file.encode("utf-8")) > 4096 or file in seen:
            continue
        seen.add(file)
        files.append(file)
        if len(files) >= _MAX_SOURCE_CITATIONS:
            break
    return tuple(files)


def _safe_contract(factory, *args, **kwargs) -> dict[str, Any] | None:
    try:
        return factory(*args, **kwargs)
    except (TypeError, ValueError):
        return None


def _call_path_title(contract: Mapping[str, Any]) -> str:
    """Name the path by its ends and say that it is one path of many."""

    data = contract["data"]
    nodes = {node["id"]: node["label"] for node in data["nodes"]}
    targets = {edge["target"] for edge in data["edges"]}
    sources = {edge["source"] for edge in data["edges"]}
    start = next(node for node in nodes if node not in targets)
    end = next(node for node in nodes if node not in sources)
    return f"Selected call path: {nodes[start]} → {nodes[end]}"


def _call_path_slot(
    *,
    slot_id: str,
    contract: dict[str, Any],
    citations: tuple[str, ...],
    purpose: str,
) -> dict[str, Any]:
    return WikiMediaSlot(
        id=slot_id,
        kind="diagram",
        placement="aside",
        title=_call_path_title(contract),
        purpose=purpose,
        source_citations=citations,
        prompt=(
            "Draw the supplied call path exactly. Every arrow is a call recorded "
            "in the index; do not add, remove or rename a step."
        ),
        render_contract=contract,
    ).to_dict()


def plan_media_slots(
    *,
    page_id: str,
    title: str,
    citations: Iterable[dict[str, Any]] = (),
    diagram: str = "",
    relations: Iterable[dict[str, Any]] = (),
    story: Mapping[str, Any] | None = None,
    flow: Mapping[str, Any] | None = None,
    architecture: Mapping[str, Any] | None = None,
    tier: str = TIER_DETAIL,
) -> list[dict[str, Any]]:
    """Return deterministic media slots for a wiki page.

    These slots intentionally describe *what* should be rendered, not *how* to
    call a provider. That keeps the wiki API stable while allowing future VLM
    adapters and human-prior configuration to fill the assets later.

    The Overview draws the main path the index recorded from the public entry
    inward (``flow``), beside its semantic architecture when one was admitted.
    A page in ``FIGURE_TIERS`` draws the longest call path among its relations.
    Arrows always come from the index; a page with no such path draws nothing.
    """

    del diagram, story
    citation_files = _citation_files(citations)
    if page_id != "overview":
        if tier not in FIGURE_TIERS:
            return []
        contract = _safe_contract(flow_contract_from_relations, relations)
        if contract is None:
            return []
        return [
            _call_path_slot(
                slot_id=f"{page_id or 'page'}-call-path",
                contract=contract,
                citations=citation_files,
                purpose="Show how this area's code calls through, step by step.",
            )
        ]

    slots: list[dict[str, Any]] = []
    flow_contract = _safe_contract(flow_contract_from_plan, flow)
    if flow_contract is not None:
        slots.append(
            _call_path_slot(
                slot_id="overview-main-path",
                contract=flow_contract,
                citations=citation_files,
                purpose="Show the main path through the code, from the public entry.",
            )
        )
    structure_contract = _safe_contract(
        architecture_contract_from_plan,
        architecture,
    )
    if structure_contract is None:
        return slots

    page_slug = page_id or "page"
    page_title = title or page_slug
    authored_title = (
        str(architecture.get("title") or "").strip()
        if isinstance(architecture, Mapping)
        else ""
    )
    if authored_title.casefold() in {
        "architecture overview",
        "how the system is organized",
        "overview",
        "runtime architecture",
        "system architecture",
    }:
        authored_title = ""
    if not authored_title:
        data = structure_contract["data"]
        nodes_by_id = {node["id"]: node for node in data["nodes"]}
        primary_path = data["primary_path"]
        authored_title = (
            f"{nodes_by_id[primary_path[0]]['label']} → "
            f"{nodes_by_id[primary_path[-1]]['label']}"
        )
    slot = WikiMediaSlot(
        id=f"{page_slug}-system-architecture",
        kind="diagram",
        placement="aside",
        title=authored_title or f"{page_title}: runtime architecture",
        purpose=(
            "Show the system's architectural roles, primary path, supporting "
            "dependencies, and explicit boundaries."
        ),
        source_citations=citation_files,
        prompt=(
            "Render the supplied semantic architecture contract exactly. Do "
            "not replace its roles and boundaries with file or call graphs."
        ),
        render_contract=structure_contract,
    )
    return [slot.to_dict(), *slots]


__all__ = ["MEDIA_PLAN_VERSION", "WikiMediaSlot", "plan_media_slots"]
