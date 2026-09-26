# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Typed, source-grounded render contracts for Wiki visual adapters."""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from itertools import islice
from typing import Any

VISUAL_SCHEMA_VERSION = 1
VISUAL_ADAPTERS = frozenset({"architecture", "flow", "storyboard", "bar-chart"})
VISUAL_ADAPTER_MEDIA_KIND = {
    "architecture": "diagram",
    "flow": "diagram",
    "storyboard": "storyboard",
    "bar-chart": "chart",
}

_ADAPTER_PROVENANCE = {
    "architecture": frozenset(
        {"architecture-plan", "relations", "deterministic-index"}
    ),
    "flow": frozenset({"relations"}),
    "storyboard": frozenset({"story"}),
    "bar-chart": frozenset({"metrics"}),
}

_MAX_CONTRACT_BYTES = 64 * 1024
_MAX_NODES = 12
_MAX_FLOW_NODES = 7
_MAX_EDGES = 18
_MAX_PANELS = 8
_MAX_BARS = 10
_MAX_EVIDENCE = 24
_MAX_TEXT_BYTES = 1024
_ARCHITECTURE_LAYERS = frozenset(
    {"external", "interface", "coordination", "execution", "data"}
)
_ARCHITECTURE_KINDS = frozenset(
    {
        "external",
        "frontend",
        "backend",
        "security",
        "messagebus",
        "database",
        "cloud",
    }
)
# A connection that merely restates the call graph ("calls X", "imports Y")
# is not an architectural relationship. Match the phrasing, not the word: a
# role that handles "system calls" or "reference counting" is a fine label.
_CALL_GRAPH_EDGE_RE = re.compile(
    r"^\s*(?:(?:it|which|that|and)\s+)?(?:calls?|depends\s+on|imports?|references?)\b"
    r"|\b(?:calls?|imports?|references?)\s+(?:the\s+|a\s+|an\s+)?[`A-Z_a-z][\w.]*\(",
    re.IGNORECASE,
)
# ``kind`` and ``layer`` are separate axes, but a plan that writes a kind
# where a layer belongs still names a real role; map it instead of dropping
# the whole architecture.
_KIND_TO_LAYER = {
    "external": "external",
    "cloud": "external",
    "frontend": "interface",
    "messagebus": "coordination",
    "backend": "execution",
    "security": "execution",
    "database": "data",
}


def _label_names_source_path(label: str) -> bool:
    """Tell ``src/core/router`` from ``I/O driver`` or ``NumPy/SciPy backend``.

    A slash makes a label a source path only when the slashed token reads
    like one: lowercase path characters throughout, or several segments.
    """

    for token in label.split():
        if "/" not in token:
            continue
        if token.count("/") >= 2 or re.fullmatch(
            r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)+", token
        ):
            return True
    return False


def _connected_primary_path(
    path: Sequence[str], edge_keys: set[tuple[str, str]]
) -> list[str]:
    """Return the longest run of ``path`` whose steps are authored connections.

    A plan usually gets one step wrong: it lists a component on the path but
    forgets to author the edge into it. Skipping that component keeps the
    rest of the path; discarding the whole architecture would not. Among
    equally long runs the one that still ends on the plan's outcome wins.
    """

    if not path:
        return []
    best_len = [1] * len(path)
    previous: list[int | None] = [None] * len(path)
    for index in range(len(path)):
        for earlier in range(index):
            if (path[earlier], path[index]) in edge_keys and (
                best_len[earlier] + 1 > best_len[index]
            ):
                best_len[index] = best_len[earlier] + 1
                previous[index] = earlier
    longest = max(best_len)
    end = len(path) - 1
    if best_len[end] != longest:
        end = best_len.index(longest)
    run: list[str] = []
    cursor: int | None = end
    while cursor is not None:
        run.append(path[cursor])
        cursor = previous[cursor]
    return list(reversed(run))


def repair_architecture_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the deterministic repairs a plan may need before validation.

    The gate keeps its rules; this only fixes what a rule can prove: a kind
    written in the layer field maps to that kind's layer, and a primary path
    keeps its longest connected run. Anything else still fails validation.
    """

    plan = dict(value)
    components = []
    for raw in plan.get("components") or ():
        if isinstance(raw, Mapping):
            layer = str(raw.get("layer") or "")
            kind = str(raw.get("kind") or "")
            if layer not in _ARCHITECTURE_LAYERS and layer in _KIND_TO_LAYER:
                raw = {**raw, "layer": _KIND_TO_LAYER[layer]}
            elif layer not in _ARCHITECTURE_LAYERS and kind in _KIND_TO_LAYER:
                raw = {**raw, "layer": _KIND_TO_LAYER[kind]}
        components.append(raw)
    plan["components"] = components
    edge_keys = {
        (str(raw.get("source") or ""), str(raw.get("target") or ""))
        for raw in plan.get("connections") or ()
        if isinstance(raw, Mapping)
    }
    path = [str(item) for item in plan.get("primary_path") or ()]
    if path and any(
        (a, b) not in edge_keys for a, b in zip(path, path[1:], strict=False)
    ):
        plan["primary_path"] = _connected_primary_path(path, edge_keys)
    return plan


# These are schema examples, not meaningful repository roles. Reject a plan
# that copies several of them together: it has reproduced the prompt template
# instead of synthesizing the architecture supported by this repository.
_ARCHITECTURE_PLACEHOLDER_LABELS = frozenset(
    {
        "entry surfaces",
        "coordination core",
        "execution engine",
        "result store",
    }
)
_NODE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_MERMAID_NODE = re.compile(
    r"^\s*(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s*"
    r'(?:\["(?P<label>.*?)"\]|\[\'(?P<label_single>.*?)\'\])\s*$'
)
_MERMAID_EDGE = re.compile(
    r"^\s*(?P<source>[A-Za-z][A-Za-z0-9_-]*)\s*-->"
    r"(?:\|(?P<label>[^|\n]*)\|)?\s*"
    r"(?P<target>[A-Za-z][A-Za-z0-9_-]*)"
    r'(?:\["(?P<target_label>.*?)"\]|\[\'(?P<target_single>.*?)\'\])?\s*$'
)


def _text(value: Any, *, label: str, required: bool = True) -> str:
    result = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not result:
        raise ValueError(f"visual {label} is required")
    if len(result.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"visual {label} exceeds the byte limit")
    return result


def _evidence(values: Any, *, allowed: set[str] | None = None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("visual evidence must be a sequence")
    result: list[str] = []
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError("visual evidence must be a sequence") from exc
    for raw in iterator:
        value = _text(raw, label="evidence reference")
        if allowed is not None and value not in allowed:
            raise ValueError(f"visual evidence is not on the page: {value}")
        if value not in result:
            result.append(value)
        if len(result) > _MAX_EVIDENCE:
            raise ValueError("visual evidence count exceeds the limit")
    return result


def _bounded_items(values: Any, *, limit: int, label: str) -> list[Any]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"visual {label} must be a sequence")
    try:
        items = list(islice(iter(values), limit + 1))
    except TypeError as exc:
        raise ValueError(f"visual {label} must be a sequence") from exc
    if len(items) > limit:
        raise ValueError(f"visual {label} count exceeds the limit")
    return items


def _node(value: Any, *, allowed: set[str] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("visual node must be an object")
    node_id = _text(value.get("id"), label="node id")
    if _NODE_ID.fullmatch(node_id) is None:
        raise ValueError(f"visual node id is invalid: {node_id!r}")
    node = {
        "id": node_id,
        "label": _text(value.get("label"), label="node label"),
        "detail": _text(value.get("detail"), label="node detail", required=False),
        "evidence": _evidence(value.get("evidence"), allowed=allowed),
    }
    for key in ("kind", "layer"):
        optional = _text(
            value.get(key),
            label=f"node {key}",
            required=False,
        ).casefold()
        if optional:
            node[key] = optional
    return node


def _edge(
    value: Any,
    *,
    node_ids: set[str],
    allowed: set[str] | None,
    require_evidence: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("visual edge must be an object")
    source = _text(value.get("source"), label="edge source")
    target = _text(value.get("target"), label="edge target")
    if source not in node_ids or target not in node_ids:
        raise ValueError("visual edge references an unknown node")
    if source == target:
        raise ValueError("visual edge cannot point to itself")
    evidence = _evidence(value.get("evidence"), allowed=allowed)
    if require_evidence and not evidence:
        raise ValueError("relation-backed visual edge requires evidence")
    return {
        "source": source,
        "target": target,
        "label": _text(value.get("label"), label="edge label", required=False),
        "evidence": evidence,
    }


def _graph_data(
    data: Any,
    *,
    adapter: str,
    provenance: str,
    allowed: set[str] | None,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{adapter} visual data must be an object")
    raw_nodes = data.get("nodes") or ()
    raw_edges = data.get("edges") or ()
    maximum_nodes = _MAX_FLOW_NODES if adapter == "flow" else _MAX_NODES
    nodes = [
        _node(item, allowed=allowed)
        for item in _bounded_items(
            raw_nodes,
            limit=maximum_nodes,
            label=f"{adapter} nodes",
        )
    ]
    if not 2 <= len(nodes) <= maximum_nodes:
        raise ValueError(f"{adapter} visual requires 2-{maximum_nodes} nodes")
    node_ids = [node["id"] for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("visual node ids must be unique")
    require_edge_evidence = provenance in {"architecture-plan", "relations"}
    edges = [
        _edge(
            item,
            node_ids=set(node_ids),
            allowed=allowed,
            require_evidence=require_edge_evidence,
        )
        for item in _bounded_items(
            raw_edges,
            limit=_MAX_EDGES,
            label=f"{adapter} edges",
        )
    ]
    if not 1 <= len(edges) <= _MAX_EDGES:
        raise ValueError(f"{adapter} visual requires 1-{_MAX_EDGES} edges")
    edge_keys = {(edge["source"], edge["target"]) for edge in edges}
    if len(edge_keys) != len(edges):
        raise ValueError("visual edges must be unique")

    result: dict[str, Any] = {"nodes": nodes, "edges": edges}
    if adapter == "architecture" and provenance == "architecture-plan":
        if not 4 <= len(nodes) <= 10:
            raise ValueError("planned architecture requires 4-10 components")
        copied_placeholders = {
            node["label"].casefold()
            for node in nodes
            if node["label"].casefold() in _ARCHITECTURE_PLACEHOLDER_LABELS
        }
        if len(copied_placeholders) >= 2:
            raise ValueError(
                "planned architecture must use repository-specific roles, not "
                "prompt placeholders"
            )
        if len({str(node.get("layer") or "") for node in nodes}) < 3:
            raise ValueError("planned architecture requires at least three layers")
        for node in nodes:
            if node.get("layer") not in _ARCHITECTURE_LAYERS:
                raise ValueError("planned architecture node layer is invalid")
            if node.get("kind") not in _ARCHITECTURE_KINDS:
                raise ValueError("planned architecture node kind is invalid")
            if (
                not node["detail"]
                or not node["evidence"]
                or not any(item.startswith("E") for item in node["evidence"])
            ):
                raise ValueError(
                    "every planned architecture component needs responsibility evidence"
                )
            if (
                (node["label"].endswith(")") and "(" in node["label"])
                or "`" in node["label"]
                or _label_names_source_path(node["label"])
                or re.search(
                    r"\.(?:py|go|rs|ts|tsx|js|jsx|java|rb|php|cs|kt|cpp|c|h)\b",
                    node["label"],
                    re.IGNORECASE,
                )
            ):
                raise ValueError(
                    "planned architecture labels must name roles, not source symbols"
                )
        for edge in edges:
            if not edge["label"]:
                raise ValueError("planned architecture connections require labels")
            if _CALL_GRAPH_EDGE_RE.search(edge["label"]):
                raise ValueError(
                    "planned architecture connections require semantic labels"
                )

        primary_path = [
            _text(item, label="primary path component")
            for item in _bounded_items(
                data.get("primary_path") or (),
                limit=7,
                label="architecture primary path",
            )
        ]
        if not 3 <= len(primary_path) <= 7:
            raise ValueError("planned architecture requires a 3-7 component path")
        if len(primary_path) != len(set(primary_path)) or any(
            item not in node_ids for item in primary_path
        ):
            raise ValueError("planned architecture primary path is invalid")
        edge_keys = {(edge["source"], edge["target"]) for edge in edges}
        if any(
            (source, target) not in edge_keys
            for source, target in zip(primary_path, primary_path[1:], strict=False)
        ):
            raise ValueError(
                "planned architecture primary path needs authored connections"
            )

        boundaries = []
        boundary_ids: set[str] = set()
        for raw in _bounded_items(
            data.get("boundaries") or (),
            limit=3,
            label="architecture boundaries",
        ):
            if not isinstance(raw, Mapping):
                raise ValueError("architecture boundary must be an object")
            boundary_id = _text(raw.get("id"), label="boundary id")
            if _NODE_ID.fullmatch(boundary_id) is None or boundary_id in boundary_ids:
                raise ValueError("architecture boundary id is invalid")
            boundary_ids.add(boundary_id)
            members = [
                _text(item, label="boundary member")
                for item in _bounded_items(
                    raw.get("members") or (),
                    limit=10,
                    label="boundary members",
                )
            ]
            if not members or any(item not in node_ids for item in members):
                raise ValueError("architecture boundary members are invalid")
            boundary_evidence = _evidence(raw.get("evidence"), allowed=allowed)
            if not boundary_evidence or not any(
                item.startswith("E") for item in boundary_evidence
            ):
                raise ValueError("architecture boundary requires evidence")
            boundaries.append(
                {
                    "id": boundary_id,
                    "label": _text(raw.get("label"), label="boundary label"),
                    "detail": _text(
                        raw.get("detail"),
                        label="boundary detail",
                        required=False,
                    ),
                    "members": list(dict.fromkeys(members)),
                    "evidence": boundary_evidence,
                }
            )
        result.update(
            {
                "primary_path": primary_path,
                "boundaries": boundaries,
            }
        )

    if adapter == "flow":
        incoming: dict[str, int] = {node_id: 0 for node_id in node_ids}
        outgoing: dict[str, int] = {node_id: 0 for node_id in node_ids}
        for edge in edges:
            outgoing[edge["source"]] += 1
            incoming[edge["target"]] += 1
        if any(value > 1 for value in (*incoming.values(), *outgoing.values())):
            raise ValueError("flow visual must be one directed path")
        starts = [node_id for node_id in node_ids if incoming[node_id] == 0]
        ends = [node_id for node_id in node_ids if outgoing[node_id] == 0]
        if len(starts) != 1 or len(ends) != 1 or len(edges) != len(nodes) - 1:
            raise ValueError("flow visual must connect every node in one path")
        seen = {starts[0]}
        cursor = starts[0]
        edges_by_source = {edge["source"]: edge for edge in edges}
        while cursor in edges_by_source:
            cursor = edges_by_source[cursor]["target"]
            if cursor in seen:
                raise ValueError("flow visual must not contain a cycle")
            seen.add(cursor)
        if len(seen) != len(nodes):
            raise ValueError("flow visual must connect every node in one path")
    return result


def _storyboard_data(
    data: Any,
    *,
    allowed: set[str] | None,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("storyboard visual data must be an object")
    raw_panels = data.get("panels") or ()
    panels = []
    for index, value in enumerate(
        _bounded_items(
            raw_panels,
            limit=_MAX_PANELS,
            label="storyboard panels",
        )
    ):
        if not isinstance(value, Mapping):
            raise ValueError("storyboard panel must be an object")
        panels.append(
            {
                "id": _text(value.get("id") or f"p{index + 1}", label="panel id"),
                "title": _text(value.get("title"), label="panel title"),
                "detail": _text(
                    value.get("detail"), label="panel detail", required=False
                ),
                "role": _text(value.get("role"), label="panel role", required=False),
                "evidence": _evidence(value.get("evidence"), allowed=allowed),
            }
        )
    if not 2 <= len(panels) <= _MAX_PANELS:
        raise ValueError(f"storyboard visual requires 2-{_MAX_PANELS} panels")
    panel_ids = [panel["id"] for panel in panels]
    if len(panel_ids) != len(set(panel_ids)):
        raise ValueError("storyboard panel ids must be unique")
    if any(_NODE_ID.fullmatch(panel_id) is None for panel_id in panel_ids):
        raise ValueError("storyboard panel ids must be stable identifiers")
    if any(not panel["evidence"] for panel in panels):
        raise ValueError("every storyboard panel requires evidence")
    return {"panels": panels}


def _bar_chart_data(
    data: Any,
    *,
    allowed: set[str] | None,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("bar-chart visual data must be an object")
    raw_bars = data.get("bars") or ()
    bars = []
    for value in _bounded_items(
        raw_bars,
        limit=_MAX_BARS,
        label="bar-chart bars",
    ):
        if not isinstance(value, Mapping):
            raise ValueError("bar-chart bar must be an object")
        raw_number = value.get("value")
        if isinstance(raw_number, bool):
            raise ValueError("bar-chart values must be finite numbers")
        try:
            number = float(raw_number)
        except (TypeError, ValueError) as exc:
            raise ValueError("bar-chart values must be finite numbers") from exc
        if not math.isfinite(number) or not 0 <= number <= 1e12:
            raise ValueError("bar-chart values must be finite non-negative numbers")
        bars.append(
            {
                "label": _text(value.get("label"), label="bar label"),
                "value": number,
                "evidence": _evidence(value.get("evidence"), allowed=allowed),
            }
        )
    if not 2 <= len(bars) <= _MAX_BARS:
        raise ValueError(f"bar-chart visual requires 2-{_MAX_BARS} bars")
    labels = [bar["label"].casefold() for bar in bars]
    if len(labels) != len(set(labels)):
        raise ValueError("bar-chart labels must be unique")
    if any(not bar["evidence"] for bar in bars):
        raise ValueError("every bar-chart value requires evidence")
    return {
        "unit": _text(data.get("unit"), label="chart unit", required=False),
        "bars": bars,
    }


def normalize_render_contract(
    value: Any,
    *,
    allowed_evidence: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate and canonicalize one renderer-specific visual contract."""

    if not isinstance(value, Mapping):
        raise ValueError("visual render contract must be an object")
    version = value.get("schema_version")
    if version != VISUAL_SCHEMA_VERSION:
        raise ValueError(f"visual schema_version must be {VISUAL_SCHEMA_VERSION}")
    adapter = _text(value.get("adapter"), label="adapter").casefold()
    if adapter not in VISUAL_ADAPTERS:
        raise ValueError(f"unsupported visual adapter: {adapter!r}")
    provenance = _text(value.get("provenance"), label="provenance").casefold()
    if provenance not in {
        "architecture-plan",
        "relations",
        "story",
        "metrics",
        "deterministic-index",
    }:
        raise ValueError(f"unsupported visual provenance: {provenance!r}")
    if provenance not in _ADAPTER_PROVENANCE[adapter]:
        raise ValueError(
            f"visual adapter {adapter!r} cannot use provenance {provenance!r}"
        )
    allowed = (
        {_text(item, label="allowed evidence") for item in allowed_evidence}
        if allowed_evidence is not None
        else None
    )
    evidence = _evidence(value.get("evidence"), allowed=allowed)
    if not evidence:
        raise ValueError("visual render contract requires evidence")

    if adapter in {"architecture", "flow"}:
        data = _graph_data(
            value.get("data"),
            adapter=adapter,
            provenance=provenance,
            allowed=allowed,
        )
    elif adapter == "storyboard":
        data = _storyboard_data(value.get("data"), allowed=allowed)
    else:
        data = _bar_chart_data(value.get("data"), allowed=allowed)
    normalized = {
        "schema_version": VISUAL_SCHEMA_VERSION,
        "adapter": adapter,
        "provenance": provenance,
        "evidence": evidence,
        "data": data,
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_CONTRACT_BYTES:
        raise ValueError("visual render contract exceeds the byte limit")
    return normalized


def render_contract_report(
    value: Any,
    *,
    allowed_evidence: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return review-friendly validation instead of raising."""

    try:
        normalized = normalize_render_contract(
            value,
            allowed_evidence=allowed_evidence,
        )
    except (TypeError, ValueError) as exc:
        return {
            "valid": False,
            "adapter": None,
            "errors": [str(exc)],
            "element_count": 0,
            "evidence_count": 0,
        }
    data = normalized["data"]
    if normalized["adapter"] in {"architecture", "flow"}:
        element_count = len(data["nodes"]) + len(data["edges"])
    elif normalized["adapter"] == "storyboard":
        element_count = len(data["panels"])
    else:
        element_count = len(data["bars"])
    return {
        "valid": True,
        "adapter": normalized["adapter"],
        "errors": [],
        "element_count": element_count,
        "evidence_count": len(normalized["evidence"]),
    }


def _endpoint_parts(value: str) -> tuple[str, str]:
    endpoint = re.sub(r"\s+", " ", str(value or "")).strip().strip("`")
    file, separator, symbol = endpoint.rpartition(":")
    if not separator:
        return endpoint, ""
    return symbol or endpoint, file


def architecture_contract_from_plan(value: Any) -> dict[str, Any] | None:
    """Compile an Overview's semantic architecture synthesis into visual IR."""

    if not isinstance(value, Mapping):
        return None
    value = repair_architecture_plan(value)
    try:
        raw_components = _bounded_items(
            value.get("components") or (),
            limit=10,
            label="architecture components",
        )
        raw_connections = _bounded_items(
            value.get("connections") or (),
            limit=_MAX_EDGES,
            label="architecture connections",
        )
        raw_boundaries = _bounded_items(
            value.get("boundaries") or (),
            limit=3,
            label="architecture boundaries",
        )
        primary_path = _bounded_items(
            value.get("primary_path") or (),
            limit=7,
            label="architecture primary path",
        )
    except ValueError:
        return None

    nodes = []
    edges = []
    boundaries = []
    evidence: list[str] = []

    def refs(raw: Any) -> list[str]:
        values = _evidence(raw)
        for item in values:
            if item not in evidence:
                evidence.append(item)
        return values

    try:
        for raw in raw_components:
            if not isinstance(raw, Mapping):
                return None
            nodes.append(
                {
                    "id": _text(raw.get("id"), label="component id"),
                    "label": _text(raw.get("label"), label="component label"),
                    "detail": _text(
                        raw.get("responsibility"),
                        label="component responsibility",
                    ),
                    "kind": _text(raw.get("kind"), label="component kind"),
                    "layer": _text(raw.get("layer"), label="component layer"),
                    "evidence": refs(raw.get("evidence")),
                }
            )
        for raw in raw_connections:
            if not isinstance(raw, Mapping):
                return None
            edges.append(
                {
                    "source": _text(
                        raw.get("source"),
                        label="connection source",
                    ),
                    "target": _text(
                        raw.get("target"),
                        label="connection target",
                    ),
                    "label": _text(
                        raw.get("label"),
                        label="connection label",
                    ),
                    "evidence": refs(raw.get("evidence")),
                }
            )
        for raw in raw_boundaries:
            if not isinstance(raw, Mapping):
                return None
            boundaries.append(
                {
                    "id": _text(raw.get("id"), label="boundary id"),
                    "label": _text(raw.get("label"), label="boundary label"),
                    "detail": _text(
                        raw.get("detail"),
                        label="boundary detail",
                        required=False,
                    ),
                    "members": list(raw.get("members") or ()),
                    "evidence": refs(raw.get("evidence")),
                }
            )
        return normalize_render_contract(
            {
                "schema_version": VISUAL_SCHEMA_VERSION,
                "adapter": "architecture",
                "provenance": "architecture-plan",
                "evidence": evidence,
                "data": {
                    "nodes": nodes,
                    "edges": edges,
                    "primary_path": list(primary_path),
                    "boundaries": boundaries,
                },
            }
        )
    except (TypeError, ValueError):
        return None


def architecture_contract_from_relations(
    relations: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Build a bounded architecture contract from static relation facts."""

    nodes: list[dict[str, Any]] = []
    node_ids: dict[str, str] = {}
    edges: list[dict[str, Any]] = []
    contract_evidence: list[str] = []

    def node_id(endpoint: str, refs: Sequence[str]) -> str:
        key = endpoint.casefold()
        existing = node_ids.get(key)
        if existing:
            node = next(item for item in nodes if item["id"] == existing)
            node["evidence"] = list(dict.fromkeys([*node["evidence"], *refs]))
            return existing
        identifier = f"n{len(nodes)}"
        label, detail = _endpoint_parts(endpoint)
        nodes.append(
            {
                "id": identifier,
                "label": label,
                "detail": detail,
                "evidence": list(refs),
            }
        )
        node_ids[key] = identifier
        return identifier

    try:
        relation_values = islice(iter(relations), _MAX_EDGES * 3)
    except TypeError:
        return None
    for raw in relation_values:
        if not isinstance(raw, Mapping) or len(edges) >= _MAX_EDGES:
            continue
        try:
            source = _text(raw.get("source"), label="relation source", required=False)
            target = _text(raw.get("target"), label="relation target", required=False)
        except ValueError:
            continue
        if not source or not target or source.casefold() == target.casefold():
            continue
        raw_refs = [raw.get("id")] if raw.get("id") else raw.get("anchors") or ()
        try:
            refs = _evidence(raw_refs)
        except ValueError:
            continue
        if not refs:
            continue
        new_endpoints = sum(
            endpoint.casefold() not in node_ids for endpoint in (source, target)
        )
        if len(nodes) + new_endpoints > _MAX_NODES:
            continue
        for ref in refs:
            if ref not in contract_evidence:
                contract_evidence.append(ref)
        source_id = node_id(source, refs)
        target_id = node_id(target, refs)
        edge_key = (source_id, target_id)
        if any((edge["source"], edge["target"]) == edge_key for edge in edges):
            continue
        edges.append(
            {
                "source": source_id,
                "target": target_id,
                "label": (
                    "calls"
                    if all(
                        "(" in _endpoint_parts(endpoint)[0]
                        and _endpoint_parts(endpoint)[0].endswith(")")
                        for endpoint in (source, target)
                    )
                    else "references"
                ),
                "evidence": refs,
            }
        )
    if len(nodes) < 2 or not edges or len(nodes) > _MAX_NODES:
        return None
    return normalize_render_contract(
        {
            "schema_version": VISUAL_SCHEMA_VERSION,
            "adapter": "architecture",
            "provenance": "relations",
            "evidence": contract_evidence,
            "data": {"nodes": nodes, "edges": edges},
        }
    )


def flow_contract_from_plan(flow: Any) -> dict[str, Any] | None:
    """Build a linear flow contract from an already admitted fact-plan path."""

    if not isinstance(flow, Mapping):
        return None
    nodes: list[dict[str, Any]] = []
    node_ids: dict[str, str] = {}
    edges: list[dict[str, Any]] = []
    contract_evidence: list[str] = []

    def node_id(endpoint: str, refs: Sequence[str]) -> str:
        key = endpoint.casefold()
        if key in node_ids:
            existing = node_ids[key]
            node = next(item for item in nodes if item["id"] == existing)
            node["evidence"] = list(dict.fromkeys([*node["evidence"], *refs]))
            return existing
        identifier = f"n{len(nodes)}"
        label, detail = _endpoint_parts(endpoint)
        nodes.append(
            {
                "id": identifier,
                "label": label,
                "detail": detail,
                "evidence": list(refs),
            }
        )
        node_ids[key] = identifier
        return identifier

    try:
        steps = _bounded_items(
            flow.get("steps") or (),
            limit=_MAX_FLOW_NODES - 1,
            label="flow steps",
        )
    except ValueError:
        return None
    for raw in steps:
        if not isinstance(raw, Mapping):
            continue
        try:
            source = _text(raw.get("from"), label="flow source", required=False)
            target = _text(raw.get("to"), label="flow target", required=False)
            refs = _evidence(raw.get("evidence"))
        except ValueError:
            continue
        if not source or not target or not refs:
            continue
        new_endpoints = sum(
            endpoint.casefold() not in node_ids for endpoint in (source, target)
        )
        if len(nodes) + new_endpoints > _MAX_FLOW_NODES:
            return None
        try:
            label = _text(raw.get("label"), label="flow label", required=False)
        except ValueError:
            continue
        source_id = node_id(source, refs)
        target_id = node_id(target, refs)
        edges.append(
            {
                "source": source_id,
                "target": target_id,
                "label": label,
                "evidence": refs,
            }
        )
        for ref in refs:
            if ref not in contract_evidence:
                contract_evidence.append(ref)
    if len(nodes) < 2 or not edges:
        return None
    try:
        return normalize_render_contract(
            {
                "schema_version": VISUAL_SCHEMA_VERSION,
                "adapter": "flow",
                "provenance": "relations",
                "evidence": contract_evidence,
                "data": {"nodes": nodes, "edges": edges},
            }
        )
    except ValueError:
        return None


def storyboard_contract_from_story(story: Any) -> dict[str, Any] | None:
    """Build storyboard panels from final story beats, never generic filler."""

    def compact_identifier_paths(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            raw = match.group("value")
            source, separator, symbol = raw.rpartition(":")
            if not separator or not symbol:
                return match.group(0)
            if "/" not in source and not re.search(
                r"\.(?:py|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|java|rb|php|cs|kt|kts)$",
                source,
                flags=re.IGNORECASE,
            ):
                return match.group(0)
            return f"`{symbol}`"

        return re.sub(r"`(?P<value>[^`\n]+)`", replace, value)

    def compact_title(value: str) -> str:
        return re.sub(
            r"^(?:orientation|entry|mechanism|decision|handoff|boundary|outcome)"
            r"\s*:\s*",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()

    if not isinstance(story, Mapping):
        return None
    panels = []
    evidence: list[str] = []
    try:
        beats = _bounded_items(
            story.get("beats") or (),
            limit=_MAX_PANELS * 4,
            label="story beats",
        )
    except ValueError:
        return None
    for index, beat in enumerate(beats):
        if not isinstance(beat, Mapping):
            continue
        try:
            refs = _evidence(beat.get("evidence"))
        except ValueError:
            continue
        if not refs:
            continue
        transition = beat.get("transition") or {}
        try:
            detail = (
                _text(
                    transition.get("statement"),
                    label="story transition",
                    required=False,
                )
                if isinstance(transition, Mapping)
                else ""
            )
            title = compact_title(_text(beat.get("section"), label="story section"))
            role = _text(beat.get("role"), label="story role", required=False)
        except ValueError:
            continue
        panels.append(
            {
                "id": f"p{index + 1}",
                "title": title,
                "detail": compact_identifier_paths(detail),
                "role": role,
                "evidence": refs,
            }
        )
        for ref in refs:
            if ref not in evidence:
                evidence.append(ref)
        if len(panels) > _MAX_PANELS:
            return None
    if len(panels) < 2:
        return None
    return normalize_render_contract(
        {
            "schema_version": VISUAL_SCHEMA_VERSION,
            "adapter": "storyboard",
            "provenance": "story",
            "evidence": evidence,
            "data": {"panels": panels},
        }
    )


def architecture_contract_from_mermaid(
    diagram: str,
    *,
    evidence: Iterable[str],
) -> dict[str, Any] | None:
    """Parse the small deterministic Mermaid subset emitted by WikiBuilder."""

    refs = _evidence(evidence)
    if not refs or len(str(diagram or "").encode("utf-8")) > _MAX_CONTRACT_BYTES:
        return None
    labels: dict[str, str] = {}
    raw_edges: list[tuple[str, str, str]] = []
    for line in str(diagram or "").splitlines():
        if re.match(r"^\s*(?:graph|flowchart)\s+(?:TD|TB|LR|RL)\s*$", line):
            continue
        edge = _MERMAID_EDGE.fullmatch(line)
        if edge:
            source = edge.group("source")
            target = edge.group("target")
            target_label = edge.group("target_label") or edge.group("target_single")
            if target_label:
                labels[target] = target_label
            raw_edges.append((source, target, edge.group("label") or ""))
            continue
        node = _MERMAID_NODE.fullmatch(line)
        if node:
            labels[node.group("id")] = node.group("label") or node.group("label_single")
    node_names = list(
        dict.fromkeys([endpoint for edge in raw_edges for endpoint in edge[:2]])
    )[:_MAX_NODES]
    node_set = set(node_names)
    edges = [
        {
            "source": source,
            "target": target,
            "label": label,
            "evidence": [],
        }
        for source, target, label in raw_edges[:_MAX_EDGES]
        if source in node_set and target in node_set and source != target
    ]
    nodes = []
    for node_id in node_names:
        label = re.sub(r"<br\s*/?>", " · ", labels.get(node_id, node_id), flags=re.I)
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "detail": "",
                "evidence": refs,
            }
        )
    if len(nodes) < 2 or not edges:
        return None
    try:
        return normalize_render_contract(
            {
                "schema_version": VISUAL_SCHEMA_VERSION,
                "adapter": "architecture",
                "provenance": "deterministic-index",
                "evidence": refs,
                "data": {"nodes": nodes, "edges": edges},
            }
        )
    except ValueError:
        return None


def bar_chart_contract(
    bars: Iterable[Mapping[str, Any]],
    *,
    evidence: Iterable[str],
    unit: str = "",
) -> dict[str, Any]:
    """Build an explicit chart contract for metrics with source provenance."""

    bounded_bars = _bounded_items(
        bars,
        limit=_MAX_BARS,
        label="bar-chart bars",
    )
    return normalize_render_contract(
        {
            "schema_version": VISUAL_SCHEMA_VERSION,
            "adapter": "bar-chart",
            "provenance": "metrics",
            "evidence": _evidence(evidence),
            "data": {"unit": unit, "bars": bounded_bars},
        }
    )


def page_visual_evidence_refs(page: Mapping[str, Any]) -> set[str]:
    """Return the evidence identifiers a page is allowed to cite visually."""

    evidence = page.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    allowed = {
        str(item.get("id") or "")
        for key in ("items", "relations")
        for item in (evidence.get(key) or ())
        if isinstance(item, Mapping) and item.get("id")
    }
    allowed.update(
        str(citation.get("file") or "")
        for citation in page.get("citations") or ()
        if isinstance(citation, Mapping) and citation.get("file")
    )
    return allowed


def page_visual_contract_report(page: Mapping[str, Any]) -> dict[str, Any]:
    """Audit every typed visual against evidence exposed by the page."""

    allowed = page_visual_evidence_refs(page)
    reports = []
    typed_slots = 0
    materialized_typed_slots = 0
    visible_assets = 0
    repository_assets = 0
    for slot in page.get("media_slots") or ():
        if not isinstance(slot, Mapping):
            continue
        asset = slot.get("asset")
        materialized = bool(
            isinstance(asset, Mapping) and str(asset.get("uri") or "").strip()
        )
        if materialized:
            visible_assets += 1
            if str(asset.get("provider") or "") == "repository":
                repository_assets += 1
        if "render_contract" not in slot:
            continue
        typed_slots += 1
        report = render_contract_report(
            slot.get("render_contract"),
            allowed_evidence=allowed,
        )
        if report["valid"]:
            expected_kind = VISUAL_ADAPTER_MEDIA_KIND[str(report["adapter"])]
            if str(slot.get("kind") or "") != expected_kind:
                report.update(
                    {
                        "valid": False,
                        "errors": [
                            f"visual adapter {report['adapter']!r} requires "
                            f"media kind {expected_kind!r}"
                        ],
                    }
                )
        report["slot_id"] = str(slot.get("id") or "")
        report["materialized"] = materialized
        if report["valid"] and materialized:
            materialized_typed_slots += 1
        reports.append(report)
    contract_valid = all(report["valid"] for report in reports)
    required = str(page.get("id") or "") == "overview"
    grounded_visuals = repository_assets + materialized_typed_slots
    errors = []
    if not contract_valid:
        errors.append("one or more typed visual contracts are invalid")
    if required and grounded_visuals == 0:
        errors.append("Overview has no materialized repository-owned or typed visual")
    return {
        "valid": contract_valid,
        "required": required,
        "publication_ready": not errors,
        "errors": errors,
        "typed_slots": typed_slots,
        "valid_typed_slots": sum(report["valid"] for report in reports),
        "materialized_typed_slots": materialized_typed_slots,
        "visible_assets": visible_assets,
        "repository_assets": repository_assets,
        "grounded_visuals": grounded_visuals,
        "adapters": list(
            dict.fromkeys(
                str(report["adapter"])
                for report in reports
                if report.get("valid") and report.get("adapter")
            )
        ),
        "contracts": copy.deepcopy(reports),
    }


__all__ = [
    "VISUAL_ADAPTERS",
    "VISUAL_ADAPTER_MEDIA_KIND",
    "VISUAL_SCHEMA_VERSION",
    "architecture_contract_from_mermaid",
    "architecture_contract_from_plan",
    "architecture_contract_from_relations",
    "bar_chart_contract",
    "flow_contract_from_plan",
    "normalize_render_contract",
    "page_visual_contract_report",
    "page_visual_evidence_refs",
    "render_contract_report",
    "storyboard_contract_from_story",
]
