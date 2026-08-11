# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for exact and framework-aware fact resolution."""

from __future__ import annotations

import pytest

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_EDGES,
    COMPLETENESS_PARTIAL,
    PROVENANCE_FRAMEWORK,
    PROVENANCE_SCIP,
    DefinitionHotCache,
    EdgeFact,
    FactBatch,
    FactResolverPipeline,
    FrameworkRule,
    FrameworkRuleResolver,
    ResolverRegistry,
    SnapshotDefinitionResolver,
    SourceRange,
    SymbolFact,
    sha256_digest,
)
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION

_PROFILE = sha256_digest("resolver input")
_RESOLVED_PROFILE = sha256_digest("resolver output")


def _symbol(symbol_id: str, moniker: str, line: int = 0) -> SymbolFact:
    return SymbolFact(
        symbol_id=symbol_id,
        moniker=moniker,
        display_name=moniker,
        kind=NODE_TYPE_FUNCTION,
        definition_range=SourceRange(line, 0, line + 1, 0),
    )


def _batch(
    file_path: str,
    *,
    symbols=(),
    edges=(),
) -> FactBatch:
    capabilities = [CAPABILITY_DEFINITIONS]
    if edges:
        capabilities.append(CAPABILITY_EDGES)
    return FactBatch(
        file_path=file_path,
        language="python",
        content_digest=sha256_digest(f"source:{file_path}"),
        profile_digest=_PROFILE,
        provider="scip-python",
        completeness=COMPLETENESS_PARTIAL,
        capabilities=tuple(capabilities),
        symbols=tuple(symbols),
        edges=tuple(edges),
    )


def test_exact_resolver_replaces_unique_moniker_and_keeps_provenance() -> None:
    unresolved = EdgeFact(
        kind=EDGE_TYPE_REFERENCE,
        provenance=PROVENANCE_SCIP,
        source_symbol_id="caller-id",
        target_moniker="target-moniker",
        anchor_range=SourceRange(4, 2, 4, 8),
    )
    source = _batch(
        "src/caller.py",
        symbols=(_symbol("caller-id", "caller-moniker"),),
        edges=(unresolved,),
    )
    target = _batch(
        "src/target.py",
        symbols=(_symbol("target-id", "target-moniker"),),
    )

    result = FactResolverPipeline().resolve(
        (target, source),
        profile_digest=_RESOLVED_PROFILE,
    )

    assert [batch.file_path for batch in result.batches] == [
        "src/caller.py",
        "src/target.py",
    ]
    resolved = result.batches[0].edges[0]
    assert resolved.target_symbol_id == "target-id"
    assert resolved.target_moniker is None
    assert resolved.provenance == PROVENANCE_SCIP
    assert resolved.resolver == "exact_moniker_v1"
    assert result.unresolved_edges == 0
    assert result.passes[0].to_dict() == {
        "name": "exact_moniker_v1",
        "input_edges": 1,
        "output_edges": 1,
        "unresolved_before": 1,
        "unresolved_after": 0,
        "inferred_edges_after": 0,
    }


def test_exact_resolver_leaves_ambiguous_global_target_unresolved() -> None:
    source = _batch(
        "src/caller.py",
        edges=(
            EdgeFact(
                kind=EDGE_TYPE_REFERENCE,
                provenance=PROVENANCE_SCIP,
                target_moniker="duplicate",
            ),
        ),
    )
    left = _batch(
        "src/left.py",
        symbols=(_symbol("left-id", "duplicate"),),
    )
    right = _batch(
        "src/right.py",
        symbols=(_symbol("right-id", "duplicate"),),
    )

    result = FactResolverPipeline().resolve(
        (source, left, right),
        profile_digest=_RESOLVED_PROFILE,
    )

    caller = next(
        batch for batch in result.batches if batch.file_path == "src/caller.py"
    )
    assert caller.edges[0].target_moniker == "duplicate"
    assert result.unresolved_edges == 1


def test_exact_resolver_scopes_local_monikers_to_the_source_file() -> None:
    local = "local 0 local_value."
    source = _batch(
        "src/a.py",
        symbols=(_symbol("a-local", local),),
        edges=(
            EdgeFact(
                kind=EDGE_TYPE_REFERENCE,
                provenance=PROVENANCE_SCIP,
                target_moniker=local,
            ),
        ),
    )
    other = _batch(
        "src/b.py",
        symbols=(_symbol("b-local", local),),
    )

    result = FactResolverPipeline().resolve(
        (source, other),
        profile_digest=_RESOLVED_PROFILE,
    )

    assert result.batches[0].edges[0].target_symbol_id == "a-local"


def test_framework_rule_plugin_adds_labeled_confidence_bounded_edge() -> None:
    source = _batch(
        "src/controller.py",
        symbols=(_symbol("controller-id", "Controller.handle"),),
    )
    target = _batch(
        "src/job.py",
        symbols=(_symbol("job-id", "RefreshJob.perform"),),
    )
    registry = ResolverRegistry()
    registry.register(
        FrameworkRuleResolver(
            "sidekiq",
            [
                FrameworkRule(
                    source_moniker="Controller.handle",
                    source_file="src/controller.py",
                    target_moniker="RefreshJob.perform",
                    target_file="src/job.py",
                    confidence=0.85,
                )
            ],
        )
    )

    result = registry.pipeline().resolve(
        (source, target),
        profile_digest=_RESOLVED_PROFILE,
        provider="scip+frameworks",
    )

    controller = result.batches[0]
    assert CAPABILITY_EDGES in controller.capabilities
    assert controller.provider == "scip+frameworks"
    assert len(controller.edges) == 1
    edge = controller.edges[0]
    assert edge.source_symbol_id == "controller-id"
    assert edge.target_symbol_id == "job-id"
    assert edge.provenance == PROVENANCE_FRAMEWORK
    assert edge.confidence == 0.85
    assert edge.resolver == "framework:sidekiq"
    assert [report.name for report in result.passes] == [
        "exact_moniker_v1",
        "framework:sidekiq",
    ]
    assert result.passes[-1].inferred_edges_after == 1


def test_resolver_registry_rejects_duplicate_plugin_names() -> None:
    registry = ResolverRegistry()
    resolver = FrameworkRuleResolver("django", [])
    registry.register(resolver)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(resolver)


def test_snapshot_resolver_cache_is_bounded_and_never_cross_contaminates() -> None:
    source = _batch(
        "src/caller.py",
        symbols=(_symbol("caller", "caller"),),
        edges=(
            EdgeFact(
                kind=EDGE_TYPE_REFERENCE,
                provenance=PROVENANCE_SCIP,
                source_symbol_id="caller",
                target_moniker="target",
            ),
        ),
    )
    target_one = _batch(
        "src/one.py",
        symbols=(_symbol("one", "target"),),
    )
    target_two = _batch(
        "src/two.py",
        symbols=(_symbol("two", "target"),),
    )
    cache = DefinitionHotCache(max_entries=2)
    first = SnapshotDefinitionResolver(
        "snapshot-one",
        (source, target_one),
        cache=cache,
    )
    second = SnapshotDefinitionResolver(
        "snapshot-two",
        (source, target_two),
        cache=cache,
    )

    assert first.resolve_edge(source, source.edges[0]).symbol.symbol_id == "one"
    assert second.resolve_edge(source, source.edges[0]).symbol.symbol_id == "two"
    assert first.resolve_edge(source, source.edges[0]).symbol.symbol_id == "one"
    assert cache.hits == 1
    assert cache.misses == 2
    assert len(cache) == 2

    assert first.resolve("missing") is None
    assert len(cache) == 2
    assert cache.evictions == 1
    assert cache.clear_snapshot("snapshot-two") == 0
    assert cache.clear_snapshot("snapshot-one") == 2
    assert len(cache) == 0
