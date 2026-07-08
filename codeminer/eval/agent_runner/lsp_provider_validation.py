# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compare static-index LSP calls against a reference LSP provider."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

from codeminer.agent.lsp_provider import (
    CAPABILITY_DEFINITION,
    CAPABILITY_REFERENCES,
    CAPABILITY_ROUTE,
    JSON_RPC_LSP_PROVIDER,
    STATIC_LSP_PROVIDER,
    StaticLSPProvider,
    fingerprint_lsp_result,
    lsp_result_metadata,
    normalize_lsp_capability,
    preview_lsp_result,
)

ProviderCallable = Callable[[str, Mapping[str, Any]], Any]
FingerprintFn = Callable[[Sequence[Any]], str]
Clock = Callable[[], float]


@dataclass(frozen=True)
class LSPProviderRequest:
    """One graph-facing LSP request to replay across providers."""

    capability: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None

    @property
    def normalized_capability(self) -> str:
        return normalize_lsp_capability(self.capability)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "capability": self.normalized_capability,
            "arguments": dict(self.arguments),
        }
        if self.request_id is not None:
            out["request_id"] = self.request_id
        return out


@dataclass(frozen=True)
class LSPProviderCall:
    """Observed result and latency for one provider call."""

    provider: str
    capability: str
    status: str
    duration_ms: Optional[float] = None
    result_count: Optional[int] = None
    result_fingerprint: Optional[str] = None
    result_preview: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    fallback_reason: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "capability": self.capability,
            "status": self.status,
        }
        if self.duration_ms is not None:
            out["duration_ms"] = self.duration_ms
        if self.result_count is not None:
            out["result_count"] = self.result_count
        if self.result_fingerprint is not None:
            out["result_fingerprint"] = self.result_fingerprint
        if self.result_preview:
            out["result_preview"] = [dict(item) for item in self.result_preview]
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        if self.fallback_reason:
            out["fallback_reason"] = self.fallback_reason
        if self.error:
            out["error"] = self.error
        return out


@dataclass(frozen=True)
class LSPProviderComparison:
    """Static-index vs reference-provider comparison for one request."""

    request: LSPProviderRequest
    static_call: LSPProviderCall
    reference_call: LSPProviderCall
    same_result: Optional[bool]
    verdict: str
    latency_saved_ms: Optional[float] = None
    speedup_ratio: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "request": self.request.to_dict(),
            "static_call": self.static_call.to_dict(),
            "reference_call": self.reference_call.to_dict(),
            "same_result": self.same_result,
            "verdict": self.verdict,
        }
        if self.latency_saved_ms is not None:
            out["latency_saved_ms"] = self.latency_saved_ms
        if self.speedup_ratio is not None:
            out["speedup_ratio"] = self.speedup_ratio
        return out


@dataclass(frozen=True)
class LSPProviderValidationSummary:
    """Machine-readable gate summary for static-vs-reference validation."""

    total: int
    verdict_counts: Mapping[str, int]
    same_result_count: int
    mismatch_count: int
    fallback_count: int
    error_count: int
    static_faster_count: int
    static_not_faster_count: int
    promotion_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "verdict_counts": dict(self.verdict_counts),
            "same_result_count": self.same_result_count,
            "mismatch_count": self.mismatch_count,
            "fallback_count": self.fallback_count,
            "error_count": self.error_count,
            "static_faster_count": self.static_faster_count,
            "static_not_faster_count": self.static_not_faster_count,
            "promotion_ready": self.promotion_ready,
        }


def compare_static_lsp_provider(
    requests: Iterable[LSPProviderRequest | Mapping[str, Any]],
    *,
    graph: Any,
    reference_provider: ProviderCallable,
    reference_provider_name: str = JSON_RPC_LSP_PROVIDER,
    static_provider: Optional[StaticLSPProvider] = None,
    fingerprint_fn: FingerprintFn = fingerprint_lsp_result,
    clock: Clock = time.monotonic,
) -> List[LSPProviderComparison]:
    """Replay the same LSP requests through static and reference providers.

    ``arguments`` are the graph-facing arguments consumed by skill executors.
    For agent traces, prefer ``resolved_arguments`` when present because the
    runner has already converted line-number inputs across the agent boundary.
    """

    provider = static_provider or StaticLSPProvider(graph)
    comparisons: List[LSPProviderComparison] = []
    for raw_request in requests:
        request = _coerce_request(raw_request)
        static_call = _call_static_provider(
            provider,
            request,
            fingerprint_fn=fingerprint_fn,
            clock=clock,
        )
        reference_call = _call_reference_provider(
            reference_provider,
            request,
            provider_name=reference_provider_name,
            fingerprint_fn=fingerprint_fn,
            clock=clock,
        )
        same_result = _same_fingerprint(static_call, reference_call)
        latency_saved_ms = _latency_saved_ms(static_call, reference_call)
        comparisons.append(
            LSPProviderComparison(
                request=request,
                static_call=static_call,
                reference_call=reference_call,
                same_result=same_result,
                verdict=_verdict(
                    static_call,
                    reference_call,
                    same_result=same_result,
                    latency_saved_ms=latency_saved_ms,
                ),
                latency_saved_ms=latency_saved_ms,
                speedup_ratio=_speedup_ratio(static_call, reference_call),
            )
        )
    return comparisons


def summarize_lsp_provider_validation(
    comparisons: Sequence[LSPProviderComparison | Mapping[str, Any]],
) -> LSPProviderValidationSummary:
    """Summarize whether static provider rows are ready for fast-path routing."""

    rows = [_comparison_dict(row) for row in comparisons]
    verdicts = Counter(str(row.get("verdict") or "unknown") for row in rows)
    error_count = sum(
        1
        for row in rows
        if str(row.get("verdict") or "")
        in {"static_error", "reference_error", "unknown"}
    )
    same_result_count = sum(1 for row in rows if row.get("same_result") is True)
    mismatch_count = verdicts.get("mismatch", 0)
    fallback_count = verdicts.get("fallback_required", 0)
    static_faster_count = verdicts.get("equivalent_static_faster", 0)
    static_not_faster_count = verdicts.get("equivalent_static_not_faster", 0)
    promotion_ready = bool(rows) and static_faster_count == len(rows)
    return LSPProviderValidationSummary(
        total=len(rows),
        verdict_counts=dict(sorted(verdicts.items())),
        same_result_count=same_result_count,
        mismatch_count=mismatch_count,
        fallback_count=fallback_count,
        error_count=error_count,
        static_faster_count=static_faster_count,
        static_not_faster_count=static_not_faster_count,
        promotion_ready=promotion_ready,
    )


def render_lsp_provider_validation_markdown(
    comparisons: Sequence[LSPProviderComparison | Mapping[str, Any]],
) -> str:
    """Render provider comparisons as a compact markdown table."""

    lines = ["# LSP provider validation", ""]
    lines.append(
        "Each row replays one graph-facing LSP request through CodeMiner's "
        "static index and a reference provider such as JSON-RPC LSP."
    )
    lines.append("")
    head = [
        "request",
        "capability",
        "verdict",
        "same result",
        "static ms",
        "reference ms",
        "saved ms",
        "static provider",
        "fallback",
    ]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("| " + " | ".join("---" for _ in head) + " |")
    for comparison in comparisons:
        row = _comparison_dict(comparison)
        request = row["request"]
        static_call = row["static_call"]
        reference_call = row["reference_call"]
        rendered = [
            str(request.get("request_id") or ""),
            str(request.get("capability") or ""),
            str(row.get("verdict") or ""),
            _yes_no(row.get("same_result")),
            _fmt(static_call.get("duration_ms"), 2),
            _fmt(reference_call.get("duration_ms"), 2),
            _fmt(row.get("latency_saved_ms"), 2),
            str(static_call.get("provider") or ""),
            str(static_call.get("fallback_reason") or ""),
        ]
        lines.append("| " + " | ".join(rendered) + " |")
    lines.append("")
    summary = summarize_lsp_provider_validation(comparisons)
    lines.append(f"Promotion ready: {_yes_no(summary.promotion_ready)}")
    lines.append("")
    return "\n".join(lines)


def _call_static_provider(
    provider: StaticLSPProvider,
    request: LSPProviderRequest,
    *,
    fingerprint_fn: FingerprintFn,
    clock: Clock,
) -> LSPProviderCall:
    decision = provider.can_serve(request.capability)
    capability = request.normalized_capability
    metadata = decision.to_dict()
    if decision.status != "ok":
        return LSPProviderCall(
            provider=STATIC_LSP_PROVIDER,
            capability=capability,
            status=decision.status,
            metadata=metadata,
            fallback_reason=decision.fallback_reason,
        )
    return _timed_call(
        provider_name=STATIC_LSP_PROVIDER,
        capability=capability,
        call=lambda: _invoke_static(provider, request),
        fingerprint_fn=fingerprint_fn,
        clock=clock,
        default_metadata=metadata,
    )


def _call_reference_provider(
    provider: ProviderCallable,
    request: LSPProviderRequest,
    *,
    provider_name: str,
    fingerprint_fn: FingerprintFn,
    clock: Clock,
) -> LSPProviderCall:
    capability = request.normalized_capability
    return _timed_call(
        provider_name=provider_name,
        capability=capability,
        call=lambda: provider(capability, dict(request.arguments)),
        fingerprint_fn=fingerprint_fn,
        clock=clock,
        default_metadata={},
    )


def _timed_call(
    *,
    provider_name: str,
    capability: str,
    call: Callable[[], Any],
    fingerprint_fn: FingerprintFn,
    clock: Clock,
    default_metadata: Mapping[str, Any],
) -> LSPProviderCall:
    start = clock()
    try:
        result = call()
        duration_ms = (clock() - start) * 1000
        status, error = _result_status(result)
        items = _result_items(result)
        fingerprint = fingerprint_fn(items) if items is not None else None
        preview = preview_lsp_result(items) if items is not None else []
        metadata = lsp_result_metadata(result) or dict(default_metadata)
        return LSPProviderCall(
            provider=str(metadata.get("provider") or provider_name),
            capability=capability,
            status=status,
            duration_ms=duration_ms,
            result_count=len(items) if items is not None else None,
            result_fingerprint=fingerprint,
            result_preview=tuple(preview),
            metadata=metadata,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001 - comparison rows must be complete.
        duration_ms = (clock() - start) * 1000
        return LSPProviderCall(
            provider=provider_name,
            capability=capability,
            status="error",
            duration_ms=duration_ms,
            metadata=dict(default_metadata),
            error=f"{type(exc).__name__}: {exc}",
        )


def _invoke_static(provider: StaticLSPProvider, request: LSPProviderRequest) -> Any:
    args = dict(request.arguments)
    capability = request.normalized_capability
    if capability == CAPABILITY_DEFINITION:
        return provider.definition(**args)
    if capability == CAPABILITY_REFERENCES:
        return provider.references(**args)
    if capability == CAPABILITY_ROUTE:
        return provider.route(**args)
    raise ValueError(f"unsupported LSP capability: {request.capability!r}")


def _coerce_request(raw: LSPProviderRequest | Mapping[str, Any]) -> LSPProviderRequest:
    if isinstance(raw, LSPProviderRequest):
        return raw
    capability = str(raw.get("capability") or raw.get("lsp_method") or "")
    args = raw.get("arguments") or raw.get("resolved_arguments") or {}
    return LSPProviderRequest(
        capability=capability,
        arguments=dict(args) if isinstance(args, Mapping) else {},
        request_id=raw.get("request_id"),
    )


def _result_items(result: Any) -> Optional[List[Any]]:
    status, _ = _result_status(result)
    if status == "error":
        return None
    if result is None:
        return []
    if isinstance(result, list):
        return list(result)
    if isinstance(result, tuple):
        return list(result)
    if isinstance(result, Mapping):
        return [dict(result)]
    return [result]


def _result_status(result: Any) -> tuple[str, Optional[str]]:
    if isinstance(result, Mapping) and result.get("error"):
        return "error", str(result.get("error"))
    return "ok", None


def fingerprint_lsp_start_locations(nodes: Sequence[Any]) -> str:
    """Fingerprint LSP results by repo-relative file and start line only.

    This is the strictest equivalence mode shared by live JSON-RPC LSP and
    CodeMiner's static graph today. Static graph results may carry richer symbol
    names and enclosing ranges than a language server's definition response, so
    full-result fingerprints can be too strict for the first acceleration gate.
    """

    payload = []
    for node in nodes:
        data = _node_dict(node)
        payload.append(
            {
                "file": str(data.get("file") or data.get("file_path") or ""),
                "start_line": _coerce_int(data.get("start_line")),
            }
        )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_fingerprint(
    static_call: LSPProviderCall,
    reference_call: LSPProviderCall,
) -> Optional[bool]:
    if static_call.status != "ok" or reference_call.status != "ok":
        return None
    if (
        static_call.result_fingerprint is None
        or reference_call.result_fingerprint is None
    ):
        return None
    return static_call.result_fingerprint == reference_call.result_fingerprint


def _latency_saved_ms(
    static_call: LSPProviderCall,
    reference_call: LSPProviderCall,
) -> Optional[float]:
    if static_call.duration_ms is None or reference_call.duration_ms is None:
        return None
    return reference_call.duration_ms - static_call.duration_ms


def _speedup_ratio(
    static_call: LSPProviderCall,
    reference_call: LSPProviderCall,
) -> Optional[float]:
    if static_call.duration_ms is None or reference_call.duration_ms is None:
        return None
    if static_call.duration_ms <= 0:
        return None
    return reference_call.duration_ms / static_call.duration_ms


def _verdict(
    static_call: LSPProviderCall,
    reference_call: LSPProviderCall,
    *,
    same_result: Optional[bool],
    latency_saved_ms: Optional[float],
) -> str:
    if static_call.status in {"unsupported", "unavailable"}:
        return "fallback_required"
    if static_call.status != "ok":
        return "static_error"
    if reference_call.status != "ok":
        return "reference_error"
    if same_result is False:
        return "mismatch"
    if same_result is True and latency_saved_ms is not None and latency_saved_ms >= 0:
        return "equivalent_static_faster"
    if same_result is True:
        return "equivalent_static_not_faster"
    return "unknown"


def _comparison_dict(
    comparison: LSPProviderComparison | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(comparison, LSPProviderComparison):
        return comparison.to_dict()
    return dict(comparison)


def _node_dict(node: Any) -> dict[str, Any]:
    if isinstance(node, Mapping):
        return dict(node)
    if hasattr(node, "model_dump"):
        return node.model_dump(exclude_none=True)
    if hasattr(node, "__dict__"):
        return dict(node.__dict__)
    return {"node_name": str(node)}


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, digits: int) -> str:
    return f"{float(value):.{digits}f}" if isinstance(value, (int, float)) else "n/a"


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


__all__ = [
    "LSPProviderCall",
    "LSPProviderComparison",
    "LSPProviderRequest",
    "LSPProviderValidationSummary",
    "compare_static_lsp_provider",
    "fingerprint_lsp_start_locations",
    "render_lsp_provider_validation_markdown",
    "summarize_lsp_provider_validation",
]
