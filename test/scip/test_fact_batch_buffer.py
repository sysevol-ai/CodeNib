# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Native FactBatchBuffer ABI, parity, ownership, and fallback tests."""

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
from codenib.scip_interface.scip_decode_core import (  # noqa: E402
    SCIPDecoderCore,
    _build_code_graph,
)
from scripts.profiling.profile_fact_batch_buffer import (  # noqa: E402
    profile_fact_batch_buffer,
)

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
    return _build_code_graph(
        result["vertices"], result["edges"], project_root=result["project_root"]
    )


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
    assert actual.project_root == expected.project_root


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


def test_fact_buffer_profiler_can_gate_logical_fact_consumer() -> None:
    report = profile_fact_batch_buffer(
        index_path=_FIXTURE,
        language="typescript",
        iterations=1,
        warmups=0,
        include_semantic_consumer=True,
    )

    semantic = report["semantic_fact_consumer"]
    assert report["schema_version"] == 1
    assert report["configuration"]["copy_buffers"] is False
    assert report["configuration"]["fact_batch_buffer_contract"] == (
        codenib_core.fact_batch_buffer_contract()
    )
    assert report["configuration"]["first_arm_by_iteration"] == ["legacy"]
    assert len(report["raw_samples"]["legacy"]["total"]) == 1
    assert len(semantic["raw_samples"]["native_fact_buffer"]["total"]) == 1
    assert semantic["parity"] == {"passed": True, "error": None}
    assert semantic["batch_count"] > 0
    assert semantic["native_fact_buffer"]["fact_materialize"]["samples"] == 1


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


def test_decoder_kill_switch_preserves_legacy_compatibility(monkeypatch) -> None:
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "auto")
    native = SCIPDecoderCore(str(_FIXTURE), language="typescript")
    native_graph = native.decode()
    assert native.transport_backend == "fact-buffer-v1"
    assert native.fact_buffer_view is not None

    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "off")
    legacy = SCIPDecoderCore(str(_FIXTURE), language="typescript")
    legacy_graph = legacy.decode()
    assert legacy.transport_backend == "legacy-disabled"
    assert legacy.fact_buffer_view is None
    assert _vertex_rows(native_graph) == _vertex_rows(legacy_graph)
    assert _edge_rows(native_graph) == _edge_rows(legacy_graph)
    assert native_graph.project_root == legacy_graph.project_root


def test_decoder_defaults_to_legacy_until_promotion_gate_passes(monkeypatch) -> None:
    monkeypatch.delenv("CODENIB_CORE_FACT_BUFFER", raising=False)

    decoder = SCIPDecoderCore(str(_FIXTURE), language="typescript")
    graph = decoder.decode()

    assert graph.graph.vcount() > 0
    assert decoder.transport_backend == "legacy-disabled"
    assert decoder.fact_buffer_view is None
    assert decoder.timings["fact_buffer_enabled"] == 0


def test_decoder_auto_falls_back_but_required_mode_fails(monkeypatch) -> None:
    def fail_buffer(**_kwargs):
        raise RuntimeError("injected native buffer failure")

    monkeypatch.setattr(codenib_core, "decode_scip_fact_buffer", fail_buffer)
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "auto")
    decoder = SCIPDecoderCore(str(_FIXTURE), language="typescript")
    fallback_graph = decoder.decode()
    assert fallback_graph.graph.vcount() > 0
    assert decoder.transport_backend == "legacy-fallback"
    assert "injected native buffer failure" in decoder.transport_fallback_error

    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "off")
    legacy_graph = SCIPDecoderCore(str(_FIXTURE), language="typescript").decode()
    assert _vertex_rows(fallback_graph) == _vertex_rows(legacy_graph)
    assert _edge_rows(fallback_graph) == _edge_rows(legacy_graph)
    assert fallback_graph.project_root == legacy_graph.project_root

    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "required")
    with pytest.raises(RuntimeError, match="injected native buffer failure"):
        SCIPDecoderCore(str(_FIXTURE), language="typescript").decode()


def test_decoder_rejects_unknown_transport_mode(monkeypatch) -> None:
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "sometimes")

    with pytest.raises(ValueError, match="CODENIB_CORE_FACT_BUFFER must be"):
        SCIPDecoderCore(str(_FIXTURE), language="typescript").decode()


def test_decoder_auto_does_not_mask_memory_exhaustion(monkeypatch) -> None:
    def exhaust_memory(**_kwargs):
        raise MemoryError("injected allocation failure")

    monkeypatch.setattr(codenib_core, "decode_scip_fact_buffer", exhaust_memory)
    monkeypatch.setenv("CODENIB_CORE_FACT_BUFFER", "auto")

    with pytest.raises(MemoryError, match="injected allocation failure"):
        SCIPDecoderCore(str(_FIXTURE), language="typescript").decode()
