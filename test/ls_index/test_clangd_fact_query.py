# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Native clangd symbol/position parity, fallback, and benchmark tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

if importlib.util.find_spec("codenib_core") is None:
    pytest.skip(
        "codenib_core pybind module not built; run make core-build",
        allow_module_level=True,
    )

import codenib_core  # noqa: E402

from codenib.agent.lsp_graph import lsp_definition  # noqa: E402
from codenib.agent.lsp_graph import lsp_references
from codenib.agent.lsp_provider import CAPABILITY_DEFINITION  # noqa: E402
from codenib.agent.lsp_provider import CAPABILITY_ROUTE, StaticLSPProvider
from codenib.compiler.manifest import RepoManifest  # noqa: E402
from codenib.graph.code_graph import CodeGraph  # noqa: E402
from codenib.graph.incremental import patcher_cpp as patcher_cpp_module  # noqa: E402
from codenib.ls_index import clangd_decode  # noqa: E402
from codenib.ls_index import clangd_indexer as clangd_indexer_module  # noqa: E402
from codenib.ls_index.clangd_contract import (  # noqa: E402
    clangd_background_index_command,
    normalize_clangd_position_encoding,
)
from codenib.ls_index.clangd_decode import ClangdGraphDecoder  # noqa: E402
from codenib.ls_index.clangd_decode import (
    ClangdHybridQueryProvider,
    _validate_native_query_contract,
)
from codenib.ls_router import LSIndexer  # noqa: E402
from codenib.mcp.context import ServerContext  # noqa: E402
from codenib.mcp.tools.lsp import lsp_definition_impl  # noqa: E402
from codenib.mcp.tools.lsp import lsp_references_impl, lsp_route_impl
from codenib.types import EDGE_TYPE_REFERENCE  # noqa: E402
from codenib.types import node_has_definition
from scripts.profiling.profile_clangd_fact_query_index import (  # noqa: E402
    main as profile_main,
)
from scripts.profiling.profile_clangd_fact_query_index import (  # noqa: E402
    profile_clangd_fact_query_index,
)
from scripts.profiling.profile_clangd_workload_gate import (  # noqa: E402
    _query_ready_gate,
    profile_clangd_workload_gate,
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
    path = (
        idx_directory if idx_directory.is_file() else next(idx_directory.glob("*.idx"))
    )
    data = path.read_bytes()
    old = b"meta" + struct.pack("<I", 4) + struct.pack("<I", before)
    new = b"meta" + struct.pack("<I", 4) + struct.pack("<I", after)
    assert data.count(old) == 1
    path.write_bytes(data.replace(old, new, 1))


def _expected_snapshot_id(
    idx_directory: Path, project_root: Path, position_encoding: str = "UTF16"
) -> str:
    digest = hashlib.sha256()

    def field(value: str) -> None:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)

    def uint64(value: int) -> None:
        digest.update(value.to_bytes(8, "little"))

    field("CodeNib clangd fact query snapshot")
    field("clangd-fact-query-snapshot-v1")
    field("clangd-riff-fact-query-v3")
    uint64(3)
    field("clangd-native-normalization-v3")
    field(project_root.absolute().as_posix())
    field(position_encoding)
    uint64(3)
    for version in (18, 19, 20):
        uint64(version)
    files = sorted(idx_directory.glob("*.idx"), key=lambda path: path.name)
    uint64(len(files))
    for path in files:
        data = path.read_bytes()
        field(path.name)
        uint64(len(data))
        digest.update(data)
    return f"clangd_fact_query:sha256:{digest.hexdigest()}"


@pytest.fixture()
def clangd_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    source = root / "src" / "main.cpp"
    source.parent.mkdir(parents=True)
    source.write_text(
        "int target() { return 1; }\n"
        "int caller() { return target(); }\n"
        "int target();\n"
        "Thing value;\n"
        "int clean() { return 0; }\n",
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
    clean_id = bytes.fromhex("7172737475767778")
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
        "clean",
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
            _symbol(
                clean_id,
                kind=12,
                name_index=8,
                scope_index=4,
                file_index=1,
                line=4,
                column=4,
                end_column=9,
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
        "abi_version": 3,
        "format": "clangd-riff-fact-query-v3",
        "normalization_profile": "clangd-native-normalization-v3",
        "snapshot_schema": "clangd-fact-query-snapshot-v1",
        "snapshot_digest": "sha256",
        "supported_versions": [18, 19, 20],
        "default_position_encoding": "UTF16",
        "supported_position_encodings": ["UTF8", "UTF16", "UTF32"],
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
            "position_queries": True,
            "route_queries": True,
        },
    }
    assert payload["graph_materialized"] is False
    assert payload["decode_profile_ns"]["materialize_graph"] == 0
    assert index.fact_query_index is True
    assert index.materializes_graph is False
    assert index.requires_anchored_references is False
    assert index.capabilities["position_queries"] is True
    assert index.capabilities["route_queries"] is True
    assert index.supports_route_adjacency is True
    assert index.supports_graph_routes is False
    assert index.position_encoding == "UTF16"
    assert index.occurrence_count > 0
    assert not hasattr(index, "graph")


def test_full_and_incremental_generation_share_utf16_contract(
    clangd_fixture, tmp_path: Path
) -> None:
    root, _idx_directory = clangd_fixture
    indexer = clangd_indexer_module.ClangdIndexer(root, output_dir=tmp_path / "out")
    indexer._clangd_path = "/opt/clangd"
    comp_db = root / "build" / "compile_commands.json"

    expected = [
        "/opt/clangd",
        "--background-index",
        f"--compile-commands-dir={comp_db.parent}",
        "--background-index-priority=normal",
        "--offset-encoding=utf-16",
        "--log=error",
    ]
    assert indexer._build_index_command(comp_db) == expected
    assert list(clangd_background_index_command("/opt/clangd", comp_db.parent)) == (
        expected
    )
    assert (
        patcher_cpp_module.clangd_background_index_command
        is clangd_background_index_command
    )
    assert normalize_clangd_position_encoding("utf-8") == "UTF8"
    assert normalize_clangd_position_encoding("UTF_32") == "UTF32"
    with pytest.raises(ValueError, match="must be one of"):
        normalize_clangd_position_encoding("bytes")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("normalization_profile", "changed", "normalization profile mismatch"),
        ("snapshot_schema", "changed", "snapshot schema mismatch"),
        ("snapshot_digest", "sha1", "snapshot digest mismatch"),
    ],
)
def test_native_snapshot_contract_fails_closed(
    field: str, value: str, message: str
) -> None:
    contract = dict(codenib_core.clangd_fact_query_contract())
    contract[field] = value

    with pytest.raises(RuntimeError, match=message):
        _validate_native_query_contract(contract)


def test_native_snapshot_receipt_matches_canonical_recipe(clangd_fixture) -> None:
    root, idx_directory = clangd_fixture

    payload = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(idx_directory), project_root=str(root)
    )
    receipt = codenib_core.clangd_fact_query_snapshot(
        idx_directory=str(idx_directory), project_root=str(root)
    )
    expected = _expected_snapshot_id(idx_directory, root)

    assert payload["snapshot_id"] == expected
    assert payload["index"].snapshot_id == expected
    assert receipt == {
        "snapshot_id": expected,
        "file_count": 1,
        "index_bytes": next(idx_directory.glob("*.idx")).stat().st_size,
    }
    assert payload["decode_profile_ns"]["hash_index"] > 0
    assert payload["decode_profile_ns"]["verify_snapshot"] > 0


def test_position_encoding_is_explicit_and_snapshot_bound(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture

    default = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(idx_directory), project_root=str(root)
    )
    utf8 = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(idx_directory),
        project_root=str(root),
        position_encoding="utf-8",
    )

    assert default["index"].position_encoding == "UTF16"
    assert utf8["index"].position_encoding == "UTF8"
    assert default["snapshot_id"] != utf8["snapshot_id"]
    assert utf8["snapshot_id"] == _expected_snapshot_id(idx_directory, root, "UTF8")

    monkeypatch.setenv("CODENIB_CLANGD_POSITION_ENCODING", "utf-32")
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    assert decoder.position_encoding == "UTF32"
    assert decoder.decode_native_query_provider().position_encoding == "UTF32"
    assert list(clangd_background_index_command("clangd", root))[-2] == (
        "--offset-encoding=utf-32"
    )

    monkeypatch.setenv("CODENIB_CLANGD_POSITION_ENCODING", "bytes")
    with pytest.raises(ValueError, match="must be UTF8, UTF16, or UTF32"):
        ClangdGraphDecoder(str(idx_directory), str(root))


def test_native_snapshot_is_order_independent_and_content_bound(
    clangd_fixture, tmp_path: Path
) -> None:
    root, idx_directory = clangd_fixture
    original = next(idx_directory.glob("*.idx")).read_bytes()
    extra = _riff_bytes(_minimal_chunks(19))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.idx").write_bytes(original)
    (first / "b.idx").write_bytes(extra)
    (second / "b.idx").write_bytes(extra)
    (second / "a.idx").write_bytes(original)

    def snapshot(directory: Path, project: Path = root) -> str:
        return codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(directory), project_root=str(project)
        )["snapshot_id"]

    baseline = snapshot(first)
    assert snapshot(second) == baseline

    (second / "a.idx").rename(second / "renamed.idx")
    assert snapshot(second) != baseline
    (second / "renamed.idx").rename(second / "a.idx")
    assert snapshot(second) == baseline

    content_path = second / "a.idx"
    content = bytearray(content_path.read_bytes())
    timestamps = content_path.stat()
    content[-1] ^= 1
    content_path.write_bytes(content)
    os.utime(
        content_path,
        ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns),
    )
    assert snapshot(second) != baseline
    content_path.write_bytes(original)
    assert snapshot(second) == baseline

    _set_meta_version(second / "a.idx", 18, 20)
    assert snapshot(second) != baseline

    other_root = tmp_path / "other-root"
    other_root.mkdir()
    assert snapshot(first, other_root) != baseline


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
    assert index.symbol_count == len(_definition_symbols(graph)) == 8
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


def test_hybrid_serves_symbols_positions_and_routes_without_graph(
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
    native_result = provider.definition(symbol=target)
    assert [node.model_dump() for node in native_result] == [
        node.model_dump() for node in legacy.definition(symbol=target)
    ]
    assert provider.snapshot_id.startswith("clangd_fact_query:sha256:")
    assert native_result.provider_metadata_dict()["index_snapshot"] == (
        provider.snapshot_id
    )
    assert decoder._graph_materialized is False

    position_arguments = {
        "file_path": "src/main.cpp",
        "line": 1,
        "character": 22,
        "top_k": 100,
    }
    position_result = provider.definition(**position_arguments)
    assert [node.model_dump() for node in position_result] == [
        node.model_dump() for node in legacy.definition(**position_arguments)
    ]
    assert position_result.provider_metadata_dict()["index_snapshot"] == (
        provider.snapshot_id
    )
    assert position_result.provider_metadata_dict()["behavior_contract"] == (
        "static_native_occurrence_lsp_v1"
    )
    assert position_result.provider_metadata_dict()["position_encoding"] == "UTF16"
    assert decoder._graph_materialized is False
    assert provider.graph_materialization_count == 0

    route_arguments = {
        "symbols": [target],
        "query": "target caller",
        "top_k": 10,
        "include_neighbors": True,
    }
    route_result = provider.route(**route_arguments)
    assert [node.model_dump() for node in route_result] == [
        node.model_dump() for node in legacy.route(**route_arguments)
    ]
    assert route_result.provider_metadata_dict()["backend"] == (
        "native-clangd-route-adjacency-v1"
    )
    assert provider.graph_materialization_count == 0
    assert decoder._graph_materialized is False


def test_static_provider_marks_raw_fact_routes_unavailable(clangd_fixture) -> None:
    root, idx_directory = clangd_fixture
    index = codenib_core.decode_clangd_fact_query_index(
        idx_directory=str(idx_directory), project_root=str(root)
    )["index"]

    provider = StaticLSPProvider(index)

    assert provider.can_serve(CAPABILITY_DEFINITION).status == "ok"
    route = provider.can_serve(CAPABILITY_ROUTE)
    assert route.status == "unavailable"
    assert route.fallback_reason == "fact_index_does_not_support_routes"
    assert index.supports_route_adjacency is True


def test_native_route_view_matches_legacy_order_adjacency_and_results(
    clangd_fixture,
) -> None:
    root, idx_directory = clangd_fixture
    graph = ClangdGraphDecoder(str(idx_directory), str(root)).materialize_code_graph()
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    native = decoder.decode_native_query_provider()
    index = decoder.query_index

    assert index.iter_route_names(10_000) == list(graph.name_to_vertex)
    for name in graph.name_to_vertex:
        legacy_successors = [
            graph.get_node_info_by_id(vertex_id)["name"]
            for vertex_id in graph.get_successors(name)
        ]
        native_successors = [
            index.get_node_info_by_id(vertex_id)["name"]
            for vertex_id in index.get_successors(name)
        ]
        legacy_predecessors = [
            graph.get_node_info_by_id(vertex_id)["name"]
            for vertex_id in graph.get_predecessors(name)
        ]
        native_predecessors = [
            index.get_node_info_by_id(vertex_id)["name"]
            for vertex_id in index.get_predecessors(name)
        ]
        assert native_successors == legacy_successors
        assert native_predecessors == legacy_predecessors

    legacy = StaticLSPProvider(graph)
    cases = (
        {
            "symbols": ["1112131415161718"],
            "query": "target caller",
            "top_k": 10,
            "include_neighbors": True,
        },
        {
            "symbols": [],
            "query": "demo target",
            "top_k": 10,
            "include_neighbors": True,
        },
        {
            "symbols": ["target"],
            "query": None,
            "top_k": 10,
            "include_neighbors": False,
        },
    )
    for arguments in cases:
        expected = [node.model_dump() for node in legacy.route(**arguments)]
        observed = [node.model_dump() for node in native.route(**arguments)]
        assert observed == expected
    assert native.graph_materialization_count == 0
    assert decoder._graph_materialized is False


def test_native_query_route_failure_falls_back_without_partial_results(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    legacy_graph = ClangdGraphDecoder(
        str(idx_directory), str(root)
    ).materialize_code_graph()
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_native_query_provider()

    def fail_preparation(*, query: str, top_k: int):
        raise RuntimeError(f"injected route failure: {query}/{top_k}")

    monkeypatch.setattr(provider._route_view, "prepare_query_route", fail_preparation)
    arguments = {
        "symbols": [],
        "query": "demo target",
        "top_k": 10,
        "include_neighbors": True,
    }

    observed = provider.route(**arguments)
    expected = StaticLSPProvider(legacy_graph).route(**arguments)

    assert [node.model_dump() for node in observed] == [
        node.model_dump() for node in expected
    ]
    assert observed.provider_metadata_dict()["backend"] == ("lazy-clangd-code-graph-v1")
    assert observed.provider_metadata_dict()["fallback_reason"] == (
        "native_clangd_route_adjacency_failed"
    )
    assert provider.graph_materialization_count == 1
    assert decoder._graph_materialized is True


def test_native_position_queries_match_legacy_without_materializing_graph(
    clangd_fixture,
) -> None:
    root, idx_directory = clangd_fixture
    graph = ClangdGraphDecoder(str(idx_directory), str(root)).materialize_code_graph()
    legacy = StaticLSPProvider(graph)
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    native = decoder.decode_native_query_provider()
    assert decoder.query_index.occurrence_count == 0
    assert native.position_initialization_count == 0

    queries = [
        ("definition", {"file_path": "src/main.cpp", "line": 1, "character": 22}),
        (
            "references",
            {
                "file_path": "src/main.cpp",
                "line": 4,
                "character": 4,
                "include_declaration": True,
            },
        ),
        (
            "references",
            {
                "file_path": "src/main.cpp",
                "line": 4,
                "character": 4,
                "include_declaration": False,
            },
        ),
        ("definition", {"file_path": "src/main.cpp", "line": 0, "character": 4}),
    ]
    for capability, arguments in queries:
        expected = getattr(legacy, capability)(**arguments, top_k=100)
        observed = getattr(native, capability)(**arguments, top_k=100)
        assert [node.model_dump() for node in observed] == [
            node.model_dump() for node in expected
        ]
        metadata = observed.provider_metadata_dict()
        assert metadata["backend"] == "native-clangd-fact-query-v1", (
            capability,
            arguments,
            native.last_position_fallback_reason,
        )
        assert metadata["behavior_contract"] == "static_native_occurrence_lsp_v1"
        assert metadata["position_granularity"] == "character"
        assert metadata["position_encoding"] == "UTF16"
        assert decoder._graph_materialized is False

    index = decoder.position_query_index
    assert index is not None
    assert index.has_occurrence_at("src/main.cpp", 1, 22) is True
    assert index.has_occurrence_at("src/main.cpp", 1, 27) is True
    assert index.has_occurrence_at("src/main.cpp", 1, 28) is False
    declaration = index.position_definitions("src/main.cpp", 2, 4)
    assert declaration["served"] is False
    assert declaration["fallback_reason"] == (
        "native_position_declaration_requires_legacy_graph"
    )
    assert native.position_initialization_count == 1
    assert native.graph_materialization_count == 0


def test_native_position_missing_source_falls_back_deterministically(
    clangd_fixture,
) -> None:
    root, idx_directory = clangd_fixture
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_native_query_provider()
    position_index = decoder.decode_native_position_query_index()
    (root / "src" / "main.cpp").unlink()

    decision = position_index.position_definitions("src/main.cpp", 1, 22)
    assert decision == {
        "served": False,
        "fallback_reason": "native_position_source_unavailable",
        "locations": [],
        "targets": [],
    }
    with pytest.raises(ValueError, match="no indexed definition token"):
        provider.definition(file_path="src/main.cpp", line=1, character=22, top_k=10)
    assert provider.last_position_fallback_reason == (
        "native_position_source_unavailable"
    )
    assert decoder._graph_materialized is True


def test_concurrent_first_native_routes_remain_graph_free(
    clangd_fixture, monkeypatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    root, idx_directory = clangd_fixture
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_native_query_provider()
    build_graph = decoder._build_graph
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(8)

    def counted_build_graph():
        nonlocal calls
        with calls_lock:
            calls += 1
        return build_graph()

    monkeypatch.setattr(decoder, "_build_graph", counted_build_graph)
    target = bytes.fromhex("1112131415161718").hex()

    def route_once(_index: int):
        start.wait()
        result = provider.route(
            symbols=[target],
            query="concurrent first route",
            top_k=100,
            include_neighbors=True,
        )
        return (
            [node.model_dump() for node in result],
            result.provider_metadata_dict(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(route_once, range(8)))

    assert calls == 0
    assert provider.graph_materialization_count == 0
    assert decoder._graph_materialized is False
    assert all(result == results[0] for result in results)
    assert results[0][1]["backend"] == "native-clangd-route-adjacency-v1"
    position_after_route = provider.definition(
        file_path="src/main.cpp", line=1, character=22, top_k=100
    )
    assert position_after_route.provider_metadata_dict()["backend"] == (
        "native-clangd-fact-query-v1"
    )
    assert provider.position_initialization_count == 1
    assert provider.graph_materialization_count == 0


def test_concurrent_first_position_initializes_exactly_once(
    clangd_fixture, monkeypatch
) -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    root, idx_directory = clangd_fixture
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_native_query_provider()
    initialize = decoder.decode_native_position_query_index
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(8)

    def counted_initialize():
        nonlocal calls
        time.sleep(0.02)
        with calls_lock:
            calls += 1
        return initialize()

    monkeypatch.setattr(
        decoder, "decode_native_position_query_index", counted_initialize
    )

    def definition_once(_index: int):
        start.wait()
        result = provider.definition(
            file_path="src/main.cpp", line=1, character=22, top_k=100
        )
        return (
            [node.model_dump() for node in result],
            result.provider_metadata_dict(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(definition_once, range(8)))

    assert all(result == results[0] for result in results)
    assert calls == 1
    assert provider.position_initialization_count == 1
    assert provider.graph_materialization_count == 0
    assert decoder._graph_materialized is False


def test_lazy_graph_fails_closed_when_snapshot_changes(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    monkeypatch.delenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", raising=False)
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_query_provider()
    original_snapshot = provider.snapshot_id

    _set_meta_version(idx_directory, 18, 19)

    with pytest.raises(RuntimeError, match="snapshot changed before native route"):
        provider.route(symbols=["1112131415161718"], top_k=10)
    assert decoder._graph_materialized is False
    assert provider.snapshot_id == original_snapshot


def test_lazy_graph_detects_mutation_during_record_collection(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_query_provider()
    provider._route_provider = None
    collect = decoder._collect_all_idx

    def collect_then_mutate() -> None:
        collect()
        _set_meta_version(idx_directory, 18, 19)

    monkeypatch.setattr(decoder, "_collect_all_idx", collect_then_mutate)

    with pytest.raises(
        RuntimeError, match="snapshot changed after legacy record collection"
    ):
        provider.route(symbols=["1112131415161718"], top_k=10)
    assert decoder._graph_materialized is False


def test_lazy_graph_detects_index_file_list_changes(clangd_fixture) -> None:
    root, idx_directory = clangd_fixture
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_query_provider()
    (idx_directory / "new.idx").write_bytes(_empty_idx())

    with pytest.raises(RuntimeError, match="snapshot changed"):
        provider.route(symbols=["1112131415161718"], top_k=10)
    assert decoder._graph_materialized is False


def test_native_snapshot_cannot_bind_a_preexisting_graph(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    graph = decoder.materialize_code_graph()
    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "required")

    with pytest.raises(RuntimeError, match="records collected before the receipt"):
        decoder.decode_query_index()
    assert decoder.query_snapshot_id is None

    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "auto")
    assert decoder.decode_query_index() is graph
    assert decoder.query_backend == "legacy-query-fallback"


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


def test_real_mcp_boundary_keeps_supported_native_queries_graph_free(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    monkeypatch.delenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", raising=False)
    legacy_graph = ClangdGraphDecoder(
        str(idx_directory), str(root)
    ).materialize_code_graph()
    legacy = SimpleNamespace(
        lsp_provider=StaticLSPProvider(legacy_graph),
        symbol_graph=legacy_graph,
    )
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))
    provider = decoder.decode_native_query_provider()
    context = SimpleNamespace(lsp_provider=provider, symbol_graph=None)
    target = bytes.fromhex("1112131415161718").hex()

    definitions = lsp_definition_impl(context, symbol=target, top_k=10)
    references = lsp_references_impl(context, symbol=target, top_k=10)

    assert decoder._graph_materialized is False
    assert provider.graph_materialization_count == 0
    assert definitions[0]["lsp_provider"]["backend"] == ("native-clangd-fact-query-v1")
    assert definitions[0]["lsp_provider"]["index_snapshot"] == (provider.snapshot_id)
    assert references[0]["lsp_provider"]["backend"] == ("native-clangd-fact-query-v1")

    position_arguments = {
        "file_path": "src/main.cpp",
        # MCP positions are one-based at this boundary; the provider receives
        # the zero-based clangd occurrence on source line 1.
        "line": 2,
        "character": 22,
        "top_k": 10,
    }
    position = lsp_definition_impl(context, **position_arguments)
    expected_position = lsp_definition_impl(legacy, **position_arguments)

    assert [
        {key: value for key, value in row.items() if key != "lsp_provider"}
        for row in position
    ] == expected_position
    assert position[0]["lsp_provider"]["backend"] == ("native-clangd-fact-query-v1")
    assert position[0]["lsp_provider"]["position_encoding"] == "UTF16"
    assert decoder._graph_materialized is False
    assert provider.graph_materialization_count == 0

    route_arguments = {
        "symbols": [target],
        "query": "target caller",
        "top_k": 10,
    }
    route = lsp_route_impl(context, **route_arguments)
    expected_route = lsp_route_impl(legacy, **route_arguments)

    assert [
        {key: value for key, value in row.items() if key != "lsp_provider"}
        for row in route
    ] == expected_route
    assert route[0]["lsp_provider"]["backend"] == ("native-clangd-route-adjacency-v1")
    assert provider.graph_materialization_count == 0
    assert decoder._graph_materialized is False


def test_server_context_uses_persisted_graph_for_manifest_source_policy(
    clangd_fixture, monkeypatch
) -> None:
    root, idx_directory = clangd_fixture
    monkeypatch.delenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", raising=False)
    graph = ClangdGraphDecoder(str(idx_directory), str(root)).materialize_code_graph()
    context = ServerContext(
        manifest=RepoManifest(repo_path=str(root), languages=["cpp"]),
        symbol_graph=graph,
    )

    manifest_bound = context.configure_lsp_provider(allow_native=True)

    assert manifest_bound["backend"] == "persisted-symbol-graph-v1"
    assert manifest_bound["fallback_reason"] == (
        "repository_source_policy_requires_persisted_graph"
    )
    assert manifest_bound["capabilities"] == {
        "definition": True,
        "references": True,
        "route": True,
    }
    assert context.lsp_provider.graph is graph

    portable = context.configure_lsp_provider(
        allow_native=False,
        native_disabled_reason="portable_artifact_uses_persisted_graph",
    )

    assert portable["backend"] == "persisted-symbol-graph-v1"
    assert portable["fallback_reason"] == "portable_artifact_uses_persisted_graph"
    assert context.lsp_provider.graph is graph


@pytest.mark.parametrize(
    ("native", "parity", "graph_free", "expected"),
    [
        (0.8, True, None, True),
        (0.8, True, True, True),
        (math.nextafter(0.8, math.inf), True, True, False),
        (0.5, False, True, False),
        (0.5, True, False, False),
    ],
)
def test_query_ready_gate_has_exact_threshold_and_structural_requirements(
    native: float,
    parity: bool,
    graph_free: bool | None,
    expected: bool,
) -> None:
    gate = _query_ready_gate(
        legacy=1.0,
        native=native,
        threshold=0.2,
        parity=parity,
        graph_free=graph_free,
    )

    assert gate["threshold"] == 0.2
    assert gate["improvement_fraction"] == pytest.approx(1.0 - native)
    assert ("graph_free" in gate) is (graph_free is not None)
    if graph_free is not None:
        assert gate["graph_free"] is graph_free
    assert gate["passed"] is expected
    if expected:
        # The quotient is fractionally below 0.2 in binary floating point;
        # the multiplicative cutoff must still accept the exact boundary.
        assert gate["improvement_fraction"] < gate["threshold"]


def test_process_isolated_workload_gate_reports_rss_parity_and_concurrency(
    clangd_fixture,
) -> None:
    root, idx_directory = clangd_fixture

    report = profile_clangd_workload_gate(
        idx_directory=idx_directory,
        project_root=root,
        iterations=1,
        warmups=0,
        query_workload_size=2,
        symbol_threshold=0.0,
        position_threshold=0.0,
        route_threshold=0.0,
        graph_non_regression_budget=10.0,
        peak_rss_ratio_budget=10.0,
        repeat_rss_spread_budget=10.0,
        max_native_peak_rss_mb=4096.0,
        concurrency_workers=4,
        worker_timeout_seconds=120.0,
    )

    assert report["schema_version"] == 1
    assert report["benchmark"] == "clangd_mixed_workload_gate_v3"
    assert set(report["workloads"]) == {
        "symbol_only",
        "position_first",
        "route_first",
        "mixed",
    }
    assert all(
        workload["parity"]["passed"] for workload in report["workloads"].values()
    )
    assert report["workloads"]["symbol_only"]["native"]["observed_backends"] == [
        "native-clangd-fact-query-v1"
    ]
    native_counts = report["workloads"]["symbol_only"]["native"]["index_counts"][0]
    assert native_counts["definition_count"] == 8
    assert native_counts["occurrence_count"] == 0
    assert native_counts["snapshot_file_count"] == 1
    native_stages = report["workloads"]["symbol_only"]["native"]["stages_seconds"]
    assert "native_decompressed_string_bytes" not in native_stages
    assert "native_materialized_string_bytes" not in native_stages
    assert "native_string_entry_count" not in native_stages
    assert report["workloads"]["position_first"]["native"]["observed_backends"] == [
        "native-clangd-fact-query-v1"
    ]
    position_counts = report["workloads"]["position_first"]["native"]["index_counts"][0]
    assert position_counts["occurrence_count"] > 0
    assert (
        "lazy_native_position_index"
        in report["workloads"]["position_first"]["native"]["stages_seconds"]
    )
    assert report["gates"]["lazy_graph_materialization"]["workloads"]["position_first"][
        "observed_native_counts"
    ] == [0]
    for workload in ("route_first", "mixed"):
        assert report["workloads"][workload]["native"]["observed_backends"] == [
            "native-clangd-fact-query-v1",
            "native-clangd-route-adjacency-v1",
        ]
        assert report["gates"]["lazy_graph_materialization"]["workloads"][workload][
            "observed_native_counts"
        ] == [0]
    assert report["gates"]["lazy_graph_materialization"]["workloads"]["symbol_only"][
        "observed_native_counts"
    ] == [0]
    assert report["concurrency"]["graph_materialization_counts"] == [0]
    assert report["concurrency"]["passed"] is True
    assert report["configuration"]["process_isolated_arms"] is True
    assert report["configuration"]["alternating_arm_order"] is True
    assert 1 <= report["configuration"]["position_query_workload_size"] <= 2
    assert report["configuration"]["position_encoding"] == "UTF16"
    assert report["gates"]["parity"]["passed"] is True
    assert report["gates"]["lazy_graph_materialization"]["passed"] is True
    assert report["gates"]["concurrent_graph_free_route"]["passed"] is True
    assert report["gates"]["version_matrix"]["passed"] is True
    assert report["gates"]["position_first"]["graph_free"] is True
    assert report["gates"]["route_first"]["graph_free"] is True
    for workload in ("symbol_only", "position_first", "route_first"):
        gate = report["gates"][workload]
        legacy_wall = report["workloads"][workload]["legacy"]["wall_seconds"]["median"]
        native_wall = report["workloads"][workload]["native"]["wall_seconds"]["median"]
        assert gate["threshold"] == 0.0
        assert math.isfinite(legacy_wall) and legacy_wall > 0
        assert math.isfinite(native_wall) and native_wall > 0
        assert math.isfinite(gate["improvement_fraction"])
        assert gate["improvement_fraction"] == pytest.approx(
            (legacy_wall - native_wall) / legacy_wall
        )
        assert isinstance(gate["passed"], bool)
        assert report["workloads"][workload]["native"]["observed_fallbacks"] == []
    assert report["repository_preparation"]["included_in_query_ready_gate"] is False
    assert report["content_receipt"]["before"] == report["content_receipt"]["after"]
    assert report["version_matrix"]["generated_fixture_versions"] == [18, 19, 20]
    assert report["subject_manifest"]["subject_count"] == 3
    assert isinstance(report["gates"]["passed"], bool)


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
    assert report["snapshot_receipt"]["snapshot_id"].startswith(
        "clangd_fact_query:sha256:"
    )
    assert 0 <= report["snapshot_receipt"]["fraction_of_native_startup"] <= 1
    assert report["decision"]["parity"] is True
    assert report["mcp_consumer_decision"]["parity"] is True
    assert len(report["raw_samples"]["legacy_clangd_graph"]["consumer_total"]) == 2
    assert (
        len(report["raw_samples"]["native_clangd_fact_query_index"]["consumer_total"])
        == 2
    )
    for count_name in (
        "decompressed_string_bytes",
        "materialized_string_bytes",
        "string_entry_count",
    ):
        assert (
            f"native_stage_{count_name}"
            not in report["raw_samples"]["native_clangd_fact_query_index"]
        )


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
