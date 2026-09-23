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

from .visual_ir import (
    architecture_contract_from_plan,
    architecture_contract_from_relations,
    flow_contract_from_plan,
)

MediaKind = Literal["diagram", "image", "storyboard", "chart", "video"]
MediaPlacement = Literal["lead", "section", "aside", "appendix"]
MEDIA_PLAN_VERSION = 11

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
) -> list[dict[str, Any]]:
    """Return deterministic media slots for a wiki page.

    These slots intentionally describe *what* should be rendered, not *how* to
    call a provider. That keeps the wiki API stable while allowing future VLM
    adapters and human-prior configuration to fill the assets later.
    """

    del diagram, story
    if page_id != "overview":
        return []
    structure_contract = _safe_contract(
        architecture_contract_from_plan,
        architecture,
    )
    if structure_contract is None:
        # No admitted architecture: fall back to the recorded entry path, so
        # the landing page still opens with one grounded picture of the
        # system rather than none.
        flow_contract = _safe_contract(flow_contract_from_plan, flow)
        if flow_contract is None:
            # No recorded path either: the page's own static relations are
            # still a true, cited picture of which components touch which.
            relation_contract = _safe_contract(
                architecture_contract_from_relations,
                list(relations or ()),
            )
            if relation_contract is None:
                return []
            page_slug = page_id or "page"
            slot = WikiMediaSlot(
                id=f"{page_slug}-relation-map",
                kind="diagram",
                placement="aside",
                title=f"{title or page_slug}: recorded relations",
                purpose=(
                    "Show the components this page cites and the call sites "
                    "recorded between them."
                ),
                source_citations=_citation_files(citations),
                prompt=(
                    "Render the supplied relation contract exactly; every edge "
                    "is a recorded call site."
                ),
                render_contract=relation_contract,
            )
            return [slot.to_dict()]
        page_slug = page_id or "page"
        flow_title = (
            str((flow or {}).get("title") or "").strip()
            if isinstance(flow, Mapping)
            else ""
        )
        slot = WikiMediaSlot(
            id=f"{page_slug}-entry-path",
            kind="diagram",
            placement="aside",
            title=flow_title or f"{title or page_slug}: how a call moves",
            purpose=(
                "Show the recorded call path from the public entry inward, one "
                "stage per hop, as the index proves it."
            ),
            source_citations=_citation_files(citations),
            prompt=(
                "Render the supplied flow contract exactly; every arrow is a "
                "recorded call site."
            ),
            render_contract=flow_contract,
        )
        return [slot.to_dict()]

    citation_files = _citation_files(citations)
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
    return [slot.to_dict()]


__all__ = ["MEDIA_PLAN_VERSION", "WikiMediaSlot", "plan_media_slots"]
