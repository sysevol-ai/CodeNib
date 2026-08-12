# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-free native FactQueryIndex parity and selection tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import pytest

if importlib.util.find_spec("codenib_core") is None:
    pytest.skip(
        "codenib_core pybind module not built; run make core-build",
        allow_module_level=True,
    )

import codenib_core  # noqa: E402

from codenib.agent.lsp_graph import (  # noqa: E402
    lsp_definition,
    lsp_references,
    lsp_route,
)
from codenib.graph.code_graph import CodeGraph  # noqa: E402
from codenib.scip_interface import scip_decode_core as scip_core  # noqa: E402
from codenib.scip_interface.scip_decode_core import (  # noqa: E402
    SCIPDecoderCore,
    _build_code_graph,
    _validate_fact_query_contract,
)
from codenib.types import node_has_definition  # noqa: E402

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures/typescript_schema_v5/index.decoded"
)


def _legacy_graph() -> CodeGraph:
    payload = codenib_core.decode_scip(
        index_file=str(_FIXTURE), project_root=None, language="typescript"
    )
    return _build_code_graph(payload["vertices"], payload["edges"], project_root=None)


def _native_index():
    payload = codenib_core.decode_scip_fact_query_index(
        index_file=str(_FIXTURE), project_root=None, language="typescript"
    )
    assert payload["graph_materialized"] is False
    return payload["index"]


def _outcome(call: Callable[[], Any]) -> tuple[Any, ...]:
    try:
        return "ok", call()
    except Exception as exc:  # noqa: BLE001 - public failure parity is required
        return "error", type(exc).__name__, str(exc)


def _symbol_seeds(graph: CodeGraph) -> list[str]:
    seeds: list[str] = []
    for name in graph.name_to_vertex:
        info = graph.get_node_info_by_name(name) or {}
        if not node_has_definition(info):
            continue
        display = str(info.get("unified_name") or name)
        bare = display.split(":")[-1].split(".")[-1].split("#")[-1]
        bare = bare.rstrip("()")
        seeds.extend((name, display, bare, f"`{bare}`"))
    seeds.append("definitely-missing-fact-query-symbol")
    return list(dict.fromkeys(seed for seed in seeds if seed))


def test_compiled_contract_and_capabilities_are_explicit() -> None:
    contract = codenib_core.fact_query_index_contract()
    _validate_fact_query_contract(contract)
    index = _native_index()

    assert contract == {
        "abi_version": 1,
        "format": "fact-query-index-v1",
        "requires_anchored_references": True,
        "filter_identity_proof_schema_version": 1,
        "input_receipt_schema_version": 1,
        "capabilities": {
            "definition_by_symbol": True,
            "references_by_symbol": True,
            "position_queries": False,
            "route_queries": False,
        },
    }
    assert index.fact_query_index is True
    assert index.materializes_graph is False
    assert index.capabilities == contract["capabilities"]
    assert index.project_root is None
    assert not hasattr(index, "graph")


def test_contract_rejects_boolean_abi_version() -> None:
    contract = dict(codenib_core.fact_query_index_contract())
    contract["abi_version"] = True

    with pytest.raises(RuntimeError, match="contract mismatch"):
        _validate_fact_query_contract(contract)


@pytest.mark.parametrize("include_declaration", (False, True))
def test_symbol_definition_reference_results_and_errors_match_code_graph(
    include_declaration: bool,
) -> None:
    graph = _legacy_graph()
    index = _native_index()

    assert index.record_count == graph.graph.vcount()
    assert index.edge_count == graph.graph.ecount()
    assert index.symbol_count == sum(
        node_has_definition(graph.get_node_info_by_name(name) or {})
        for name in graph.name_to_vertex
    )
    assert index.reference_count == sum(
        edge.attributes().get("type") == "reference" for edge in graph.graph.es
    )

    for seed in _symbol_seeds(graph):
        expected_definition = _outcome(
            lambda seed=seed: lsp_definition(graph, symbol=seed, top_k=10_000)
        )
        actual_definition = _outcome(
            lambda seed=seed: lsp_definition(index, symbol=seed, top_k=10_000)
        )
        assert actual_definition == expected_definition, seed

        expected_references = _outcome(
            lambda seed=seed: lsp_references(
                graph,
                symbol=seed,
                include_declaration=include_declaration,
                top_k=10_000,
            )
        )
        actual_references = _outcome(
            lambda seed=seed: lsp_references(
                index,
                symbol=seed,
                include_declaration=include_declaration,
                top_k=10_000,
            )
        )
        assert actual_references == expected_references, seed


def test_position_and_route_queries_fail_instead_of_returning_partial_rows() -> None:
    index = _native_index()

    with pytest.raises(ValueError, match="does not support position queries"):
        lsp_definition(index, file_path="src/types.ts", line=0, character=0)
    with pytest.raises(ValueError, match="does not support position queries"):
        lsp_references(index, file_path="src/types.ts", line=0, character=0)
    with pytest.raises(ValueError, match="does not support route queries"):
        lsp_route(index, symbols=["DefaultRunner"])


def test_query_selector_defaults_to_graph_for_unpromoted_languages(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CODENIB_NATIVE_FACT_QUERY_INDEX", raising=False)
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "off")
    decoder = SCIPDecoderCore(str(_FIXTURE), language="typescript")

    index = decoder.decode_query_index()

    assert isinstance(index, CodeGraph)
    assert decoder.query_backend == "code-graph-not-promoted"
    assert decoder.query_timings["fact_query_index_enabled"] == 0


def test_query_selector_promoted_auto_and_required_are_graph_free(monkeypatch) -> None:
    monkeypatch.setattr(
        scip_core, "_FACT_QUERY_PROMOTED_LANGUAGES", frozenset({"typescript"})
    )
    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "auto")
    decoder = SCIPDecoderCore(str(_FIXTURE), language="typescript")

    index = decoder.decode_query_index()

    assert index.fact_query_index is True
    assert decoder.code_graph is None
    assert decoder.query_backend == "fact-query-index-v1"
    assert decoder.query_fallback_error is None
    assert decoder.query_timings["fact_query_index_enabled"] == 1

    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "required")
    required = SCIPDecoderCore(str(_FIXTURE), language="typescript")
    assert required.decode_query_index().fact_query_index is True


def test_query_selector_off_preserves_complete_graph(monkeypatch) -> None:
    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "off")
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "off")
    decoder = SCIPDecoderCore(str(_FIXTURE), language="typescript")

    index = decoder.decode_query_index()

    assert isinstance(index, CodeGraph)
    assert decoder.query_backend == "code-graph-disabled"
    assert decoder.code_graph is index


def test_query_selector_auto_falls_back_but_required_fails(monkeypatch) -> None:
    def fail_query_index(**_kwargs):
        raise RuntimeError("injected FactQueryIndex failure")

    monkeypatch.setattr(codenib_core, "decode_scip_fact_query_index", fail_query_index)
    monkeypatch.setattr(
        scip_core, "_FACT_QUERY_PROMOTED_LANGUAGES", frozenset({"typescript"})
    )
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "off")
    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "auto")
    decoder = SCIPDecoderCore(str(_FIXTURE), language="typescript")

    assert isinstance(decoder.decode_query_index(), CodeGraph)
    assert decoder.query_backend == "code-graph-fallback"
    assert "injected FactQueryIndex failure" in decoder.query_fallback_error

    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "required")
    with pytest.raises(RuntimeError, match="injected FactQueryIndex failure"):
        SCIPDecoderCore(str(_FIXTURE), language="typescript").decode_query_index()


def test_query_selector_does_not_mask_memory_exhaustion(monkeypatch) -> None:
    def exhaust_memory(**_kwargs):
        raise MemoryError("injected allocation failure")

    monkeypatch.setattr(codenib_core, "decode_scip_fact_query_index", exhaust_memory)
    monkeypatch.setattr(
        scip_core, "_FACT_QUERY_PROMOTED_LANGUAGES", frozenset({"typescript"})
    )
    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "auto")

    with pytest.raises(MemoryError, match="injected allocation failure"):
        SCIPDecoderCore(str(_FIXTURE), language="typescript").decode_query_index()


def test_query_selector_rejects_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("CODENIB_NATIVE_FACT_QUERY_INDEX", "sometimes")

    with pytest.raises(ValueError, match="CODENIB_NATIVE_FACT_QUERY_INDEX must be"):
        SCIPDecoderCore(str(_FIXTURE), language="typescript").decode_query_index()
