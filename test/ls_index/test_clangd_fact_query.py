# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Baseline native clangd symbol-query parity, fallback, and benchmark tests."""

from __future__ import annotations

import importlib.util
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Callable

import pytest

if importlib.util.find_spec("codenib_core") is None:
    pytest.skip(
        "codenib_core pybind module not built; run make core-build",
        allow_module_level=True,
    )

import codenib_core  # noqa: E402

from codenib.agent.lsp_graph import lsp_definition, lsp_references  # noqa: E402
from codenib.agent.lsp_provider import StaticLSPProvider  # noqa: E402
from codenib.graph.code_graph import CodeGraph  # noqa: E402
from codenib.ls_index import clangd_decode  # noqa: E402
from codenib.ls_index.clangd_decode import ClangdGraphDecoder  # noqa: E402
from codenib.ls_index.clangd_decode import (
    ClangdHybridQueryProvider,
    _validate_native_query_contract,
)
from codenib.ls_router import LSIndexer  # noqa: E402
from codenib.types import EDGE_TYPE_REFERENCE, node_has_definition  # noqa: E402
from scripts.profiling.profile_clangd_fact_query_index import (  # noqa: E402
    main as profile_main,
)
from scripts.profiling.profile_clangd_fact_query_index import (  # noqa: E402
    profile_clangd_fact_query_index,
)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def _location(
    file_index: int,
    line: int,
    column: int = 0,
    *,
    end_column: int | None = None,
) -> bytes:
    resolved_end_column = column + 1 if end_column is None else end_column
    return b"".join(
        _varint(value)
        for value in (file_index, line, column, line, resolved_end_column)
    )


def _symbol(
    symbol_id: bytes,
    *,
    kind: int,
    name_index: int,
    scope_index: int,
    file_index: int,
    line: int,
    column: int = 0,
    end_column: int | None = None,
) -> bytes:
    return b"".join(
        (
            symbol_id,
            bytes((kind, 2)),
            _varint(name_index),
            _varint(scope_index),
            _varint(0),
            _location(file_index, line, column, end_column=end_column),
            _location(file_index, line, column, end_column=end_column),
            _varint(0),
            b"\x00",
            _varint(0),
            _varint(0),
            _varint(0),
            _varint(0),
            _varint(0),
            _varint(0),
        )
    )


def _ref_group(symbol_id: bytes, rows: list[tuple[int, bytes, bytes]]) -> bytes:
    payload = bytearray(symbol_id + _varint(len(rows)))
    for kind, location, container in rows:
        payload.extend(bytes((kind,)))
        payload.extend(location)
        payload.extend(container)
    return bytes(payload)


def _relation(subject: bytes, predicate: int, object_id: bytes) -> bytes:
    return subject + bytes((predicate,)) + object_id


def _riff_chunk(name: bytes, payload: bytes) -> bytes:
    result = name + struct.pack("<I", len(payload)) + payload
    return result + (b"\x00" if len(payload) % 2 else b"")


def _riff_bytes(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = b"CdIx" + b"".join(_riff_chunk(name, payload) for name, payload in chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _empty_idx() -> bytes:
    return _riff_bytes(_minimal_chunks())


def _minimal_chunks(version: int = 18) -> list[tuple[bytes, bytes]]:
    return [
        (b"meta", struct.pack("<I", version)),
        (b"stri", struct.pack("<I", 0) + b"\x00"),
    ]


def _set_meta_version(idx_directory: Path, before: int, after: int) -> None:
    path = next(idx_directory.glob("*.idx"))
    data = path.read_bytes()
    old = b"meta" + struct.pack("<I", 4) + struct.pack("<I", before)
    new = b"meta" + struct.pack("<I", 4) + struct.pack("<I", after)
    assert data.count(old) == 1
    path.write_bytes(data.replace(old, new, 1))


@pytest.fixture()
def clangd_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    source = root / "src" / "main.cpp"
    source.parent.mkdir(parents=True)
    source.write_text(
        "int target() { return 1; }\n"
        "int caller() { return target(); }\n"
        "int target();\n"
        "Thing value;\n",
        encoding="utf-8",
    )
    idx_directory = root / ".cache" / "clangd" / "index"
    idx_directory.mkdir(parents=True)

    caller_id = bytes.fromhex("0102030405060708")
    target_id = bytes.fromhex("1112131415161718")
    method_one_id = bytes.fromhex("2122232425262728")
    method_two_id = bytes.fromhex("3132333435363738")
    function_id = bytes.fromhex("4142434445464748")
    method_three_id = bytes.fromhex("5152535455565758")
    directory_collision_id = bytes.fromhex("6162636465666768")
    zero_id = b"\x00" * 8
    strings = [
        "",
        source.as_uri(),
        "caller",
        "target",
        "demo::",
        "Thing",
        "Thing::",
        "src",
    ]
    raw_strings = b"\x00".join(value.encode("utf-8") for value in strings) + b"\x00"
    string_table = struct.pack("<I", len(raw_strings)) + zlib.compress(raw_strings)
    symbols = b"".join(
        (
            _symbol(
                caller_id,
                kind=12,
                name_index=2,
                scope_index=4,
                file_index=1,
                line=1,
                column=4,
                end_column=10,
            ),
            _symbol(
                target_id,
                kind=12,
                name_index=3,
                scope_index=4,
                file_index=1,
                line=0,
                column=4,
                end_column=10,
            ),
            _symbol(
                method_one_id,
                kind=22,
                name_index=5,
                scope_index=6,
                file_index=1,
                line=3,
            ),
            _symbol(
                method_two_id,
                kind=22,
                name_index=5,
                scope_index=6,
                file_index=1,
                line=3,
            ),
            _symbol(
                function_id,
                kind=12,
                name_index=5,
                scope_index=0,
                file_index=1,
                line=3,
            ),
            _symbol(
                method_three_id,
                kind=22,
                name_index=5,
                scope_index=6,
                file_index=1,
                line=3,
            ),
            _symbol(
                directory_collision_id,
                kind=12,
                name_index=7,
                scope_index=0,
                file_index=1,
                line=0,
            ),
        )
    )
    refs = b"".join(
        (
            _ref_group(
                caller_id,
                [(0x2 | 0x8, _location(1, 1, 4, end_column=10), zero_id)],
            ),
            _ref_group(
                target_id,
                [
                    (0x2 | 0x8, _location(1, 0, 4, end_column=10), zero_id),
                    (
                        0x4 | 0x8,
                        _location(1, 1, 22, end_column=28),
                        caller_id,
                    ),
                    (
                        0x4 | 0x8,
                        _location(1, 1, 22, end_column=28),
                        caller_id,
                    ),
                    (0x1 | 0x8, _location(1, 2, 4, end_column=10), zero_id),
                ],
            ),
            _ref_group(
                method_one_id,
                [(0x4 | 0x8, _location(1, 3, 0, end_column=5), caller_id)],
            ),
            _ref_group(
                method_two_id,
                [(0x4 | 0x8, _location(1, 3, 0, end_column=5), caller_id)],
            ),
        )
    )
    relations = _relation(target_id, 0, caller_id)
    (idx_directory / "main.cpp.TEST.idx").write_bytes(
        _riff_bytes(
            [
                (b"meta", struct.pack("<I", 18)),
                (b"stri", string_table),
                (b"symb", symbols),
                (b"refs", refs),
                (b"rela", relations),
            ]
        )
    )
    return root, idx_directory


def _definition_symbols(graph: CodeGraph) -> list[str]:
    return [
        name
        for name in graph.name_to_vertex
        if node_has_definition(graph.get_node_info_by_name(name) or {})
    ]


def _seeds(graph: CodeGraph) -> list[str]:
    seeds: list[str] = []
    for name in _definition_symbols(graph):
        info = graph.get_node_info_by_name(name) or {}
        display = str(info.get("unified_name") or name)
        bare = display.split(":")[-1].split(".")[-1].rstrip("()")
        seeds.extend((name, display, bare, f"`{bare}`"))
    seeds.append("definitely-missing-native-clangd-symbol")
    return list(dict.fromkeys(seed for seed in seeds if seed))


def _outcome(call: Callable[[], Any]) -> tuple[Any, ...]:
    try:
        return "ok", call()
    except Exception as exc:  # noqa: BLE001 - public error parity is required
        return "error", type(exc).__name__, str(exc)


def test_contract_and_graph_free_payload_are_explicit(clangd_fixture) -> None:
    root, idx_directory = clangd_fixture
    contract = codenib_core.clangd_fact_query_contract()
    _validate_native_query_contract(contract)

    payload = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(idx_directory), project_root=str(root)
    )
    index = payload["index"]

    assert contract == {
        "abi_version": 1,
        "format": "clangd-riff-fact-query-v1",
        "supported_versions": [18, 19, 20],
        "resource_limits": {
            "max_index_files": 200_000,
            "max_chunks_per_file": 128,
            "max_index_file_bytes": 512 * 1024 * 1024,
            "max_aggregate_index_bytes": 8 * 1024 * 1024 * 1024,
            "max_string_table_bytes": 256 * 1024 * 1024,
            "max_aggregate_string_table_bytes": 2 * 1024 * 1024 * 1024,
            "max_string_entries_per_file": 1_000_000,
            "max_aggregate_string_entries": 20_000_000,
            "max_materialized_string_bytes_per_file": 512 * 1024 * 1024,
            "max_aggregate_materialized_string_bytes": 4 * 1024 * 1024 * 1024,
            "max_records_per_file": 2_000_000,
            "max_aggregate_records": 25_000_000,
        },
        "stable_filename_order": True,
        "preserves_unanchored_relations": True,
        "capabilities": {
            "definition_by_symbol": True,
            "references_by_symbol": True,
            "position_queries": False,
            "route_queries": False,
        },
    }
    assert payload["graph_materialized"] is False
    assert payload["decode_profile_ns"]["materialize_graph"] == 0
    assert index.fact_query_index is True
    assert index.materializes_graph is False
    assert index.requires_anchored_references is False
    assert not hasattr(index, "graph")


@pytest.mark.parametrize("version", [18, 19, 20])
def test_symbol_results_errors_counts_and_relations_match_graph(
    clangd_fixture, version: int
) -> None:
    root, idx_directory = clangd_fixture
    if version != 18:
        _set_meta_version(idx_directory, 18, version)
    graph = ClangdGraphDecoder(str(idx_directory), str(root)).materialize_code_graph()
    payload = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(idx_directory), project_root=str(root)
    )
    index = payload["index"]
    expected_references = sum(
        edge.attributes().get("type") == EDGE_TYPE_REFERENCE for edge in graph.graph.es
    )

    assert index.record_count == graph.graph.vcount()
    assert index.edge_count == graph.graph.ecount()
    assert index.symbol_count == len(_definition_symbols(graph)) == 7
    assert index.reference_count == expected_references == 5

    for seed in _seeds(graph):
        assert _outcome(
            lambda seed=seed: lsp_definition(graph, symbol=seed, top_k=100)
        ) == _outcome(lambda seed=seed: lsp_definition(index, symbol=seed, top_k=100))
        for include_declaration in (False, True):
            assert _outcome(
                lambda seed=seed, include_declaration=include_declaration: (
                    lsp_references(
                        graph,
                        symbol=seed,
                        include_declaration=include_declaration,
                        top_k=100,
                    )
                )
            ) == _outcome(
                lambda seed=seed, include_declaration=include_declaration: (
                    lsp_references(
                        index,
                        symbol=seed,
                        include_declaration=include_declaration,
                        top_k=100,
                    )
                )
            )

    target = bytes.fromhex("1112131415161718").hex()
    assert any(
        node.file is None
        for node in lsp_references(
            index, symbol=target, include_declaration=False, top_k=100
        )
    )


def test_native_file_discovery_is_stable_by_filename(
    clangd_fixture, tmp_path: Path
) -> None:
    root, idx_directory = clangd_fixture
    original = next(idx_directory.glob("*.idx")).read_bytes()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "z.idx").write_bytes(original)
    (first / "a.idx").write_bytes(_empty_idx())
    (second / "a.idx").write_bytes(_empty_idx())
    (second / "z.idx").write_bytes(original)

    left = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(first), project_root=str(root)
    )["index"]
    right = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(second), project_root=str(root)
    )["index"]

    assert left.record_count == right.record_count
    assert left.edge_count == right.edge_count
    for seed in ("target", "Thing", "definitely-missing-native-clangd-symbol"):
        assert _outcome(
            lambda seed=seed: lsp_references(left, symbol=seed, top_k=100)
        ) == _outcome(lambda seed=seed: lsp_references(right, symbol=seed, top_k=100))


def test_native_reader_rejects_malformed_matrix(clangd_fixture) -> None:
    root, idx_directory = clangd_fixture
    path = next(idx_directory.glob("*.idx"))
    limits = codenib_core.clangd_fact_query_contract()["resource_limits"]
    minimal = _minimal_chunks()
    valid = _riff_bytes(minimal)
    symbol_id = bytes.fromhex("1112131415161718")
    valid_symbol = _symbol(
        symbol_id,
        kind=12,
        name_index=0,
        scope_index=0,
        file_index=0,
        line=0,
    )
    oversized_strings = limits["max_string_table_bytes"] + 1
    truncated_payload_body = b"CdIx" + b"meta" + struct.pack("<I", 4) + b"\x12\x00"
    cases = [
        ("short RIFF", b"RIFF", "not a RIFF file"),
        (
            "invalid outer length",
            b"RIFF" + struct.pack("<I", 3) + b"CdIx",
            "invalid RIFF length",
        ),
        ("trailing bytes", valid + b"x", "RIFF length does not match file size"),
        (
            "wrong form",
            b"RIFF" + struct.pack("<I", 4) + b"NOPE",
            "not a clangd CdIx index",
        ),
        (
            "truncated chunk header",
            b"RIFF" + struct.pack("<I", 8) + b"CdIxmeta",
            "truncated RIFF chunk header",
        ),
        (
            "truncated chunk payload",
            b"RIFF"
            + struct.pack("<I", len(truncated_payload_body))
            + truncated_payload_body,
            "truncated RIFF chunk payload",
        ),
        (
            "missing padding",
            b"RIFF" + struct.pack("<I", 13) + b"CdIxjunk" + struct.pack("<I", 1) + b"x",
            "missing RIFF padding byte",
        ),
        (
            "too many chunks",
            _riff_bytes(
                minimal + [(b"junk", b"")] * (limits["max_chunks_per_file"] - 1)
            ),
            "RIFF chunk count exceeds safety limit",
        ),
        ("missing meta", _riff_bytes([minimal[1]]), "missing required.*meta"),
        (
            "short meta",
            _riff_bytes([(b"meta", b"\x12\x00\x00"), minimal[1]]),
            "meta chunk length",
        ),
        (
            "unsupported version",
            _riff_bytes(_minimal_chunks(21)),
            "unsupported clangd RIFF version 21",
        ),
        ("missing strings", _riff_bytes([minimal[0]]), "missing required.*stri"),
        (
            "bad zlib stream",
            _riff_bytes([minimal[0], (b"stri", struct.pack("<I", 16) + b"bad")]),
            "cannot decompress",
        ),
        (
            "zlib trailing data",
            _riff_bytes(
                [
                    minimal[0],
                    (b"stri", struct.pack("<I", 1) + zlib.compress(b"\x00") + b"x"),
                ]
            ),
            "cannot decompress",
        ),
        (
            "oversized string declaration",
            _riff_bytes([minimal[0], (b"stri", struct.pack("<I", oversized_strings))]),
            "string table exceeds per-file safety limit",
        ),
        (
            "varint overflow",
            _riff_bytes(
                minimal + [(b"symb", symbol_id + bytes((12, 2)) + (b"\x80" * 10))]
            ),
            "varint exceeds 10 bytes",
        ),
        (
            "string index",
            _riff_bytes(
                minimal
                + [
                    (
                        b"symb",
                        _symbol(
                            symbol_id,
                            kind=12,
                            name_index=9,
                            scope_index=0,
                            file_index=0,
                            line=0,
                        ),
                    )
                ]
            ),
            "string index is out of range",
        ),
        (
            "include header count",
            _riff_bytes(minimal + [(b"symb", valid_symbol[:-1] + _varint(100))]),
            "invalid clangd include-header count",
        ),
        (
            "reference count",
            _riff_bytes(minimal + [(b"refs", symbol_id + _varint(2))]),
            "invalid clangd reference count",
        ),
        (
            "relation record",
            _riff_bytes(minimal + [(b"rela", b"x" * 16)]),
            "truncated clangd relation record",
        ),
    ]

    for name, payload, message in cases:
        path.write_bytes(payload)
        errors = []
        for _attempt in range(2):
            with pytest.raises(RuntimeError, match=message) as raised:
                codenib_core.decode_clangd_fact_query_index(
                    idx_directory=str(idx_directory), project_root=str(root)
                )
            errors.append(str(raised.value))
        assert errors[0] == errors[1], name
        assert path.name in errors[0], name


@pytest.mark.parametrize(
    "chunk_id", [b"meta", b"srcs", b"stri", b"symb", b"refs", b"rela", b"cmdl"]
)
def test_native_reader_rejects_duplicate_known_chunks(
    clangd_fixture, chunk_id: bytes
) -> None:
    root, idx_directory = clangd_fixture
    path = next(idx_directory.glob("*.idx"))
    payload = struct.pack("<I", 18) if chunk_id == b"meta" else b""
    if chunk_id == b"stri":
        payload = struct.pack("<I", 0) + b"\x00"
    chunks = _minimal_chunks()
    chunks.extend(((chunk_id, payload), (chunk_id, payload)))
    path.write_bytes(_riff_bytes(chunks))

    with pytest.raises(RuntimeError, match="duplicate clangd RIFF chunk"):
        codenib_core.decode_clangd_fact_query_index(
            idx_directory=str(idx_directory), project_root=str(root)
        )


def test_native_reader_rejects_resource_sizes_before_reading(
    clangd_fixture, tmp_path: Path
) -> None:
    root, idx_directory = clangd_fixture
    limits = codenib_core.clangd_fact_query_contract()["resource_limits"]
    path = next(idx_directory.glob("*.idx"))
    with path.open("wb") as stream:
        stream.truncate(limits["max_index_file_bytes"] + 1)

    with pytest.raises(RuntimeError, match="per-file safety limit"):
        codenib_core.decode_clangd_fact_query_index(
            idx_directory=str(idx_directory), project_root=str(root)
        )

    aggregate_directory = tmp_path / "aggregate-index"
    aggregate_directory.mkdir()
    file_size = limits["max_index_file_bytes"]
    file_count = limits["max_aggregate_index_bytes"] // file_size + 1
    for index in range(file_count):
        with (aggregate_directory / f"{index}.idx").open("wb") as stream:
            stream.truncate(file_size)

    with pytest.raises(RuntimeError, match="aggregate index bytes"):
        codenib_core.decode_clangd_fact_query_index(
            idx_directory=str(aggregate_directory), project_root=str(root)
        )


def test_native_reader_caps_string_object_expansion(clangd_fixture) -> None:
    root, idx_directory = clangd_fixture
    limits = codenib_core.clangd_fact_query_contract()["resource_limits"]
    path = next(idx_directory.glob("*.idx"))
    too_many_strings = b"\x00" * (limits["max_string_entries_per_file"] + 1)
    path.write_bytes(
        _riff_bytes(
            [
                (b"meta", struct.pack("<I", 18)),
                (b"stri", struct.pack("<I", 0) + too_many_strings),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="per-file entry limit"):
        codenib_core.decode_clangd_fact_query_index(
            idx_directory=str(idx_directory), project_root=str(root)
        )


def test_unsupported_version_auto_required_and_off_modes(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    _set_meta_version(idx_directory, 18, 21)
    monkeypatch.setattr(clangd_decode, "_NATIVE_QUERY_PROMOTED", True)

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "auto")
    fallback = ClangdGraphDecoder(str(idx_directory), str(root))
    graph = fallback.decode_query_index()
    assert isinstance(graph, CodeGraph)
    assert fallback.query_backend == "legacy-query-fallback"
    assert "unsupported clangd RIFF version 21" in fallback.query_fallback_error

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "required")
    with pytest.raises(RuntimeError, match="unsupported clangd RIFF version 21"):
        ClangdGraphDecoder(str(idx_directory), str(root)).decode_query_index()

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "off")
    disabled = ClangdGraphDecoder(str(idx_directory), str(root))
    assert isinstance(disabled.decode_query_index(), CodeGraph)
    assert disabled.query_backend == "legacy-query-disabled"


def test_selector_auto_off_required_and_failure_modes(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    monkeypatch.delenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", raising=False)
    monkeypatch.setattr(clangd_decode, "_NATIVE_QUERY_PROMOTED", True)
    automatic = ClangdGraphDecoder(str(idx_directory), str(root))

    index = automatic.decode_query_index()

    assert index.fact_query_index is True
    assert automatic.query_backend == "native-clangd-fact-query-v1"
    assert automatic._graph_materialized is False
    assert automatic.query_profile["fact_query_native"] == 1

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "off")
    disabled = ClangdGraphDecoder(str(idx_directory), str(root))
    assert isinstance(disabled.decode_query_index(), CodeGraph)
    assert disabled.query_backend == "legacy-query-disabled"
    assert disabled._graph_materialized is True

    def fail(**_kwargs):
        raise RuntimeError("injected native clangd failure")

    monkeypatch.setattr(codenib_core, "decode_clangd_fact_query_index", fail)
    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "auto")
    fallback = ClangdGraphDecoder(str(idx_directory), str(root))
    assert isinstance(fallback.decode_query_index(), CodeGraph)
    assert fallback.query_backend == "legacy-query-fallback"
    assert "injected native clangd failure" in fallback.query_fallback_error

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "required")
    with pytest.raises(RuntimeError, match="injected native clangd failure"):
        ClangdGraphDecoder(str(idx_directory), str(root)).decode_query_index()


def test_selector_does_not_mask_memory_exhaustion_or_unknown_mode(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture

    def exhaust(**_kwargs):
        raise MemoryError("injected allocation failure")

    monkeypatch.setattr(codenib_core, "decode_clangd_fact_query_index", exhaust)
    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "auto")
    with pytest.raises(MemoryError, match="injected allocation failure"):
        ClangdGraphDecoder(str(idx_directory), str(root)).decode_query_index()

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "sometimes")
    with pytest.raises(
        ValueError, match="CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX must be"
    ):
        ClangdGraphDecoder(str(idx_directory), str(root)).decode_query_index()


def test_hybrid_uses_native_symbols_then_materializes_graph_once(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    monkeypatch.delenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", raising=False)
    graph = ClangdGraphDecoder(str(idx_directory), str(root)).materialize_code_graph()
    legacy = StaticLSPProvider(graph)
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_query_provider()
    target = bytes.fromhex("1112131415161718").hex()

    assert isinstance(provider, ClangdHybridQueryProvider)
    assert [node.model_dump() for node in provider.definition(symbol=target)] == [
        node.model_dump() for node in legacy.definition(symbol=target)
    ]
    assert decoder._graph_materialized is False

    position_arguments = {
        "file_path": "src/main.cpp",
        "line": 1,
        "character": 22,
        "top_k": 100,
    }
    assert [
        node.model_dump() for node in provider.definition(**position_arguments)
    ] == [node.model_dump() for node in legacy.definition(**position_arguments)]
    assert decoder._graph_materialized is True
    assert provider.graph_materialization_count == 1

    route_arguments = {
        "symbols": [target],
        "query": "target caller",
        "top_k": 10,
        "include_neighbors": True,
    }
    assert [node.model_dump() for node in provider.route(**route_arguments)] == [
        node.model_dump() for node in legacy.route(**route_arguments)
    ]
    assert provider.graph_materialization_count == 1


def test_ls_indexer_exposes_query_index_and_provider(
    clangd_fixture, tmp_path: Path, monkeypatch
) -> None:
    root, _idx_directory = clangd_fixture
    monkeypatch.delenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", raising=False)
    indexer = LSIndexer(root, output_dir=tmp_path / "output", language="cpp")

    index = indexer.process_query_index()
    provider = indexer.process_query_provider()

    assert index.fact_query_index is True
    assert isinstance(provider, ClangdHybridQueryProvider)
    assert provider.decoder._graph_materialized is False


def test_profiler_records_parity_samples_order_and_contract(
    clangd_fixture,
) -> None:
    root, idx_directory = clangd_fixture

    report = profile_clangd_fact_query_index(
        idx_directory=idx_directory,
        project_root=root,
        iterations=2,
        warmups=0,
        query_workload_size=4,
        parity_sample_limit=7,
    )

    assert report["schema_version"] == 1
    assert report["benchmark"] == "native_clangd_fact_query_index_v1"
    assert report["parity"] == {"passed": True, "error": None}
    assert report["configuration"]["first_arm_by_iteration"] == [
        "legacy",
        "native",
    ]
    assert report["configuration"]["clangd_fact_query_contract"] == (
        codenib_core.clangd_fact_query_contract()
    )
    assert len(report["raw_samples"]["legacy_clangd_graph"]["total"]) == 2
    assert len(report["raw_samples"]["native_clangd_fact_query_index"]["total"]) == 2
    assert report["decision"]["parity"] is True


def test_profiler_cli_writes_json(clangd_fixture, tmp_path: Path, capsys) -> None:
    root, idx_directory = clangd_fixture
    output = tmp_path / "clangd-query.json"

    result = profile_main(
        [
            "--idx-directory",
            str(idx_directory),
            "--project-root",
            str(root),
            "--iterations",
            "1",
            "--warmups",
            "0",
            "--query-workload-size",
            "2",
            "--parity-sample-limit",
            "2",
            "--output-json",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads(
        capsys.readouterr().out
    )
