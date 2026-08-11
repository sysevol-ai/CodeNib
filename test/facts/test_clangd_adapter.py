# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""clangd profile and per-file FactBatch adapter contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_DIAGNOSTICS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    PROVENANCE_CLANGD,
    ClangdFactProfile,
    DiagnosticFact,
    SourceRange,
    fact_batches_from_clangd_records,
    sha256_digest,
)
from codenib.ls_index import clangd_decode
from codenib.ls_index.clangd_decode import (
    KIND_FUNCTION,
    REF_KIND_DEFINITION,
    REF_KIND_REFERENCE,
    REF_KIND_SPELLED,
    ZERO_SYMBOL_ID,
    ClangdGraphDecoder,
)
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION

_CALLER = "11" * 8
_TARGET = "22" * 8
_EXTERNAL = "33" * 8


def _contract() -> dict:
    return {
        "format": "clangd-riff-fact-query-v3",
        "normalization_profile": "clangd-native-normalization-v3",
        "supported_versions": [18, 19, 20],
        "supported_position_encodings": ["UTF8", "UTF16", "UTF32"],
        "resource_limits": {"max_index_files": 200_000},
    }


def _profile() -> ClangdFactProfile:
    return ClangdFactProfile.from_runtime(
        analyzer_version="20.1.0",
        target_triple="x86_64-unknown-linux-gnu",
        toolchain_digest=sha256_digest("clangd binary"),
        compile_commands_digest=sha256_digest("compile commands"),
        build_context_digest=sha256_digest("includes, defines, standard"),
        position_encoding="UTF16",
        native_contract=_contract(),
    )


def _location(root: Path, file_path: str, start: tuple[int, int]):
    return {
        "file": f"file://{root / file_path}",
        "start": start,
        "end": (start[0], start[1] + 3),
    }


def test_real_decoder_collects_records_without_materializing_graph(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    idx_directory = root / ".cache/clangd/index"
    idx_directory.mkdir(parents=True)
    (idx_directory / "one.idx").write_bytes(b"fixture")
    parsed = {
        "symbols": [
            {
                "id": _CALLER,
                "kind": KIND_FUNCTION,
                "name": "caller",
                "scope": "demo::",
                "definition": None,
            }
        ],
        "refs": [],
        "relations": [],
    }
    parse_calls = []

    def parse(path: str):
        parse_calls.append(path)
        return parsed

    monkeypatch.setattr(clangd_decode, "parse_idx_file", parse)
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))

    assert decoder.collect_records(strict=True) is decoder
    assert decoder.collect_records(strict=True) is decoder
    assert parse_calls == [str(idx_directory / "one.idx")]
    assert decoder._records_collected is True
    assert decoder._graph_materialized is False
    assert decoder._symbols[_CALLER]["name"] == "caller"
    assert decoder._id_to_display[_CALLER] == "demo::caller"


def test_real_decoder_strict_collection_is_atomic(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    idx_directory = root / ".cache/clangd/index"
    idx_directory.mkdir(parents=True)
    (idx_directory / "a.idx").write_bytes(b"valid")
    (idx_directory / "b.idx").write_bytes(b"invalid")

    def parse(path: str):
        if path.endswith("b.idx"):
            raise ValueError("corrupt fixture")
        return {
            "symbols": [{"id": _CALLER, "name": "caller", "scope": ""}],
            "refs": [],
            "relations": [],
        }

    monkeypatch.setattr(clangd_decode, "parse_idx_file", parse)
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))

    with pytest.raises(RuntimeError, match="Failed to parse clangd index b.idx"):
        decoder.collect_records(strict=True)

    assert decoder._records_collected is False
    assert decoder._symbols == {}
    assert decoder._refs == {}
    assert decoder._relations == []


def test_real_decoder_strict_collection_rejects_empty_index(tmp_path) -> None:
    root = tmp_path / "repo"
    idx_directory = root / ".cache/clangd/index"
    idx_directory.mkdir(parents=True)
    decoder = ClangdGraphDecoder(str(idx_directory), str(root))

    with pytest.raises(RuntimeError, match=r"No \.idx files found"):
        decoder.collect_records(strict=True)

    assert decoder._records_collected is False

    decoder.collect_records()
    with pytest.raises(RuntimeError, match="record collection was incomplete"):
        decoder.collect_records(strict=True)


class _FakeDecoder:
    def __init__(self, root: Path) -> None:
        self.project_root = root
        self.position_encoding = "UTF16"
        self.collected_strictly = False
        caller_definition = _location(root, "src/caller.cc", (0, 4))
        target_definition = _location(root, "src/target.cc", (0, 4))
        self._symbols = {
            _CALLER: {
                "id": _CALLER,
                "kind": KIND_FUNCTION,
                "name": "caller",
                "scope": "demo::",
                "definition": caller_definition,
                "declaration": caller_definition,
            },
            _TARGET: {
                "id": _TARGET,
                "kind": KIND_FUNCTION,
                "name": "target",
                "scope": "demo::",
                "definition": target_definition,
                "declaration": target_definition,
            },
        }
        self._refs = {
            _CALLER: [
                {
                    "kind": REF_KIND_DEFINITION | REF_KIND_SPELLED,
                    "location": caller_definition,
                    "container": ZERO_SYMBOL_ID,
                }
            ],
            _TARGET: [
                {
                    "kind": REF_KIND_DEFINITION | REF_KIND_SPELLED,
                    "location": target_definition,
                    "container": ZERO_SYMBOL_ID,
                },
                {
                    "kind": REF_KIND_REFERENCE,
                    "location": _location(root, "src/caller.cc", (2, 9)),
                    "container": _CALLER,
                },
            ],
            _EXTERNAL: [
                {
                    "kind": REF_KIND_REFERENCE,
                    "location": _location(root, "src/caller.cc", (3, 9)),
                    "container": _CALLER,
                }
            ],
        }
        self._relations = []

    def collect_records(self, *, strict: bool = False):
        self.collected_strictly = strict
        return self

    def _file_uri_to_relative(self, uri: str) -> str:
        return str(Path(uri.removeprefix("file://")).relative_to(self.project_root))

    @staticmethod
    def _kind_to_node_type(kind: int) -> str:
        assert kind == KIND_FUNCTION
        return NODE_TYPE_FUNCTION


def test_clangd_profile_binds_every_semantic_compatibility_axis() -> None:
    profile = _profile()
    alternatives = (
        replace(profile, analyzer_version="20.1.1"),
        replace(profile, target_triple="aarch64-unknown-linux-gnu"),
        replace(profile, toolchain_digest=sha256_digest("other toolchain")),
        replace(
            profile,
            compile_commands_digest=sha256_digest("other compile commands"),
        ),
        replace(profile, build_context_digest=sha256_digest("other flags")),
        replace(profile, position_encoding="UTF8"),
        replace(profile, native_format="clangd-riff-fact-query-v4"),
        replace(profile, normalization_profile="other-normalization"),
        replace(profile, native_contract_digest=sha256_digest("other contract")),
        replace(profile, supported_riff_versions=(20,)),
    )

    assert all(candidate.digest != profile.digest for candidate in alternatives)
    assert profile.catalog_config()["fact_profile_digest"] == profile.digest
    with pytest.raises(ValueError, match="incomplete"):
        ClangdFactProfile.from_runtime(
            analyzer_version="20",
            target_triple="x86_64",
            toolchain_digest=sha256_digest("tool"),
            compile_commands_digest=sha256_digest("commands"),
            build_context_digest=sha256_digest("context"),
            position_encoding="UTF16",
            native_contract={"format": "missing axes"},
        )


def test_clangd_adapter_emits_exact_per_file_facts_and_unresolved_xrefs(
    tmp_path,
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    caller_source = b"int caller() {\n  return target();\n}\n"
    target_source = b"int target() { return 1; }\n"
    (root / "src/caller.cc").write_bytes(caller_source)
    (root / "src/target.cc").write_bytes(target_source)
    decoder = _FakeDecoder(root)

    batches = fact_batches_from_clangd_records(
        decoder,
        profile=_profile(),
        content_digests={
            "src/caller.cc": sha256_digest(caller_source),
            "src/target.cc": sha256_digest(target_source),
        },
        diagnostics={
            "src/caller.cc": (
                DiagnosticFact(
                    severity="warning",
                    code="fixture-warning",
                    message="fixture diagnostic",
                    source_range=SourceRange(2, 9, 2, 12),
                ),
            ),
        },
        require_native_validation=False,
    )

    assert decoder.collected_strictly is True
    assert [batch.file_path for batch in batches] == [
        "src/caller.cc",
        "src/target.cc",
    ]
    caller, target = batches
    assert caller.capabilities == (
        CAPABILITY_DEFINITIONS,
        CAPABILITY_DIAGNOSTICS,
        CAPABILITY_EDGES,
        CAPABILITY_OCCURRENCES,
    )
    assert caller.diagnostics[0].code == "fixture-warning"
    assert CAPABILITY_DIAGNOSTICS not in target.capabilities
    assert caller.symbols[0].symbol_id == _CALLER
    assert caller.symbols[0].definition_range.sort_key == (0, 4, 0, 7)
    assert target.symbols[0].symbol_id == _TARGET
    assert {
        (item.symbol_moniker, item.source_range.sort_key) for item in caller.occurrences
    } >= {
        (_TARGET, (2, 9, 2, 12)),
        (_EXTERNAL, (3, 9, 3, 12)),
    }
    references = [edge for edge in caller.edges if edge.kind == EDGE_TYPE_REFERENCE]
    assert {edge.target_moniker for edge in references} == {_TARGET, _EXTERNAL}
    assert all(edge.target_symbol_id is None for edge in references)
    assert all(edge.source_symbol_id == _CALLER for edge in references)
    assert all(edge.provenance == PROVENANCE_CLANGD for edge in references)


def test_clangd_adapter_rejects_content_or_snapshot_drift(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/caller.cc").write_text("caller")
    (root / "src/target.cc").write_text("target")
    decoder = _FakeDecoder(root)

    with pytest.raises(RuntimeError, match="bounded native clangd validation"):
        fact_batches_from_clangd_records(
            decoder,
            profile=_profile(),
            file_paths=("src/caller.cc",),
        )

    with pytest.raises(ValueError, match="differs from current bytes"):
        fact_batches_from_clangd_records(
            decoder,
            profile=_profile(),
            content_digests={"src/caller.cc": sha256_digest("stale")},
            require_native_validation=False,
        )

    snapshots = iter(({"snapshot_id": "one"}, {"snapshot_id": "two"}))
    decoder._current_native_snapshot = lambda: next(snapshots)
    with pytest.raises(RuntimeError, match="snapshot changed"):
        fact_batches_from_clangd_records(
            decoder,
            profile=_profile(),
            file_paths=("src/caller.cc",),
            require_native_validation=False,
        )

    decoder = _FakeDecoder(root)
    decoder.query_index = SimpleNamespace(
        fact_query_index=True,
        snapshot_id="clangd_fact_query:sha256:index",
    )
    decoder.query_snapshot_id = "clangd_fact_query:sha256:decoder"
    decoder._decode_native_query_index = lambda: decoder.query_index
    with pytest.raises(RuntimeError, match="not bound to the decoder snapshot"):
        fact_batches_from_clangd_records(
            decoder,
            profile=_profile(),
            file_paths=("src/caller.cc",),
        )

    with pytest.raises(ValueError, match="duplicate.*file paths"):
        fact_batches_from_clangd_records(
            _FakeDecoder(root),
            profile=_profile(),
            file_paths=("src/caller.cc", "src/caller.cc"),
            require_native_validation=False,
        )


def test_clangd_adapter_accepts_current_native_index_profile_pair(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/caller.cc").write_text("caller")
    (root / "src/target.cc").write_text("target")
    decoder = _FakeDecoder(root)
    snapshot_id = "clangd_fact_query:sha256:" + "a" * 64
    native_index = SimpleNamespace(
        fact_query_index=True,
        snapshot_id=snapshot_id,
    )
    decoder.query_index = None
    decoder.query_snapshot_id = snapshot_id
    decoder.query_profile = {}
    decoder.query_backend = "not-decoded"
    decoder._decode_native_query_index = lambda: (
        native_index,
        {"binding": 0.01},
    )
    decoder._current_native_snapshot = lambda: {"snapshot_id": snapshot_id}

    batches = fact_batches_from_clangd_records(
        decoder,
        profile=_profile(),
        file_paths=("src/caller.cc",),
    )

    assert [batch.file_path for batch in batches] == ["src/caller.cc"]
    assert decoder.query_index is native_index
    assert decoder.query_backend == "native-clangd-fact-query-v1"
    assert decoder.query_profile == {"binding": 0.01, "fact_query_native": 1}
