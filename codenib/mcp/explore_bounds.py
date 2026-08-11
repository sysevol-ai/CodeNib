# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic byte bounds for composed MCP exploration responses.

MCP 2.x publishes a structured ``dict`` result twice: once as
``structuredContent`` and once as a pretty-printed text block for older
clients.  Measuring only the source dictionary therefore understates the
actual tool result substantially.  The helpers in this module reproduce that
conversion and project oversized explore responses before they reach the
transport.

The public limit applies to the serialized ``CallToolResult`` owned by
CodeNib.  A small reserve remains for protocol metadata and the JSON-RPC
envelope, whose request identifier is controlled by the client.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pydantic_core
from mcp.types import CallToolResult, TextContent

MAX_EXPLORE_CALL_RESULT_BYTES = 256 * 1024
MCP_ENVELOPE_RESERVE_BYTES = 4 * 1024
MAX_EXPLORE_PAYLOAD_BYTES = MAX_EXPLORE_CALL_RESULT_BYTES - MCP_ENVELOPE_RESERVE_BYTES

_RESPONSE_MEASURE = "mcp-call-result-json-utf8-v1"
_UTF8_LIMITS = {
    "backend": 512,
    "capability": 256,
    "code": 128,
    "commit": 512,
    "content_fingerprint": 512,
    "content_kind": 128,
    "detail": 512,
    "direction": 128,
    "error": 512,
    "fallback_reason": 512,
    "file": 4_096,
    "file_path": 4_096,
    "index_snapshot": 512,
    "kind": 128,
    "name": 1_024,
    "node_id": 1_024,
    "node_name": 1_024,
    "note": 512,
    "origin": 128,
    "origins": 128,
    "provider": 512,
    "query": 4_096,
    "repository": 4_096,
    "root": 1_024,
    "seed": 1_024,
    "seeds": 1_024,
    "source": 1_024,
    "source_fingerprint": 512,
    "symbol": 1_024,
    "symbols": 1_024,
    "target": 1_024,
    "type": 128,
    "verification_error": 512,
}
_DEFAULT_METADATA_UTF8_BYTES = 1_024
_MIN_RETAINED_CONTENT_BYTES = 512


@dataclass(slots=True)
class _ProjectionStats:
    metadata_fields_elided: int = 0
    metadata_bytes_omitted: int = 0
    relationships_summarized: int = 0
    relationship_nodes_omitted: int = 0
    relationship_edges_omitted: int = 0
    anchors_omitted: int = 0
    content_bodies_omitted: int = 0
    content_bodies_truncated: int = 0
    content_chars_omitted: int = 0
    content_bytes_omitted: int = 0
    contexts_omitted: int = 0
    minimal: bool = False

    @property
    def applied(self) -> bool:
        return any(
            (
                self.metadata_fields_elided,
                self.relationships_summarized,
                self.anchors_omitted,
                self.content_bodies_omitted,
                self.content_bodies_truncated,
                self.contexts_omitted,
                self.minimal,
            )
        )


def _projection_stats(response: Mapping[str, Any]) -> _ProjectionStats:
    """Carry prior projection accounting across repeated bounding passes."""

    summary = response.get("summary")
    if not isinstance(summary, Mapping):
        return _ProjectionStats()
    projection = summary.get("response_projection")
    if not isinstance(projection, Mapping):
        return _ProjectionStats()

    def count(name: str) -> int:
        value = projection.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    return _ProjectionStats(
        metadata_fields_elided=count("metadata_fields_elided"),
        metadata_bytes_omitted=count("metadata_bytes_omitted"),
        relationships_summarized=count("relationships_summarized"),
        relationship_nodes_omitted=count("relationship_nodes_omitted"),
        relationship_edges_omitted=count("relationship_edges_omitted"),
        anchors_omitted=count("anchors_omitted"),
        content_bodies_omitted=count("content_bodies_omitted"),
        content_bodies_truncated=count("content_bodies_truncated"),
        content_chars_omitted=count("content_chars_omitted"),
        content_bytes_omitted=count("content_bytes_omitted"),
        contexts_omitted=count("contexts_omitted"),
        minimal=projection.get("minimal") is True,
    )


def _as_call_tool_result(payload: Mapping[str, Any]) -> CallToolResult:
    """Mirror MCPServer's structured-dict conversion without private APIs."""

    structured = dict(payload)
    text = pydantic_core.to_json(
        structured,
        fallback=str,
        indent=2,
    ).decode()
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
    )


def call_tool_result_bytes(result: CallToolResult) -> int:
    """Return the serialized UTF-8 size of one MCP call-tool result."""

    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def explore_call_result_bytes(payload: Mapping[str, Any]) -> int:
    """Measure the structured and compatibility-text copies of *payload*."""

    return call_tool_result_bytes(_as_call_tool_result(payload))


def _utf8_prefix(value: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    return value.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def _elide_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    suffix = f"…#{digest}"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= maximum:
        return _utf8_prefix(suffix, maximum)
    return _utf8_prefix(value, maximum - suffix_bytes) + suffix


def _string_limit(key: str | None) -> int:
    if key is None:
        return _DEFAULT_METADATA_UTF8_BYTES
    return _UTF8_LIMITS.get(key, _DEFAULT_METADATA_UTF8_BYTES)


def _project_metadata_strings(
    value: Any,
    stats: _ProjectionStats,
    *,
    parent_key: str | None = None,
) -> Any:
    if isinstance(value, dict):
        for key in list(value):
            child = value[key]
            if key == "content" and isinstance(child, str):
                continue
            value[key] = _project_metadata_strings(
                child,
                stats,
                parent_key=str(key),
            )
        return value
    if isinstance(value, list):
        for index, child in enumerate(value):
            value[index] = _project_metadata_strings(
                child,
                stats,
                parent_key=parent_key,
            )
        return value
    if not isinstance(value, str):
        return value

    maximum = _string_limit(parent_key)
    projected = _elide_utf8(value, maximum)
    if projected != value:
        stats.metadata_fields_elided += 1
        stats.metadata_bytes_omitted += len(value.encode("utf-8")) - len(
            projected.encode("utf-8")
        )
    return projected


def _delivery(response: dict[str, Any]) -> dict[str, Any]:
    plan = response.setdefault("plan", {})
    if not isinstance(plan, dict):
        plan = {}
        response["plan"] = plan
    delivery = plan.setdefault("delivery", {})
    if not isinstance(delivery, dict):
        delivery = {}
        plan["delivery"] = delivery
    return delivery


def _summary(response: dict[str, Any]) -> dict[str, Any]:
    summary = response.setdefault("summary", {})
    if not isinstance(summary, dict):
        summary = {}
        response["summary"] = summary
    return summary


def _projection_summary(response: dict[str, Any]) -> dict[str, Any]:
    summary = _summary(response)
    projection = summary.setdefault("response_projection", {})
    if not isinstance(projection, dict):
        projection = {}
        summary["response_projection"] = projection
    return projection


def _sync_projection_summary(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
) -> None:
    projection = _projection_summary(response)
    encoded_bytes = projection.get("encoded_bytes", 0)
    projection.clear()
    projection.update(
        {
            "applied": stats.applied,
            "encoded_bytes": encoded_bytes,
            "max_bytes": max_bytes,
            "measure": _RESPONSE_MEASURE,
            "metadata_fields_elided": stats.metadata_fields_elided,
            "metadata_bytes_omitted": stats.metadata_bytes_omitted,
            "relationships_summarized": stats.relationships_summarized,
            "relationship_nodes_omitted": stats.relationship_nodes_omitted,
            "relationship_edges_omitted": stats.relationship_edges_omitted,
            "anchors_omitted": stats.anchors_omitted,
            "content_bodies_omitted": stats.content_bodies_omitted,
            "content_bodies_truncated": stats.content_bodies_truncated,
            "content_chars_omitted": stats.content_chars_omitted,
            "content_bytes_omitted": stats.content_bytes_omitted,
            "contexts_omitted": stats.contexts_omitted,
            "minimal": stats.minimal,
        }
    )


def _stamp_encoded_bytes(response: dict[str, Any]) -> int:
    projection = _projection_summary(response)
    for _ in range(12):
        measured = explore_call_result_bytes(response)
        if projection.get("encoded_bytes") == measured:
            return measured
        projection["encoded_bytes"] = measured
    measured = explore_call_result_bytes(response)
    projection["encoded_bytes"] = measured
    final = explore_call_result_bytes(response)
    if final != measured:  # pragma: no cover - integer width stabilizes quickly
        raise RuntimeError("explore response byte accounting did not stabilize")
    return final


def _iter_contexts(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    files = response.get("files")
    if not isinstance(files, list):
        return contexts
    for group in files:
        if not isinstance(group, dict):
            continue
        values = group.get("contexts")
        if not isinstance(values, list):
            continue
        contexts.extend(item for item in values if isinstance(item, dict))
    return contexts


def _returned_content(response: Mapping[str, Any]) -> tuple[int, int]:
    chars = 0
    encoded_bytes = 0
    for context in _iter_contexts(response):
        content = context.get("content")
        if not isinstance(content, str):
            continue
        chars += len(content)
        encoded_bytes += len(content.encode("utf-8"))
    return chars, encoded_bytes


def _refresh_returned_summary(response: dict[str, Any]) -> None:
    summary = _summary(response)
    returned_chars, returned_bytes = _returned_content(response)
    summary["returned_content_chars"] = returned_chars
    summary["returned_content_bytes"] = returned_bytes

    files = response.get("files")
    if isinstance(files, list):
        groups = [group for group in files if isinstance(group, dict)]
        summary["files"] = len(groups)
        summary["source_windows"] = sum(
            len(group.get("contexts", []))
            for group in groups
            if isinstance(group.get("contexts"), list)
        )


def _ensure_budget_diagnostic(response: dict[str, Any]) -> None:
    diagnostics = response.setdefault("diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []
        response["diagnostics"] = diagnostics
    if any(
        isinstance(item, Mapping) and item.get("code") == "response_budget_applied"
        for item in diagnostics
    ):
        return
    diagnostics.append(
        {
            "code": "response_budget_applied",
            "detail": (
                "The explore response was deterministically projected to its "
                "MCP call-result byte budget; see summary.response_projection."
            ),
        }
    )


def _summarize_relationships(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
) -> int:
    relationships = response.get("relationships")
    if not isinstance(relationships, list):
        return _stamp_encoded_bytes(response)

    measured = _stamp_encoded_bytes(response)
    for relationship in reversed(relationships):
        if measured <= max_bytes:
            break
        if not isinstance(relationship, dict):
            continue
        nodes = relationship.get("nodes")
        edges = relationship.get("edges")
        node_count = len(nodes) if isinstance(nodes, list) else 0
        edge_count = len(edges) if isinstance(edges, list) else 0
        if not node_count and not edge_count:
            continue

        relationship["nodes"] = []
        relationship["edges"] = []
        relationship["truncated"] = True
        relationship["response_projection"] = {
            "reason": "response_budget",
            "original_nodes": node_count,
            "original_edges": edge_count,
            "returned_nodes": 0,
            "returned_edges": 0,
        }
        stats.relationships_summarized += 1
        stats.relationship_nodes_omitted += node_count
        stats.relationship_edges_omitted += edge_count
        _sync_projection_summary(response, stats, max_bytes=max_bytes)
        measured = _stamp_encoded_bytes(response)
    return measured


def _trim_extra_anchors(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
) -> int:
    measured = _stamp_encoded_bytes(response)
    for context in reversed(_iter_contexts(response)):
        if measured <= max_bytes:
            break
        anchors = context.get("anchors")
        if not isinstance(anchors, list) or len(anchors) <= 1:
            continue
        stats.anchors_omitted += len(anchors) - 1
        context["anchors"] = anchors[:1]
        _sync_projection_summary(response, stats, max_bytes=max_bytes)
        measured = _stamp_encoded_bytes(response)
    return measured


def _mark_content_omitted(context: dict[str, Any], content: str) -> None:
    context.pop("content", None)
    context["content_kind"] = "response_budget_location"
    context["response_projection"] = {
        "truncated": True,
        "original_chars": len(content),
        "original_bytes": len(content.encode("utf-8")),
        "returned_chars": 0,
        "returned_bytes": 0,
        "strategy": "response_budget_metadata_only",
    }


def _omit_lower_ranked_bodies(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
    protected: dict[str, Any] | None,
) -> int:
    measured = _stamp_encoded_bytes(response)
    for context in reversed(_iter_contexts(response)):
        if measured <= max_bytes:
            break
        if context is protected:
            continue
        content = context.get("content")
        if not isinstance(content, str) or not content:
            continue
        _mark_content_omitted(context, content)
        stats.content_bodies_omitted += 1
        stats.content_chars_omitted += len(content)
        stats.content_bytes_omitted += len(content.encode("utf-8"))
        _refresh_returned_summary(response)
        _sync_projection_summary(response, stats, max_bytes=max_bytes)
        measured = _stamp_encoded_bytes(response)
    return measured


def _trim_remaining_anchors(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
    protected: dict[str, Any] | None,
) -> int:
    measured = _stamp_encoded_bytes(response)
    contexts = _iter_contexts(response)
    protected_anchor_kept = protected is not None
    for context in reversed(contexts):
        if measured <= max_bytes:
            break
        anchors = context.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            continue
        if context is protected and protected_anchor_kept:
            continue
        stats.anchors_omitted += len(anchors)
        context["anchors"] = []
        _sync_projection_summary(response, stats, max_bytes=max_bytes)
        measured = _stamp_encoded_bytes(response)

    if measured > max_bytes and protected is not None:
        anchors = protected.get("anchors")
        if isinstance(anchors, list) and anchors:
            stats.anchors_omitted += len(anchors)
            protected["anchors"] = []
            _sync_projection_summary(response, stats, max_bytes=max_bytes)
            measured = _stamp_encoded_bytes(response)
    return measured


def _truncate_protected_body(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
    protected: dict[str, Any] | None,
) -> int:
    measured = _stamp_encoded_bytes(response)
    if measured <= max_bytes or protected is None:
        return measured
    content = protected.get("content")
    if not isinstance(content, str) or not content:
        return measured

    original_chars = len(content)
    original_bytes = len(content.encode("utf-8"))
    base_truncated = stats.content_bodies_truncated
    base_chars = stats.content_chars_omitted
    base_bytes = stats.content_bytes_omitted
    budget = max(_MIN_RETAINED_CONTENT_BYTES, original_bytes // 2)

    while True:
        projected = _elide_utf8(content, budget)
        returned_bytes = len(projected.encode("utf-8"))
        protected["content"] = projected
        protected["response_projection"] = {
            "truncated": True,
            "original_chars": original_chars,
            "original_bytes": original_bytes,
            "returned_chars": len(projected),
            "returned_bytes": returned_bytes,
            "strategy": "response_budget_utf8",
        }
        stats.content_bodies_truncated = base_truncated + 1
        stats.content_chars_omitted = base_chars + original_chars - len(projected)
        stats.content_bytes_omitted = base_bytes + original_bytes - returned_bytes
        _refresh_returned_summary(response)
        _sync_projection_summary(response, stats, max_bytes=max_bytes)
        measured = _stamp_encoded_bytes(response)
        if measured <= max_bytes:
            return measured
        if budget <= _MIN_RETAINED_CONTENT_BYTES:
            break
        budget = max(_MIN_RETAINED_CONTENT_BYTES, budget // 2)

    stats.content_bodies_truncated = base_truncated
    stats.content_chars_omitted = base_chars + original_chars
    stats.content_bytes_omitted = base_bytes + original_bytes
    stats.content_bodies_omitted += 1
    _mark_content_omitted(protected, content)
    _refresh_returned_summary(response)
    _sync_projection_summary(response, stats, max_bytes=max_bytes)
    return _stamp_encoded_bytes(response)


def _drop_tail_contexts(
    response: dict[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
    protected: dict[str, Any] | None,
) -> int:
    measured = _stamp_encoded_bytes(response)
    files = response.get("files")
    if not isinstance(files, list):
        return measured

    for group in reversed(files):
        if measured <= max_bytes:
            break
        if not isinstance(group, dict):
            continue
        contexts = group.get("contexts")
        if not isinstance(contexts, list):
            continue
        for index in range(len(contexts) - 1, -1, -1):
            if measured <= max_bytes:
                break
            context = contexts[index]
            if context is protected:
                continue
            if isinstance(context, Mapping):
                anchors = context.get("anchors")
                if isinstance(anchors, list):
                    stats.anchors_omitted += len(anchors)
            del contexts[index]
            stats.contexts_omitted += 1
            _refresh_returned_summary(response)
            _sync_projection_summary(response, stats, max_bytes=max_bytes)
            measured = _stamp_encoded_bytes(response)
    response["files"] = [
        group
        for group in files
        if not isinstance(group, Mapping)
        or not isinstance(group.get("contexts"), list)
        or group["contexts"]
    ]
    _refresh_returned_summary(response)
    _sync_projection_summary(response, stats, max_bytes=max_bytes)
    return _stamp_encoded_bytes(response)


def _source_provenance(response: Mapping[str, Any]) -> dict[str, Any]:
    source = response.get("source")
    if not isinstance(source, Mapping):
        return {}
    return {
        key: source[key]
        for key in (
            "repository",
            "commit",
            "source_fingerprint",
            "verified",
        )
        if key in source
    }


def _session_usage(response: Mapping[str, Any]) -> dict[str, Any]:
    summary = response.get("summary")
    if not isinstance(summary, Mapping):
        return {}
    return {
        key: summary[key]
        for key in (
            "session_call",
            "session_deduplicated_contexts",
            "session_ranges",
            "session_max_ranges",
            "session_remaining_ranges",
            "session_evicted_ranges",
            "session_total_evictions",
        )
        if key in summary
    }


def _minimal_response(
    response: Mapping[str, Any],
    stats: _ProjectionStats,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    stats.minimal = True
    query = response.get("query")
    query_fingerprint = (
        f"sha256:{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
        if isinstance(query, str)
        else None
    )
    plan = response.get("plan") if isinstance(response.get("plan"), Mapping) else {}
    original_delivery = (
        plan.get("delivery") if isinstance(plan.get("delivery"), Mapping) else {}
    )
    minimal_delivery = {
        "max_response_bytes": max_bytes,
        "response_measure": _RESPONSE_MEASURE,
    }
    for key in ("session_dedup", "session_scope", "session_max_ranges"):
        if key in original_delivery:
            minimal_delivery[key] = original_delivery[key]
    minimal_summary = {
        "files": 0,
        "source_windows": 0,
        "returned_content_chars": 0,
        "returned_content_bytes": 0,
        **_session_usage(response),
    }
    minimal: dict[str, Any] = {
        "plan": {
            "name": plan.get("name", "composed_explore_v1"),
            "budget": plan.get("budget"),
            "delivery": minimal_delivery,
        },
        "source": _source_provenance(response),
        "summary": minimal_summary,
        "files": [],
        "relationships": [],
        "diagnostics": [
            {
                "code": "response_budget_exhausted",
                "detail": (
                    "Only the minimal explore envelope fit the MCP call-result "
                    "byte budget."
                ),
            }
        ],
    }
    if query_fingerprint is not None:
        minimal["query_fingerprint"] = query_fingerprint
    _project_metadata_strings(minimal, stats)
    _sync_projection_summary(minimal, stats, max_bytes=max_bytes)
    if _stamp_encoded_bytes(minimal) <= max_bytes:
        return minimal

    ultra = {
        "plan": {
            "delivery": {
                "max_response_bytes": max_bytes,
                "response_measure": _RESPONSE_MEASURE,
            }
        },
        "summary": {
            "returned_content_chars": 0,
            "returned_content_bytes": 0,
        },
        "diagnostics": [
            {
                "code": "response_budget_exhausted",
                "detail": "The explore response was reduced to its minimal envelope.",
            }
        ],
    }
    _sync_projection_summary(ultra, stats, max_bytes=max_bytes)
    if _stamp_encoded_bytes(ultra) > max_bytes:
        raise ValueError("max_bytes is too small for the minimal explore envelope")
    return ultra


def bound_explore_response(
    payload: Mapping[str, Any],
    *,
    max_bytes: int = MAX_EXPLORE_PAYLOAD_BYTES,
) -> dict[str, Any]:
    """Return a deterministic response whose MCP call-result fits *max_bytes*.

    Relationships are reduced atomically to header-only summaries before any
    source body is removed.  Extra anchors are lower priority than returned
    source bodies, and the highest-ranked body is the last evidence removed.
    The input mapping is never mutated.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")

    response = copy.deepcopy(dict(payload))
    stats = _projection_stats(response)
    delivery = _delivery(response)
    delivery["max_response_bytes"] = max_bytes
    delivery["response_measure"] = _RESPONSE_MEASURE
    _refresh_returned_summary(response)
    _sync_projection_summary(response, stats, max_bytes=max_bytes)
    measured = _stamp_encoded_bytes(response)
    if measured <= max_bytes:
        return response

    query = response.get("query")
    _project_metadata_strings(response, stats)
    if isinstance(query, str) and response.get("query") != query:
        response["query_fingerprint"] = (
            f"sha256:{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
        )
        response["query_projection"] = {
            "truncated": True,
            "original_bytes": len(query.encode("utf-8")),
            "returned_bytes": len(str(response["query"]).encode("utf-8")),
            "strategy": "utf8_prefix_digest",
        }
    _ensure_budget_diagnostic(response)
    _refresh_returned_summary(response)
    _sync_projection_summary(response, stats, max_bytes=max_bytes)

    measured = _summarize_relationships(response, stats, max_bytes=max_bytes)
    if measured > max_bytes:
        measured = _trim_extra_anchors(response, stats, max_bytes=max_bytes)

    contexts = _iter_contexts(response)
    protected = next(
        (
            context
            for context in contexts
            if isinstance(context.get("content"), str) and context.get("content")
        ),
        None,
    )
    if measured > max_bytes:
        measured = _omit_lower_ranked_bodies(
            response,
            stats,
            max_bytes=max_bytes,
            protected=protected,
        )
    if measured > max_bytes:
        measured = _trim_remaining_anchors(
            response,
            stats,
            max_bytes=max_bytes,
            protected=protected,
        )
    if measured > max_bytes:
        measured = _truncate_protected_body(
            response,
            stats,
            max_bytes=max_bytes,
            protected=protected,
        )
    if measured > max_bytes:
        measured = _drop_tail_contexts(
            response,
            stats,
            max_bytes=max_bytes,
            protected=protected,
        )

    _refresh_returned_summary(response)
    _sync_projection_summary(response, stats, max_bytes=max_bytes)
    measured = _stamp_encoded_bytes(response)
    if measured > max_bytes:
        response = _minimal_response(response, stats, max_bytes=max_bytes)
        measured = _stamp_encoded_bytes(response)
    if measured > max_bytes:  # pragma: no cover - guarded by minimal response
        raise RuntimeError("bounded explore response exceeds its byte budget")
    return response


__all__ = [
    "MAX_EXPLORE_CALL_RESULT_BYTES",
    "MAX_EXPLORE_PAYLOAD_BYTES",
    "MCP_ENVELOPE_RESERVE_BYTES",
    "bound_explore_response",
    "call_tool_result_bytes",
    "explore_call_result_bytes",
]
