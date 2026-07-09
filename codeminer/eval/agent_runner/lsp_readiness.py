# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Behavioral readiness checks for live LSP reference providers."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from codeminer.agent.lsp_provider import JSON_RPC_LSP_PROVIDER, StaticLSPProvider

from .lsp_provider_validation import (
    FingerprintSelector,
    LSPProviderRequest,
    compare_static_lsp_provider,
    default_lsp_provider_fingerprint,
)

Clock = Callable[[], float]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class LSPProviderReadiness:
    """Observed warmup rows and convergence state for one live provider."""

    rows: Sequence[Mapping[str, Any]]
    completed_reps: int
    stable: bool
    live_nonempty_count: int
    equivalent_count: int
    wall_ms: float


def wait_for_lsp_provider_readiness(
    requests: Sequence[LSPProviderRequest],
    *,
    graph: Any,
    static_provider: StaticLSPProvider,
    live_provider: Any,
    minimum_reps: int = 2,
    until_stable: bool = True,
    max_reps: int = 10,
    poll_seconds: float = 1.0,
    min_live_nonempty_fraction: float = 0.1,
    minimum_equivalent_count: int = 0,
    fingerprint_selector: FingerprintSelector = default_lsp_provider_fingerprint,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> LSPProviderReadiness:
    """Replay fixed requests until live LSP behavior converges.

    Process startup is not a readiness signal: language servers commonly accept
    JSON-RPC while workspace analysis is still changing their answers. Readiness
    therefore requires two consecutive identical live-result signatures, no
    provider errors, and a minimum non-empty response fraction.
    """

    if minimum_reps < 0:
        raise ValueError("minimum_reps must be non-negative")
    if max_reps < max(1, minimum_reps):
        raise ValueError("max_reps must be at least minimum_reps")
    if not 0 <= min_live_nonempty_fraction <= 1:
        raise ValueError("min_live_nonempty_fraction must be between 0 and 1")
    if minimum_equivalent_count < 0:
        raise ValueError("minimum_equivalent_count must be non-negative")
    if minimum_equivalent_count > len(requests):
        raise ValueError("minimum_equivalent_count exceeds request count")

    rows: list[Mapping[str, Any]] = []
    limit = max_reps if until_stable else minimum_reps
    previous_signature: Optional[tuple[Any, ...]] = None
    stable = not until_stable
    live_nonempty_count = 0
    equivalent_count = 0
    completed_reps = 0
    start = clock()

    for rep in range(1, limit + 1):
        comparisons = compare_static_lsp_provider(
            requests,
            graph=graph,
            static_provider=static_provider,
            reference_provider=live_provider,
            reference_provider_name=JSON_RPC_LSP_PROVIDER,
            fingerprint_selector=fingerprint_selector,
            clock=clock,
        )
        for comparison in comparisons:
            row = comparison.to_dict()
            row["rep"] = rep
            rows.append(row)
        completed_reps = rep
        signature, live_nonempty_count, error_count = _reference_state(comparisons)
        equivalent_count = sum(
            comparison.same_result is True for comparison in comparisons
        )
        minimum_nonempty = math.ceil(len(requests) * min_live_nonempty_fraction)
        converged = (
            previous_signature == signature
            and error_count == 0
            and live_nonempty_count >= minimum_nonempty
            and equivalent_count >= minimum_equivalent_count
        )
        if until_stable and rep >= minimum_reps and converged:
            stable = True
            break
        previous_signature = signature
        if until_stable and rep < limit:
            sleep(max(0.0, poll_seconds))

    wall_ms = (clock() - start) * 1000
    if not stable:
        raise RuntimeError(
            "live reference results did not stabilize after "
            f"{completed_reps} warmup reps "
            f"(nonempty={live_nonempty_count}/{len(requests)}, "
            f"equivalent={equivalent_count}/{len(requests)})"
        )
    return LSPProviderReadiness(
        rows=rows,
        completed_reps=completed_reps,
        stable=stable,
        live_nonempty_count=live_nonempty_count,
        equivalent_count=equivalent_count,
        wall_ms=wall_ms,
    )


def _reference_state(
    comparisons: Sequence[Any],
) -> tuple[tuple[Any, ...], int, int]:
    signature = []
    nonempty_count = 0
    error_count = 0
    for comparison in comparisons:
        row = comparison.to_dict()
        request = row.get("request") or {}
        reference = row.get("reference_call") or {}
        status = str(reference.get("status") or "unknown")
        result_count = int(reference.get("result_count") or 0)
        nonempty_count += int(status == "ok" and result_count > 0)
        error_count += int(status != "ok")
        signature.append(
            (
                str(request.get("request_id") or ""),
                status,
                reference.get("result_fingerprint"),
                result_count,
            )
        )
    return tuple(signature), nonempty_count, error_count


__all__ = ["LSPProviderReadiness", "wait_for_lsp_provider_readiness"]
