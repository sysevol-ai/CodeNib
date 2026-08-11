# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for immutable per-file semantic facts."""

from __future__ import annotations

import pytest

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    COMPLETENESS_FAILED,
    COMPLETENESS_PARTIAL,
    PROVENANCE_HEURISTIC,
    PROVENANCE_SCIP,
    DiagnosticFact,
    EdgeFact,
    FactBatch,
    OccurrenceFact,
    SourceRange,
    SymbolFact,
    merge_fact_batches,
    sha256_digest,
)
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION

_CONTENT_DIGEST = sha256_digest("def run(): pass\n")
_PROFILE_DIGEST = sha256_digest("scip-profile-v1")


def _range(line: int, start: int = 0, end: int = 3) -> SourceRange:
    return SourceRange(line, start, line, end)


def _symbol(symbol_id: str, line: int) -> SymbolFact:
    return SymbolFact(
        symbol_id=symbol_id,
        moniker=f"scip python project 1 pkg/{symbol_id}().",
        display_name=symbol_id,
        kind=NODE_TYPE_FUNCTION,
        definition_range=_range(line),
    )


def _batch(*, reverse: bool = False) -> FactBatch:
    symbols = (_symbol("alpha", 1), _symbol("beta", 5))
    occurrences = (
        OccurrenceFact(symbol_moniker="beta", source_range=_range(8), roles=0),
        OccurrenceFact(symbol_moniker="alpha", source_range=_range(1), roles=1),
    )
    edges = (
        EdgeFact(
            kind=EDGE_TYPE_REFERENCE,
            provenance=PROVENANCE_SCIP,
            source_symbol_id="alpha",
            target_moniker="beta",
            anchor_range=_range(3),
        ),
    )
    return FactBatch(
        file_path="src/main.py",
        language="python",
        content_digest=_CONTENT_DIGEST,
        profile_digest=_PROFILE_DIGEST,
        provider="scip-python",
        completeness=COMPLETENESS_PARTIAL,
        capabilities=(
            CAPABILITY_OCCURRENCES,
            CAPABILITY_DEFINITIONS,
            CAPABILITY_EDGES,
        ),
        symbols=tuple(reversed(symbols)) if reverse else symbols,
        occurrences=tuple(reversed(occurrences)) if reverse else occurrences,
        edges=edges,
    )


def test_fact_batch_is_deterministic_and_round_trips() -> None:
    forward = _batch()
    reverse = _batch(reverse=True)

    assert forward == reverse
    assert forward.digest() == reverse.digest()
    assert forward.capabilities == (
        CAPABILITY_DEFINITIONS,
        CAPABILITY_EDGES,
        CAPABILITY_OCCURRENCES,
    )
    assert FactBatch.from_dict(forward.to_dict()) == forward
    assert forward.to_dict()["schema_version"] == 1


def test_fact_batch_rejects_ambiguous_or_noncanonical_identity() -> None:
    with pytest.raises(ValueError, match="repository-relative POSIX"):
        FactBatch(
            file_path="../src/main.py",
            language="python",
            content_digest=_CONTENT_DIGEST,
            profile_digest=_PROFILE_DIGEST,
            provider="test",
            completeness=COMPLETENESS_PARTIAL,
        )
    with pytest.raises(ValueError, match="canonical sha256"):
        FactBatch(
            file_path="src/main.py",
            language="python",
            content_digest="sha256:not-a-digest",
            profile_digest=_PROFILE_DIGEST,
            provider="test",
            completeness=COMPLETENESS_PARTIAL,
        )
    with pytest.raises(ValueError, match="symbol_id values must be unique"):
        FactBatch(
            file_path="src/main.py",
            language="python",
            content_digest=_CONTENT_DIGEST,
            profile_digest=_PROFILE_DIGEST,
            provider="test",
            completeness=COMPLETENESS_PARTIAL,
            symbols=(_symbol("duplicate", 1), _symbol("duplicate", 2)),
        )
    with pytest.raises(ValueError, match="schema_version"):
        FactBatch(
            file_path="src/main.py",
            language="python",
            content_digest=_CONTENT_DIGEST,
            profile_digest=_PROFILE_DIGEST,
            provider="test",
            completeness=COMPLETENESS_PARTIAL,
            schema_version=True,
        )


def test_failed_fact_batch_cannot_publish_semantic_facts() -> None:
    with pytest.raises(ValueError, match="must not contain semantic facts"):
        FactBatch(
            file_path="src/main.py",
            language="python",
            content_digest=_CONTENT_DIGEST,
            profile_digest=_PROFILE_DIGEST,
            provider="test",
            completeness=COMPLETENESS_FAILED,
            symbols=(_symbol("run", 1),),
        )


def test_edge_fact_requires_one_target_and_bounded_confidence() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        EdgeFact(
            kind=EDGE_TYPE_REFERENCE,
            provenance=PROVENANCE_SCIP,
            target_symbol_id="resolved",
            target_moniker="unresolved",
        )
    with pytest.raises(ValueError, match="between 0 and 1"):
        EdgeFact(
            kind=EDGE_TYPE_REFERENCE,
            provenance=PROVENANCE_SCIP,
            target_moniker="target",
            confidence=1.1,
        )
    with pytest.raises(ValueError, match="must name the resolver"):
        EdgeFact(
            kind=EDGE_TYPE_REFERENCE,
            provenance=PROVENANCE_HEURISTIC,
            target_moniker="target",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("code", "x" * 257, "diagnostic code must be at most 256"),
        ("message", "x" * 4097, "diagnostic message must be at most 4096"),
    ),
)
def test_diagnostic_fact_bounds_untrusted_provider_text(
    field: str, value: str, message: str
) -> None:
    values = {"severity": "warning", "code": "W001", "message": "bounded"}
    values[field] = value

    with pytest.raises(ValueError, match=message):
        DiagnosticFact(**values)


def test_merge_fact_batches_unions_provider_facts() -> None:
    first = FactBatch(
        file_path="src/main.py",
        language="python",
        content_digest=_CONTENT_DIGEST,
        profile_digest=sha256_digest("definitions"),
        provider="definitions",
        completeness=COMPLETENESS_PARTIAL,
        capabilities=(CAPABILITY_DEFINITIONS,),
        symbols=(_symbol("run", 1),),
    )
    occurrence = OccurrenceFact("run", _range(1), roles=1)
    second = FactBatch(
        file_path="src/main.py",
        language="python",
        content_digest=_CONTENT_DIGEST,
        profile_digest=sha256_digest("occurrences"),
        provider="occurrences",
        completeness=COMPLETENESS_PARTIAL,
        capabilities=(CAPABILITY_OCCURRENCES,),
        occurrences=(occurrence, occurrence),
    )

    merged = merge_fact_batches(
        (first, second),
        provider="combined",
        profile_digest=_PROFILE_DIGEST,
    )

    assert merged.provider == "combined"
    assert merged.capabilities == (
        CAPABILITY_DEFINITIONS,
        CAPABILITY_OCCURRENCES,
    )
    assert merged.symbols == first.symbols
    assert merged.occurrences == (occurrence,)


def test_merge_rejects_cross_content_contamination() -> None:
    incompatible = FactBatch(
        file_path="src/main.py",
        language="python",
        content_digest=sha256_digest("different source"),
        profile_digest=_PROFILE_DIGEST,
        provider="other",
        completeness=COMPLETENESS_PARTIAL,
    )

    with pytest.raises(ValueError, match="must share file"):
        merge_fact_batches(
            (_batch(), incompatible),
            provider="combined",
            profile_digest=_PROFILE_DIGEST,
        )
