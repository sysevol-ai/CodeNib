# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Native FactBatchBuffer ABI, parity, and ownership tests."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("codenib_core") is None:
    pytest.skip(
        "codenib_core pybind module not built; run make core-build",
        allow_module_level=True,
    )

import codenib_core  # noqa: E402

from codenib.facts import fact_batches_from_code_graph, sha256_digest  # noqa: E402
from codenib.scip_interface.fact_batch_buffer import (  # noqa: E402
    FactBatchBufferError,
    FactBatchBufferView,
    validate_compiled_contract,
)
from codenib.scip_interface.scip_decode_core import _build_code_graph  # noqa: E402

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures/typescript_schema_v5/index.decoded"
)
_PROFILE_DIGEST = sha256_digest("native-fact-buffer-test-profile")


def _legacy_graph():
    result = codenib_core.decode_scip(
        index_file=str(_FIXTURE),
        project_root=None,
        language="typescript",
    )
    return _build_code_graph(result["vertices"], result["edges"])


def _content_digests(graph) -> dict[str, str]:
    return {
        attributes["name"]: sha256_digest(f"fixture:{attributes['name']}")
        for vertex in graph.graph.vs
        if (attributes := vertex.attributes()).get("type") == "file"
    }


def _payload(
    *,
    content_digests: dict[str, str] | None = None,
    include_graph_compat: bool = True,
    copy_buffers: bool = True,
) -> dict[str, Any]:
    return codenib_core.decode_scip_fact_buffer(
        index_file=str(_FIXTURE),
        profile_digest=_PROFILE_DIGEST,
        project_root=None,
        language="typescript",
        content_digests=content_digests or {},
        include_graph_compat=include_graph_compat,
        copy_buffers=copy_buffers,
    )


def _replace_u32(payload: dict[str, Any], table: str, offset: int, value: int) -> None:
    data = bytearray(payload[table])
    struct.pack_into("<I", data, offset, value)
    payload[table] = bytes(data)


def _vertex_rows(graph) -> list[tuple[Any, ...]]:
    fields = (
        "name",
        "type",
        "file",
        "start_line",
        "end_line",
        "selection_line",
        "unified_name",
        "symbol_kind",
        "has_definition",
    )
    return [
        tuple(vertex.attributes().get(field) for field in fields)
        for vertex in graph.graph.vs
    ]


def _edge_rows(graph) -> list[tuple[Any, ...]]:
    return [
        (
            edge.source,
            edge.target,
            edge.attributes().get("type"),
            edge.attributes().get("anchor_file"),
            edge.attributes().get("anchor_line"),
        )
        for edge in graph.graph.es
    ]


def test_compiled_contract_and_graph_projection_are_exact() -> None:
    validate_compiled_contract(codenib_core.fact_batch_buffer_contract())
    expected = _legacy_graph()
    view = FactBatchBufferView(_payload())
    actual = view.materialize_code_graph()

    assert _vertex_rows(actual) == _vertex_rows(expected)
    assert _edge_rows(actual) == _edge_rows(expected)
    assert actual.name_to_vertex == expected.name_to_vertex
    assert actual.symbol_ranges == expected.symbol_ranges


def test_compiled_contract_rejects_boolean_version() -> None:
    contract = dict(codenib_core.fact_batch_buffer_contract())
    contract["abi_version"] = True

    with pytest.raises(FactBatchBufferError, match="contract mismatch"):
        validate_compiled_contract(contract)


def test_semantic_tables_match_the_legacy_graph_adapter() -> None:
    graph = _legacy_graph()
    content_digests = _content_digests(graph)
    view = FactBatchBufferView(_payload(content_digests=content_digests))

    actual = view.to_fact_batches()
    expected = fact_batches_from_code_graph(
        graph,
        language="typescript",
        content_digests=content_digests,
        profile_digest=_PROFILE_DIGEST,
        provider="scip-core",
    )

    assert actual == expected


def test_fact_only_direct_decode_skips_graph_materialization() -> None:
    graph = _legacy_graph()
    content_digests = _content_digests(graph)
    view = FactBatchBufferView(
        _payload(
            content_digests=content_digests,
            include_graph_compat=False,
            copy_buffers=False,
        )
    )

    assert view.has_graph_compat is False
    assert view.meta.graph_vertex_count == 0
    assert view.meta.graph_edge_count == 0
    assert view.meta.decode_profile_ns["materialize_graph"] == 0
    assert view.to_fact_batches() == fact_batches_from_code_graph(
        graph,
        language="typescript",
        content_digests=content_digests,
        profile_digest=_PROFILE_DIGEST,
        provider="scip-core",
    )
    with pytest.raises(FactBatchBufferError, match="does not include graph"):
        view.materialize_code_graph()


def test_owned_buffers_are_read_only_and_keep_native_storage_alive() -> None:
    payload = _payload(include_graph_compat=False, copy_buffers=False)
    meta = memoryview(payload["meta"])
    view = FactBatchBufferView(payload)

    assert meta.readonly is True
    assert bytes(meta[:4]) == b"CNFB"
    del payload
    assert view.meta.batch_count > 0
    with pytest.raises(TypeError):
        meta[0] = 0


def test_writable_buffers_are_rejected_before_validation() -> None:
    payload = _payload()
    payload["arena"] = bytearray(payload["arena"])

    with pytest.raises(FactBatchBufferError, match="arena must be read-only"):
        FactBatchBufferView(payload)


def test_missing_content_digest_blocks_logical_fact_materialization() -> None:
    view = FactBatchBufferView(_payload())

    with pytest.raises(FactBatchBufferError, match="content digest is required"):
        view.to_fact_batches()


@pytest.mark.parametrize("corruption", ("abi", "arena"))
def test_wire_corruption_is_rejected(corruption: str) -> None:
    payload = _payload()
    if corruption == "abi":
        meta = bytearray(payload["meta"])
        meta[4] = 99
        payload["meta"] = bytes(meta)
        message = "ABI 99"
    else:
        payload["arena"] = payload["arena"][:-1]
        message = "meta declares"

    with pytest.raises(FactBatchBufferError, match=message):
        FactBatchBufferView(payload)


@pytest.mark.parametrize("corruption", ("table_size", "meta_flags"))
def test_envelope_corruption_is_rejected_during_view_creation(corruption: str) -> None:
    payload = _payload()
    if corruption == "table_size":
        payload["symbols"] = payload["symbols"][:-1]
        message = "symbols has"
    else:
        _replace_u32(payload, "meta", 8, 1 << 31)
        message = "unsupported flags"

    with pytest.raises(FactBatchBufferError, match=message):
        FactBatchBufferView(payload)


@pytest.mark.parametrize("corruption", ("symbol_flags", "edge_flags", "digest_flag"))
def test_semantic_corruption_is_rejected_before_batches_are_returned(
    corruption: str,
) -> None:
    graph = _legacy_graph()
    payload = _payload(content_digests=_content_digests(graph))
    if corruption == "symbol_flags":
        _replace_u32(payload, "symbols", 4, 0)
        message = "invalid definition flags"
    elif corruption == "edge_flags":
        data = bytearray(payload["edges"])
        data[5] = 0
        payload["edges"] = bytes(data)
        message = "flags do not match optional fields"
    else:
        _replace_u32(payload, "meta", 8, 1)
        message = "content-digest flag does not match"

    view = FactBatchBufferView(payload)
    with pytest.raises(FactBatchBufferError, match=message):
        view.to_fact_batches()


def test_invalid_semantic_identity_uses_the_wire_error_boundary() -> None:
    graph = _legacy_graph()
    payload = _payload(content_digests=_content_digests(graph))
    data = bytearray(payload["edges"])
    data[5] &= ~(1 << 1)
    struct.pack_into("<II", data, 32, 0xFFFFFFFF, 0)
    payload["edges"] = bytes(data)

    view = FactBatchBufferView(payload)
    with pytest.raises(FactBatchBufferError, match="exactly one .* target"):
        view.to_fact_batches()


@pytest.mark.parametrize("corruption", ("edge_endpoint", "duplicate_vertex"))
def test_graph_corruption_is_rejected_before_a_graph_is_returned(
    corruption: str,
) -> None:
    payload = _payload()
    if corruption == "edge_endpoint":
        _replace_u32(payload, "graph_edges", 0, 0xFFFFFFFF)
        message = "endpoint"
    else:
        data = bytearray(payload["graph_vertices"])
        row_size = codenib_core.fact_batch_buffer_contract()["graph_vertex_row_size"]
        data[row_size : row_size + 8] = data[:8]
        payload["graph_vertices"] = bytes(data)
        message = "globally unique"

    view = FactBatchBufferView(payload)
    with pytest.raises(FactBatchBufferError, match=message):
        view.materialize_code_graph()
